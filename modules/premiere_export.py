"""
프리미어 프로 호환 출력 모듈.

프리미어에서 바로 쓸 수 있게 두 가지를 만든다.
  1) markers.csv : 프리미어 Marker 패널의 'Import Markers'로 불러올 수 있는
     탭 구분 CSV. (시퀀스 시작 타임코드가 00:00:00:00이 아니면 어긋날 수 있으니
     README에 안내함)
  2) markers_readable.txt : CSV 임포트가 안 맞을 경우를 대비한 사람이 읽는
     마커 목록 (몇 분 몇 초에 뭐가 있는지). 이건 100% 수동으로도 활용 가능.

자막은 이 모듈이 아니라 STT/spellcheck 단계에서 이미 표준 SRT로 나오며,
SRT는 프리미어가 네이티브로 임포트 가능하다 (File > Import 또는 캡션 임포트).
"""
import os
import re
from dataclasses import dataclass
from typing import List


def seconds_to_tc(seconds: float, fps: float = 30.0) -> str:
    if seconds < 0:
        seconds = 0
    total_frames = round(seconds * fps)
    frames = int(total_frames % fps)
    total_seconds = int(total_frames // fps)
    s = total_seconds % 60
    m = (total_seconds // 60) % 60
    h = total_seconds // 3600
    return f"{h:02d}:{m:02d}:{s:02d}:{frames:02d}"


@dataclass
class Marker:
    name: str
    description: str
    in_sec: float
    out_sec: float
    marker_type: str = "Comment"


def write_premiere_markers_csv(path: str, markers: List[Marker], fps: float = 30.0) -> None:
    lines = ["Marker Name\tDescription\tIn\tOut\tDuration\tMarker Type"]
    for m in markers:
        in_tc = seconds_to_tc(m.in_sec, fps)
        out_tc = seconds_to_tc(m.out_sec, fps)
        dur_tc = seconds_to_tc(max(0.0, m.out_sec - m.in_sec), fps)
        lines.append(f"{m.name}\t{m.description}\t{in_tc}\t{out_tc}\t{dur_tc}\t{m.marker_type}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def write_readable_markers(path: str, markers: List[Marker]) -> None:
    def fmt(sec: float) -> str:
        m = int(sec // 60)
        s = sec % 60
        return f"{m:02d}:{s:05.2f}"

    with open(path, "w", encoding="utf-8") as f:
        f.write("영상 마커 목록 (수동으로 프리미어에 마커 찍을 때 참고하세요)\n")
        f.write("=" * 50 + "\n\n")
        for m in markers:
            f.write(f"[{fmt(m.in_sec)} ~ {fmt(m.out_sec)}] {m.name}\n")
            if m.description:
                f.write(f"   -> {m.description}\n")
            f.write("\n")


def build_markers_from_pipeline(intro_result, highlights, spell_issues) -> List[Marker]:
    markers: List[Marker] = []

    if not intro_result.has_probable_intro:
        markers.append(Marker(
            name="인트로 없음 - 확인 필요",
            description=intro_result.reason + " " + intro_result.suggestion,
            in_sec=0.0,
            out_sec=2.0,
            marker_type="Comment",
        ))

    for i, h in enumerate(highlights, start=1):
        markers.append(Marker(
            name=f"숏폼 하이라이트 후보 #{i}",
            description=f"점수 {h.score:.1f} / 미리보기: {h.preview_text}",
            in_sec=h.start,
            out_sec=h.end,
            marker_type="Comment",
        ))

    for issue in spell_issues[:50]:  # 마커가 너무 많아지지 않도록 상위 50개만
        from .srt_utils import _ts_to_seconds
        try:
            t = _ts_to_seconds(issue.timestamp)
        except Exception:
            continue
        markers.append(Marker(
            name="맞춤법 확인",
            description=f"'{issue.original}' -> '{issue.suggestion}' ({issue.reason})",
            in_sec=t,
            out_sec=t + 0.5,
            marker_type="Comment",
        ))

    markers.sort(key=lambda m: m.in_sec)
    return markers


def write_autocut_edl(
    path: str,
    segments,
    source_reel_name: str,
    fps: float = 30.0,
    title: str = "AUTOCUT",
) -> None:
    """자동 컷편집 구간을 CMX3600 EDL로 내보낸다.
    Premiere에서 File > Import 로 불러오면 새 시퀀스가 만들어지는데,
    reel 이름이 프로젝트의 원본 클립 이름과 다르면 '미디어 연결' 창이
    뜰 수 있다. 그때 원본 영상 파일을 지정해주면 된다.

    EDL은 비디오 컷 위주의 단순한 인터체인지 포맷이라 100% 보장은 못한다.
    안 맞으면 같이 만들어지는 *_segments.csv / *_report.txt 를 참고해서
    수동으로 인/아웃을 찍는 게 가장 확실하다.
    """
    reel = re.sub(r"[^A-Za-z0-9]", "", source_reel_name).upper()[:8] or "AX"

    lines = [f"TITLE: {title}", "FCM: NON-DROP FRAME", ""]
    rec_cursor = 0.0
    for i, seg in enumerate(segments, start=1):
        src_in = seconds_to_tc(seg.start, fps)
        src_out = seconds_to_tc(seg.end, fps)
        rec_in = seconds_to_tc(rec_cursor, fps)
        rec_cursor += (seg.end - seg.start)
        rec_out = seconds_to_tc(rec_cursor, fps)
        lines.append(f"{i:03d}  {reel:<8} V     C        {src_in} {src_out} {rec_in} {rec_out}")
        lines.append(f"* FROM CLIP NAME: {os.path.basename(source_reel_name)}")
        lines.append("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def write_autocut_segments_csv(path: str, segments) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write("index,start_sec,end_sec,duration_sec,score,reason\n")
        for i, seg in enumerate(segments, start=1):
            f.write(
                f"{i},{seg.start:.2f},{seg.end:.2f},{seg.end - seg.start:.2f},"
                f"{seg.score:.2f},\"{seg.reason}\"\n"
            )


def write_autocut_report(
    path: str,
    original_duration: float,
    segments,
    reference_style_desc: str,
    noise_db: float,
    min_silence_len: float,
) -> None:
    kept_duration = sum(seg.end - seg.start for seg in segments)
    removed = original_duration - kept_duration
    removed_pct = (removed / original_duration * 100) if original_duration > 0 else 0

    with open(path, "w", encoding="utf-8") as f:
        f.write("자동 컷편집 리포트\n")
        f.write("=" * 50 + "\n\n")
        f.write(f"원본 길이: {original_duration:.1f}초\n")
        f.write(f"편집본 길이: {kept_duration:.1f}초\n")
        f.write(f"제거된 길이: {removed:.1f}초 ({removed_pct:.1f}%)\n")
        f.write(f"유지된 구간 수: {len(segments)}\n")
        f.write(f"무음 판정 기준: {noise_db}dB 이하가 {min_silence_len}초 이상 지속\n")
        f.write(f"레퍼런스 스타일: {reference_style_desc}\n\n")
        f.write("구간별 상세는 같은 폴더의 *_segments.csv 참고\n")
