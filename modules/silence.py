"""
무음 구간 감지 모듈 (ffmpeg silencedetect 기반).
"""
import re
import subprocess
from dataclasses import dataclass
from typing import List, Tuple


@dataclass
class SilenceInterval:
    start: float
    end: float


def detect_silence(
    video_path: str,
    noise_db: float = -35.0,
    min_silence_len: float = 0.5,
) -> List[SilenceInterval]:
    cmd = [
        "ffmpeg", "-nostdin", "-i", video_path,
        "-vn",  # 오디오 분석만 하므로 비디오 디코딩을 생략해 CPU/시간을 크게 절약한다
        "-af", f"silencedetect=noise={noise_db}dB:d={min_silence_len}",
        "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    stderr = proc.stderr

    starts = [float(m) for m in re.findall(r"silence_start:\s*([\d.]+)", stderr)]
    ends = [float(m) for m in re.findall(r"silence_end:\s*([\d.]+)", stderr)]

    intervals = []
    for i, s in enumerate(starts):
        if i < len(ends):
            intervals.append(SilenceInterval(s, ends[i]))
        # 영상이 무음으로 끝나서 silence_end가 안 잡히는 경우는 호출부에서 duration으로 보정
    return intervals


def invert_intervals(
    silences: List[SilenceInterval], total_duration: float, padding: float = 0.12
) -> List[Tuple[float, float]]:
    """무음 구간의 여집합(=소리가 있는 구간)을 계산한다. 단어가 잘리지 않도록
    앞뒤로 약간의 padding을 남긴다."""
    if not silences:
        return [(0.0, total_duration)]

    silences = sorted(silences, key=lambda s: s.start)
    kept = []
    cursor = 0.0
    for s in silences:
        seg_start = cursor
        seg_end = min(s.start + padding, total_duration)
        if seg_end - seg_start > 0.05:
            kept.append((seg_start, seg_end))
        cursor = max(cursor, s.end - padding)
    if cursor < total_duration:
        kept.append((cursor, total_duration))
    return kept
