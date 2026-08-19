"""
장면 전환(scene cut) 감지 공용 유틸리티.
intro_check.py 와 reference_style.py 가 공유해서 쓴다.
"""
import re
import subprocess
from typing import List, Optional


def detect_scene_cuts(
    video_path: str,
    window_sec: Optional[float] = None,
    threshold: float = 0.35,
) -> List[float]:
    """ffmpeg의 scene detection 필터로 컷 타임스탬프(초) 리스트를 뽑는다.
    window_sec가 None이면 영상 전체를 분석한다 (긴 영상은 시간이 걸릴 수 있음)."""
    cmd = ["ffmpeg", "-i", video_path, "-vf", f"select='gt(scene,{threshold})',showinfo"]
    if window_sec is not None:
        cmd += ["-t", str(window_sec + 1)]
    cmd += ["-f", "null", "-"]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    stderr = proc.stderr
    times = []
    for m in re.finditer(r"pts_time:([\d.]+)", stderr):
        t = float(m.group(1))
        if window_sec is None or t <= window_sec:
            times.append(t)
    return times
