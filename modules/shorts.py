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


def select_highlights(
    lines: List[SubtitleLine],
    video_duration: float,
    clip_len: float = 30.0,
    top_n: int = 3,
    stride: float = 5.0,
) -> List[HighlightCandidate]:
    """자막 밀도(초당 글자 수) + 강조 문장부호(!, ?) 기준으로 구간 점수를 매겨
    점수 높은 순으로 겹치지 않는 top_n 구간을 고른다."""
    if not lines or video_duration <= 0:
        return []

    candidates: List[HighlightCandidate] = []
    t = 0.0
    while t + clip_len <= video_duration + 1e-6:
        window_start, window_end = t, min(t + clip_len, video_duration)
        window_lines = [l for l in lines if l.start < window_end and l.end > window_start]
        char_count = sum(len(l.text) for l in window_lines)
        emphasis_bonus = sum(l.text.count("!") + l.text.count("?") for l in window_lines) * 5
        score = char_count / max(clip_len, 1) + emphasis_bonus
        preview = " / ".join(l.text for l in window_lines[:2])
        if window_lines:
            candidates.append(HighlightCandidate(window_start, window_end, score, preview))
        t += stride

    candidates.sort(key=lambda c: c.score, reverse=True)

    selected: List[HighlightCandidate] = []
    for c in candidates:
        overlap = any(not (c.end <= s.start or c.start >= s.end) for s in selected)
        if not overlap:
            selected.append(c)
        if len(selected) >= top_n:
            break

    selected.sort(key=lambda c: c.start)
    return selected


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
    cmd = ["ffmpeg", "-y", "-ss", str(start), "-i", input_path, "-t", str(duration)]
    if vertical:
        cmd += ["-vf", _vertical_filter()]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", output_path]
    subprocess.run(cmd, capture_output=True, text=True, check=True)


def reformat_vertical(input_path: str, output_path: str) -> None:
    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-vf", _vertical_filter(),
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


def export_highlight_clips(
    input_path: str,
    lines: List[SubtitleLine],
    highlights: List[HighlightCandidate],
    output_dir: str,
    base_name: str,
) -> List[dict]:
    import os

    os.makedirs(output_dir, exist_ok=True)
    results = []
    for i, h in enumerate(highlights, start=1):
        clip_name = f"{base_name}_short_{i}.mp4"
        srt_name = f"{base_name}_short_{i}.srt"
        clip_path = os.path.join(output_dir, clip_name)
        srt_path = os.path.join(output_dir, srt_name)

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
