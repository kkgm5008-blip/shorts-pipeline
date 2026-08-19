"""
자막 확보 모듈.
- 기존 SRT가 주어지면 그대로 사용.
- 없으면 faster-whisper로 음성 인식 후 SRT 생성.
"""
import os
from typing import List, Optional

from .srt_utils import SubtitleLine, parse_srt, write_srt

_MODEL_CACHE = {}


def _get_model(model_size: str = "small"):
    from faster_whisper import WhisperModel

    if model_size not in _MODEL_CACHE:
        # CPU 환경 기준. GPU 있으면 device="cuda"로 바꾸면 훨씬 빠름.
        _MODEL_CACHE[model_size] = WhisperModel(
            model_size, device="cpu", compute_type="int8"
        )
    return _MODEL_CACHE[model_size]


def ensure_subtitles(
    video_path: str,
    existing_srt: Optional[str],
    output_srt_path: str,
    model_size: str = "small",
    language: str = "ko",
) -> List[SubtitleLine]:
    """자막 파일을 확보한다. 기존 SRT가 있으면 복사해서 쓰고,
    없으면 STT로 새로 생성한다. 항상 SubtitleLine 리스트를 반환한다."""

    if existing_srt and os.path.exists(existing_srt):
        lines = parse_srt(existing_srt)
        write_srt(output_srt_path, lines)
        return lines

    try:
        model = _get_model(model_size)
    except Exception as e:
        raise RuntimeError(
            "STT 모델을 다운로드하지 못했습니다. 이 컴퓨터가 인터넷에 연결되어 "
            "있는지 확인해주세요 (최초 실행 시 Whisper 모델 파일을 huggingface.co "
            f"에서 내려받습니다). 원본 오류: {e}"
        ) from e

    segments, info = model.transcribe(
        video_path,
        language=language,
        vad_filter=True,  # 무음 구간 자동 스킵
    )

    lines: List[SubtitleLine] = []
    for i, seg in enumerate(segments, start=1):
        text = seg.text.strip()
        if not text:
            continue
        lines.append(SubtitleLine(i, seg.start, seg.end, text))

    write_srt(output_srt_path, lines)
    return lines
