"""
레퍼런스 영상의 '편집 리듬(컷 속도)'을 분석하는 모듈.

레퍼런스 영상 = 사용자가 '이런 느낌으로 편집해줘'라고 참고로 주는 다른 영상.
여기서는 정교하게 그 영상의 내용을 따라하는 게 아니라, 컷이 얼마나 자주
바뀌는지(평균 클립 길이)를 뽑아서 -> 원본 영상을 자동 편집할 때 한 구간을
얼마나 길게/짧게 살릴지 정하는 파라미터로 쓴다.

완벽한 '스타일 복제'는 아니고, "이 정도 속도감으로 잘라주면 되겠다"는
감(pace)을 잡아주는 용도의 뻐대 버전 휴리스틱이다.
"""
from dataclasses import dataclass
from typing import List

from .scene_utils import detect_scene_cuts
from .shorts import probe_duration


@dataclass
class ReferenceStyle:
    source: str
    total_duration: float
    cut_count: int
    avg_clip_len: float
    median_clip_len: float
    min_clip_len: float
    max_clip_len: float

    def describe(self) -> str:
        return (
            f"레퍼런스 '{self.source}': 총 {self.total_duration:.1f}초, "
            f"컷 {self.cut_count}회, 평균 클립 길이 {self.avg_clip_len:.2f}초 "
            f"(최소 {self.min_clip_len:.2f}s / 최대 {self.max_clip_len:.2f}s)"
        )


def analyze_reference(
    reference_path: str,
    scene_threshold: float = 0.35,
    max_analyze_sec: float = 300.0,
) -> ReferenceStyle:
    """레퍼런스 영상의 컷 리듬을 분석한다.
    너무 긴 레퍼런스는 앞부분 max_analyze_sec 초만 분석해서 시간을 아낀다."""
    total_duration = probe_duration(reference_path)
    analyze_window = min(total_duration, max_analyze_sec)

    cuts = detect_scene_cuts(reference_path, window_sec=analyze_window, threshold=scene_threshold)
    cuts = sorted(set(round(c, 2) for c in cuts))

    boundaries = [0.0] + cuts + [analyze_window]
    boundaries = sorted(set(boundaries))
    clip_lens = [b - a for a, b in zip(boundaries[:-1], boundaries[1:]) if (b - a) > 0.05]

    if not clip_lens:
        # 컷이 거의 없는 정적인 레퍼런스 -> 기본값으로 대체
        clip_lens = [analyze_window]

    clip_lens_sorted = sorted(clip_lens)
    n = len(clip_lens_sorted)
    median = clip_lens_sorted[n // 2] if n % 2 == 1 else (
        clip_lens_sorted[n // 2 - 1] + clip_lens_sorted[n // 2]
    ) / 2

    return ReferenceStyle(
        source=reference_path,
        total_duration=total_duration,
        cut_count=len(cuts),
        avg_clip_len=sum(clip_lens) / len(clip_lens),
        median_clip_len=median,
        min_clip_len=min(clip_lens),
        max_clip_len=max(clip_lens),
    )
