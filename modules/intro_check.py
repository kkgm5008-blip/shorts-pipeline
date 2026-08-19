"""
인트로 존재 여부 감지 모듈.

'완벽한 인트로 인식 AI'는 아니고, 실용적인 휴리스틱 두 가지를 조합한다.
  1) 영상 시작 구간(window_sec)에 뚜렷한 장면 전환(scene cut)이 있는가?
     -> 타이틀/로고 컷에서 본편으로 넘어가는 전형적인 인트로 패턴.
  2) 자막(대사)이 영상 시작 후 몇 초 만에 바로 시작되는가?
     -> 대사가 0~2초 안에 바로 나오면 인트로 없이 바로 본편(말하는 사람 얼굴 등)일 확률 높음.

두 신호를 함께 보고 '인트로가 없는 것으로 추정됨' 여부와 이유를 리포트한다.
100% 정확하지 않으니 사람이 최종 확인하는 걸 권장(README에 명시).
"""
from dataclasses import dataclass
from typing import List, Optional

from .srt_utils import SubtitleLine
from .scene_utils import detect_scene_cuts as _detect_scene_cuts_full


@dataclass
class IntroCheckResult:
    has_probable_intro: bool
    first_scene_cut_sec: Optional[float]
    first_subtitle_start_sec: Optional[float]
    reason: str
    suggestion: str


def _detect_scene_cuts(video_path: str, window_sec: float, threshold: float = 0.35) -> List[float]:
    """ffmpeg의 scene detection 필터로 시작 구간의 컷 타임스탬프를 뽑는다."""
    return _detect_scene_cuts_full(video_path, window_sec=window_sec, threshold=threshold)


def analyze_intro(
    video_path: str,
    subtitle_lines: List[SubtitleLine],
    window_sec: float = 6.0,
) -> IntroCheckResult:
    try:
        cuts = _detect_scene_cuts(video_path, window_sec)
    except Exception:
        cuts = []

    first_cut = cuts[0] if cuts else None
    first_sub_start = subtitle_lines[0].start if subtitle_lines else None

    # 신호 해석
    has_scene_cut_signal = first_cut is not None and first_cut >= 0.8
    dialogue_starts_fast = first_sub_start is not None and first_sub_start <= 2.0

    if has_scene_cut_signal and not dialogue_starts_fast:
        return IntroCheckResult(
            True, first_cut, first_sub_start,
            f"시작 {first_cut:.1f}초 지점에서 장면 전환이 감지되고, 첫 대사도 "
            f"{first_sub_start if first_sub_start else '알 수 없음'}초에 시작해 "
            "타이틀/인트로 구간이 있는 것으로 보입니다.",
            "별도 조치 불필요.",
        )

    if dialogue_starts_fast and not has_scene_cut_signal:
        return IntroCheckResult(
            False, first_cut, first_sub_start,
            f"첫 대사가 {first_sub_start:.1f}초 만에 시작되고, 그 전에 뚜렷한 "
            "장면 전환도 없어 인트로 없이 바로 본편으로 시작하는 것으로 추정됩니다.",
            "채널 로고/타이틀 카드(2~3초) 또는 훅(hook) 문구를 영상 맨 앞에 "
            "추가하는 것을 권장합니다. output 폴더의 markers.csv에 인트로 "
            "추가 위치(00:00:00)가 마커로 표시되어 있으니 프리미어에서 "
            "마커를 임포트해 바로 확인하세요.",
        )

    if not cuts and not subtitle_lines:
        return IntroCheckResult(
            False, None, None,
            "장면 전환도, 자막도 감지되지 않아 판단할 근거가 부족합니다.",
            "영상을 직접 확인해 인트로 유무를 확인해주세요.",
        )

    # 애매한 경우 (둘 다 약하게 신호가 있거나 둘 다 없음)
    return IntroCheckResult(
        False, first_cut, first_sub_start,
        "명확한 인트로 신호가 약합니다 (장면 전환/대사 시작 시점이 애매함).",
        "영상 앞부분을 직접 확인해 타이틀/훅 구간이 있는지 검토해주세요. "
        "필요하면 2~3초짜리 인트로 카드 추가를 고려하세요.",
    )
