"""
아주 가벼운 SRT 읽기/쓰기 유틸리티.
외부 라이브러리(pysrt 등) 없이 표준 SRT 포맷만 다룬다.
"""
from dataclasses import dataclass
from typing import List


@dataclass
class SubtitleLine:
    index: int
    start: float  # seconds
    end: float    # seconds
    text: str


def _ts_to_seconds(ts: str) -> float:
    # 00:00:01,234
    h, m, rest = ts.split(":")
    s, ms = rest.split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def seconds_to_ts(sec: float) -> str:
    if sec < 0:
        sec = 0
    ms = int(round((sec - int(sec)) * 1000))
    total = int(sec)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def parse_srt(path: str) -> List[SubtitleLine]:
    with open(path, "r", encoding="utf-8-sig") as f:
        content = f.read()

    blocks = [b.strip() for b in content.strip().split("\n\n") if b.strip()]
    lines: List[SubtitleLine] = []
    for block in blocks:
        rows = block.splitlines()
        if len(rows) < 2:
            continue
        # rows[0] = 인덱스, rows[1] = 타임스탬프, rows[2:] = 텍스트
        try:
            idx = int(rows[0].strip())
        except ValueError:
            # 일부 SRT는 인덱스가 없을 수 있음
            idx = len(lines) + 1
            rows = [str(idx)] + rows
        ts_row = rows[1]
        start_str, end_str = [t.strip() for t in ts_row.split("-->")]
        start = _ts_to_seconds(start_str)
        end = _ts_to_seconds(end_str.split(" ")[0])
        text = "\n".join(rows[2:]).strip()
        lines.append(SubtitleLine(idx, start, end, text))
    return lines


def write_srt(path: str, lines: List[SubtitleLine]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for i, line in enumerate(lines, start=1):
            f.write(f"{i}\n")
            f.write(f"{seconds_to_ts(line.start)} --> {seconds_to_ts(line.end)}\n")
            f.write(f"{line.text}\n\n")
