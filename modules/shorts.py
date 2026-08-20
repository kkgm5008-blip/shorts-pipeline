"""
숏폼 생성 모듈.
  - select_highlights: 자막 밀도를 기준으로 하이라이트 구간 후보를 고른다.
  - extract_clip: 구간을 잘라 9:16 세로 영상으로 만든다 (자막은 굽지 않음 -> 프리미어에서 수정 가능).
  - reformat_vertical: 영상 전체를 자르지 않고 9:16으로만 변환한다.

자막을 영상에 '굽지(burn-in)' 않는 이유: 사용자가 프리미어 프로에서 자막을
다시 수정할 수 있어야 한다고 했으므로, 항상 별도 SRT 파일로 내보내고
영상에는 텍스트를 합성하지 않는다.
"""
import json
import subprocess
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .srt_utils import SubtitleLine, write_srt


def probe_duration(video_path: str) -> float:
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "json", video_path,
    ]
    out = subprocess.run(cmd, capture_output=True, text=True).stdout
    data = json.loads(out)
    return float(data["format"]["duration"])


@dataclass
class HighlightCandidate:
    start: float
    end: float
    score: float
    preview_text: str


def _score_windows(
    lines: List[SubtitleLine],
    video_duration: float,
    clip_len: float,
    stride: float,
    skip_start_sec: float = 0.0,
    skip_end_sec: float = 0.0,
) -> List[HighlightCandidate]:
    """자막 밀도(초당 글자 수) + 강조 문장부호(!, ?) 기준으로 구간 점수를 매긴다.
    점수 높은 순으로 정렬해서 반환 (아직 겹침 제거는 하지 않은 원시 후보 목록)."""
    candidates: List[HighlightCandidate] = []
    t = skip_start_sec
    limit = video_duration - skip_end_sec
    while t + clip_len <= limit + 1e-6:
        window_start, window_end = t, min(t + clip_len, limit)
        window_lines = [l for l in lines if l.start < window_end and l.end > window_start]
        char_count = sum(len(l.text) for l in window_lines)
        emphasis_bonus = sum(l.text.count("!") + l.text.count("?") for l in window_lines) * 5
        score = char_count / max(clip_len, 1) + emphasis_bonus
        preview = " / ".join(l.text for l in window_lines[:2])
        if window_lines:
            candidates.append(HighlightCandidate(window_start, window_end, score, preview))
        t += stride
    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates


def _pick_nonoverlapping(candidates: List[HighlightCandidate], n: int) -> List[HighlightCandidate]:
    selected: List[HighlightCandidate] = []
    for c in candidates:
        overlap = any(not (c.end <= s.start or c.start >= s.end) for s in selected)
        if not overlap:
            selected.append(c)
        if len(selected) >= n:
            break
    selected.sort(key=lambda c: c.start)
    return selected


def select_highlights(
    lines: List[SubtitleLine],
    video_duration: float,
    clip_len: float = 30.0,
    top_n: int = 3,
    stride: float = 5.0,
) -> List[HighlightCandidate]:
    """자막 밀도 + 강조 문장부호 기준으로 점수를 매겨, 겹치지 않는 top_n 구간을
    고른다. top_n을 크게 주면(예: 8) '후보 목록'으로 쓸 수 있다 - 실제로 숏폼을
    만들지는 호출하는 쪽(화면)에서 사용자가 고른 것만 내보내면 된다."""
    if not lines or video_duration <= 0:
        return []
    candidates = _score_windows(lines, video_duration, clip_len, stride)
    return _pick_nonoverlapping(candidates, top_n)


def select_intro_candidates(
    lines: List[SubtitleLine],
    video_duration: float,
    clip_len: float = 3.0,
    top_n: int = 4,
    stride: float = 2.0,
    skip_start_sec: float = 4.0,
) -> List[HighlightCandidate]:
    """영상 뒷부분에서 훅(hook)으로 쓸만한 짧고 임팩트 있는 구간 후보를 찾는다.
    맨 앞(skip_start_sec 이내)은 이미 '인트로 없음'으로 판정된 구간이므로
    후보에서 제외한다. 로직은 select_highlights와 같은 밀도/강조 점수를
    쓰되, 창 길이를 짧게(기본 3초) 잡아서 '한 문장짜리 훅'에 가깝게 만든다."""
    if not lines or video_duration <= 0:
        return []
    candidates = _score_windows(
        lines, video_duration, clip_len, stride, skip_start_sec=skip_start_sec
    )
    return _pick_nonoverlapping(candidates, top_n)


def _vertical_filter() -> str:
    # 가로 영상을 세로 중앙 기준으로 크롭 후 1080x1920으로 스케일
    return "crop='min(iw,ih*9/16)':'min(ih,iw*16/9)',scale=1080:1920,setsar=1"


def extract_clip(
    input_path: str,
    start: float,
    end: float,
    output_path: str,
    vertical: bool = True,
) -> None:
    duration = end - start
    cmd = ["ffmpeg", "-nostdin", "-y", "-ss", str(start), "-i", input_path, "-t", str(duration)]
    if vertical:
        cmd += ["-vf", _vertical_filter()]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", output_path]
    subprocess.run(cmd, capture_output=True, text=True, check=True)


