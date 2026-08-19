"""
레퍼런스 스타일 기반 자동 컷편집 모듈.

흐름:
  1) 무음 구간을 찾아서 제거 대상으로 표시 (silence.py)
  2) 소리가 있는 구간(=발화/액션 구간) 후보를 만든다
  3) 각 후보 구간의 '재미 점수'를 매긴다
     - 자막이 있으면: 글자 밀도 + 강조 문장부호(!, ?)
     - 자막이 없으면: 오디오 음량(라우드니스)을 프록시로 사용
  4) 레퍼런스 영상에서 뽑은 평균 클립 길이(pace)를 참고해서,
     너무 긴 후보 구간은 그 길이에 맞춰 앞부분만 살리고 잘라낸다
     (레퍼런스처럼 컷이 빠르게 바뀌는 느낌을 흉내내기 위함)
  5) (선택) 목표 총 길이가 주어지면 점수 낮은 구간부터 제거해서 길이를 맞춘다
  6) 최종 구간들을 순서대로 이어붙인 mp4 + EDL + 구간 리스트 CSV를 만든다

주의: 이건 '뼈대' 버전 휴리스틱입니다. 문장 중간에서 잘릴 수도 있고,
레퍼런스 스타일을 완벽히 복제하지도 않습니다. 실제로 돌려보면서
파라미터(무음 임계값, 목표 길이 등)를 조절해가는 걸 권장합니다.
"""
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import List, Optional

from .silence import detect_silence, invert_intervals
from .shorts import probe_duration
from .srt_utils import SubtitleLine
from .reference_style import ReferenceStyle


@dataclass
class CutSegment:
    start: float
    end: float
    score: float
    reason: str

    @property
    def duration(self) -> float:
        return self.end - self.start


def _measure_loudness(video_path: str, start: float, dur: float) -> float:
    """구간의 평균 음량(dB, mean_volume)을 잰다. 클수록(0에 가까울수록) 큰 소리."""
    if dur <= 0:
        return -91.0
    cmd = [
        "ffmpeg", "-nostdin", "-ss", str(start), "-t", str(dur), "-i", video_path,
        "-vn",  # 오디오 분석만 하므로 비디오 디코딩을 생략해 CPU/시간을 크게 절약한다
        "-af", "volumedetect", "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    m = re.search(r"mean_volume:\s*(-?[\d.]+)\s*dB", proc.stderr)
    return float(m.group(1)) if m else -91.0


def build_segments(
    video_path: str,
    subtitle_lines: Optional[List[SubtitleLine]] = None,
    noise_db: float = -35.0,
    min_silence_len: float = 0.5,
    reference_style: Optional[ReferenceStyle] = None,
    max_clip_factor: float = 1.8,
    min_keep_len: float = 0.4,
    use_loudness_scoring: bool = True,
) -> List[CutSegment]:
    total_duration = probe_duration(video_path)
    silences = detect_silence(video_path, noise_db=noise_db, min_silence_len=min_silence_len)
    kept_ranges = invert_intervals(silences, total_duration)
    kept_ranges = [(s, e) for s, e in kept_ranges if (e - s) >= min_keep_len]

    # 레퍼런스 페이스에 맞춰 너무 긴 구간은 앞부분만 잘라서 쓴다
    cap = None
    if reference_style is not None:
        cap = reference_style.avg_clip_len * max_clip_factor

    trimmed_ranges = []
    for s, e in kept_ranges:
        if cap and (e - s) > cap:
            trimmed_ranges.append((s, s + cap))
        else:
            trimmed_ranges.append((s, e))

    segments: List[CutSegment] = []
    for s, e in trimmed_ranges:
        reason_parts = []
        score = 0.0

        if subtitle_lines:
            window_lines = [l for l in subtitle_lines if l.start < e and l.end > s]
            char_count = sum(len(l.text) for l in window_lines)
            emphasis = sum(l.text.count("!") + l.text.count("?") for l in window_lines) * 5
            score += char_count / max(e - s, 1) + emphasis
            if window_lines:
                reason_parts.append(f"자막밀도 {char_count}자")

        if use_loudness_scoring:
            loud = _measure_loudness(video_path, s, e - s)
            # -91dB(무음)~0dB(최대) -> 0~91 스케일 점수로 변환
            loud_score = max(0.0, 91.0 + loud)
            score += loud_score * 0.5
            reason_parts.append(f"음량 {loud:.1f}dB")

        segments.append(CutSegment(s, e, score, ", ".join(reason_parts) or "무음 아님"))

    return segments


def apply_target_duration(
    segments: List[CutSegment], target_duration: Optional[float]
) -> List[CutSegment]:
    """목표 길이가 주어지면 점수 낮은 구간부터 제거해서 총 길이를 맞춘다.
    (원래 순서는 유지)"""
    if not target_duration:
        return segments

    kept = list(segments)
    total = sum(s.duration for s in kept)
    ranked_by_score_asc = sorted(kept, key=lambda s: s.score)

    i = 0
    while total > target_duration and i < len(ranked_by_score_asc):
        victim = ranked_by_score_asc[i]
        if victim in kept:
            kept.remove(victim)
            total -= victim.duration
        i += 1

    kept.sort(key=lambda s: s.start)
    return kept


def render_autocut(
    video_path: str,
    segments: List[CutSegment],
    output_path: str,
) -> None:
    if not segments:
        raise ValueError("잘라 붙일 구간이 없습니다 (모든 구간이 무음이거나 필터링됨).")

    tmp_dir = output_path + "_parts"
    os.makedirs(tmp_dir, exist_ok=True)
    part_paths = []
    for i, seg in enumerate(segments):
        part_path = os.path.join(tmp_dir, f"part_{i:04d}.mp4")
        cmd = [
            "ffmpeg", "-nostdin", "-y", "-ss", str(seg.start), "-t", str(seg.duration),
            "-i", video_path,
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
            "-threads", "2",
            "-c:a", "aac", "-avoid_negative_ts", "make_zero",
            part_path,
        ]
        subprocess.run(cmd, capture_output=True, text=True, check=True)
        part_paths.append(part_path)

    concat_list_path = os.path.join(tmp_dir, "concat_list.txt")
    with open(concat_list_path, "w", encoding="utf-8") as f:
        for p in part_paths:
            f.write(f"file '{os.path.abspath(p)}'\n")

    cmd = [
        "ffmpeg", "-nostdin", "-y", "-f", "concat", "-safe", "0", "-i", concat_list_path,
        "-c", "copy", output_path,
    ]
    subprocess.run(cmd, capture_output=True, text=True, check=True)

    # 임시 파트 파일 정리
    for p in part_paths:
        try:
            os.remove(p)
        except OSError:
            pass
    try:
        os.remove(concat_list_path)
        os.rmdir(tmp_dir)
    except OSError:
        pass