def reformat_vertical(input_path: str, output_path: str) -> None:
    cmd = [
        "ffmpeg", "-nostdin", "-y", "-i", input_path,
        "-vf", _vertical_filter(),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac",
        output_path,
    ]
    subprocess.run(cmd, capture_output=True, text=True, check=True)


def reformat_vertical_with_intro(
    input_path: str,
    intro_start: float,
    intro_end: float,
    video_duration: float,
    output_path: str,
) -> None:
    """전체 영상을 9:16으로 변환하되, 맨 앞에 인트로 구간을 한 번 더 붙여서
    시작하게 만든다 (원본 재생 순서는 그대로 이어짐, 인트로만 미리보기처럼
    맨 앞에 하나 더 얹는 방식)."""
    extract_clip_with_intro(
        input_path, intro_start, intro_end, 0.0, video_duration, output_path, vertical=True
    )


def extract_clip_with_intro(
    input_path: str,
    intro_start: float,
    intro_end: float,
    main_start: float,
    main_end: float,
    output_path: str,
    vertical: bool = True,
) -> None:
    """같은 영상 안의 두 구간(인트로 구간 + 본편 구간)을 이어붙여 하나의
    출력 파일로 만든다. 별도 임시 파일 없이 ffmpeg concat 필터 하나로 처리한다."""
    vf_tail = ("," + _vertical_filter()) if vertical else ""
    filter_complex = (
        f"[0:v]trim=start={intro_start}:end={intro_end},setpts=PTS-STARTPTS{vf_tail}[v0];"
        f"[0:a]atrim=start={intro_start}:end={intro_end},asetpts=PTS-STARTPTS[a0];"
        f"[0:v]trim=start={main_start}:end={main_end},setpts=PTS-STARTPTS{vf_tail}[v1];"
        f"[0:a]atrim=start={main_start}:end={main_end},asetpts=PTS-STARTPTS[a1];"
        f"[v0][a0][v1][a1]concat=n=2:v=1:a=1[outv][outa]"
    )
    cmd = [
        "ffmpeg", "-nostdin", "-y", "-i", input_path,
        "-filter_complex", filter_complex,
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac",
        output_path,
    ]
    subprocess.run(cmd, capture_output=True, text=True, check=True)


def slice_subtitles_for_clip(
    lines: List[SubtitleLine], start: float, end: float
) -> List[SubtitleLine]:
    """구간에 해당하는 자막만 골라 clip 기준 상대 시간으로 다시 계산."""
    out = []
    for l in lines:
        if l.end <= start or l.start >= end:
            continue
        new_start = max(0.0, l.start - start)
        new_end = min(end - start, l.end - start)
        out.append(SubtitleLine(len(out) + 1, new_start, new_end, l.text))
    return out


def slice_subtitles_with_intro(
    lines: List[SubtitleLine],
    intro_start: float,
    intro_end: float,
    main_start: float,
    main_end: float,
) -> List[SubtitleLine]:
    """인트로 구간 + 본편 구간을 이어붙인 결과물에 맞춰 자막도 같이 이어붙인다
    (본편 자막은 인트로 길이만큼 뒤로 밀어준다)."""
    intro_lines = slice_subtitles_for_clip(lines, intro_start, intro_end)
    main_lines = slice_subtitles_for_clip(lines, main_start, main_end)
    intro_dur = intro_end - intro_start
    shifted_main = [
        SubtitleLine(0, l.start + intro_dur, l.end + intro_dur, l.text) for l in main_lines
    ]
    combined = intro_lines + shifted_main
    for i, l in enumerate(combined, start=1):
        l.index = i
    return combined


def export_highlight_clips(
    input_path: str,
    lines: List[SubtitleLine],
    highlights: List[HighlightCandidate],
    output_dir: str,
    base_name: str,
    intro: Optional[Tuple[float, float]] = None,
) -> List[dict]:
    """선택된 하이라이트 구간들을 실제로 세로 영상으로 렌더링한다.
    intro=(intro_start, intro_end)를 주면 각 클립 맨 앞에 그 구간을 붙여서
    만든다 (자막도 같이 이어붙임)."""
    import os

    os.makedirs(output_dir, exist_ok=True)
    results = []
    for i, h in enumerate(highlights, start=1):
        clip_name = f"{base_name}_short_{i}.mp4"
        srt_name = f"{base_name}_short_{i}.srt"
        clip_path = os.path.join(output_dir, clip_name)
        srt_path = os.path.join(output_dir, srt_name)

        if intro:
            intro_start, intro_end = intro
            extract_clip_with_intro(
                input_path, intro_start, intro_end, h.start, h.end, clip_path, vertical=True
            )
            clip_lines = slice_subtitles_with_intro(lines, intro_start, intro_end, h.start, h.end)
        else:
            extract_clip(input_path, h.start, h.end, clip_path, vertical=True)
            clip_lines = slice_subtitles_for_clip(lines, h.start, h.end)
        write_srt(srt_path, clip_lines)

        results.append({
            "file": clip_name,
            "srt": srt_name,
            "start": h.start,
            "end": h.end,
            "score": h.score,
            "preview": h.preview_text,
        })
    return results
