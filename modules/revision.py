"""
사용자가 자유롭게 적은 '수정 요청' 텍스트(예: "분수가 마음에 안든다, 7분으로
줄여줘", "너무 안 이어지는 것 같다", "컷이 너무 빨라요")를 해석해서, 자동
컷편집(autocut) 파라미터를 어떻게 바꿔서 다시 편집할지 결정하는 모듈.

spellcheck.py와 같은 패턴을 따른다:
  1) ANTHROPIC_API_KEY가 설정되어 있으면 Claude에게 문맥을 이해시켜서
     파라미터를 제안받는다 (가장 똑똑하지만 API 키/네트워크가 필요).
  2) 실패하거나 키가 없으면 자주 나올 법한 표현들을 규칙 기반으로 해석하는
     휴리스틱으로 대체한다 (인터넷/API 키 없이도 항상 동작).
"""
import json
import os
import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class RevisionParams:
    noise_db: float
    min_silence_len: float
    max_clip_factor: float
    target_duration: Optional[float]
    explanation: str


def _extract_duration_expr(feedback: str, original_duration: Optional[float]) -> Optional[float]:
    """자유 문장 속에 섞여 있는 '7분', '7분30초', '7:30', '420초', '70%' 같은
    길이 표현을 찾아 초 단위로 변환한다. parse_target_duration()과 달리 문장
    전체가 아니라 문장 '안'에서 표현을 찾아낸다(re.search 기반)."""
    m = re.search(r"(\d+)\s*:\s*(\d{1,2}(?:\.\d+)?)", feedback)
    if m:
        return int(m.group(1)) * 60 + float(m.group(2))

    m = re.search(r"(\d+(?:\.\d+)?)\s*분\s*(?:(\d+(?:\.\d+)?)\s*초)?", feedback)
    if m:
        mins = float(m.group(1))
        secs = float(m.group(2)) if m.group(2) else 0.0
        return mins * 60 + secs

    m = re.search(r"(\d+(?:\.\d+)?)\s*초", feedback)
    if m:
        return float(m.group(1))

    m = re.search(r"(\d+(?:\.\d+)?)\s*%", feedback)
    if m and original_duration:
        return original_duration * float(m.group(1)) / 100.0

    return None


def _rule_based_interpret(
    feedback: str,
    current: dict,
    original_duration: Optional[float],
    kept_duration: Optional[float],
) -> RevisionParams:
    noise_db = current["noise_db"]
    min_silence_len = current["min_silence_len"]
    max_clip_factor = current["max_clip_factor"]
    target_duration = current.get("target_duration")
    notes = []

    # 1) "7분으로 줄여줘" 처럼 명시적인 길이 표현이 있으면 최우선으로 반영한다.
    explicit = _extract_duration_expr(feedback, original_duration)
    if explicit:
        target_duration = explicit
        notes.append(f"목표 길이를 {target_duration:.0f}초로 설정")
    else:
        if any(k in feedback for k in ["짧게", "줄여", "축소", "짧아"]):
            base = kept_duration or target_duration or original_duration
            if base:
                target_duration = round(base * 0.8, 1)
                notes.append(f"현재보다 20% 짧게(약 {target_duration:.0f}초)로 조정")
        elif any(k in feedback for k in ["길게", "늘려", "확대", "길어"]):
            base = kept_duration or target_duration or original_duration
            if base:
                cap = original_duration or (base * 1.25)
                target_duration = round(min(base * 1.25, cap), 1)
                notes.append(f"현재보다 25% 길게(약 {target_duration:.0f}초)로 조정")

    # 2) 흐름/이어짐 관련 피드백 -> 한 컷을 더 길게 유지 + 무음 판정을 덜 민감하게
    if any(k in feedback for k in ["안 이어지", "안이어지", "매끄럽지", "부자연스럽", "뚝뚝", "끊기", "튀는", "튄다"]):
        max_clip_factor = round(max_clip_factor * 1.3, 2)
        min_silence_len = round(min_silence_len * 1.3, 2)
        notes.append(
            f"더 매끄럽게 이어지도록 컷 길이 허용치를 {max_clip_factor}배로, "
            f"무음 판정 길이를 {min_silence_len}초로 늘림"
        )

    # 3) 속도(리듬) 관련 피드백
    #    "너무 빨라요/정신없어요" = 컷을 더 여유있게(느리게), "느리다/지루하다" = 컷을 더 빠르게
    slow_down_kw = ["너무 빨라", "정신없", "느긋하게", "여유있게", "여유 있게", "천천히", "차분하게"]
    speed_up_kw = ["더 빠르게", "속도감", "컷이 느려", "느리다", "지루", "템포 좀", "박진감"]
    if any(k in feedback for k in slow_down_kw):
        max_clip_factor = round(max_clip_factor * 1.3, 2)
        notes.append(f"컷 전환이 덜 정신없도록 컷 길이 허용치를 {max_clip_factor}배로 늘림")
    elif any(k in feedback for k in speed_up_kw):
        max_clip_factor = round(max_clip_factor * 0.7, 2)
        notes.append(f"컷이 더 빠르게 바뀌도록 컷 길이 허용치를 {max_clip_factor}배로 줄임")

    # 4) 남는 무음/여백 관련 피드백
    if any(k in feedback for k in ["무음이 많이", "여백이 많", "군더더기", "쓸데없는 부분", "늘어진다"]):
        min_silence_len = max(0.15, round(min_silence_len * 0.7, 2))
        notes.append(f"무음 판정 길이를 {min_silence_len}초로 줄여 더 적극적으로 잘라냄")

    if not notes:
        notes.append(
            "구체적인 조정 규칙을 찾지 못해 기존 값을 그대로 유지했습니다. "
            "조금 더 구체적으로 적어주시면 반영하기 쉬워요 "
            "(예: '7분으로 줄여줘', '컷이 너무 빨라요', '너무 안 이어져요')."
        )

    return RevisionParams(
        noise_db=noise_db,
        min_silence_len=min_silence_len,
        max_clip_factor=max_clip_factor,
        target_duration=target_duration,
        explanation=" / ".join(notes),
    )


def _claude_interpret(
    feedback: str,
    current: dict,
    original_duration: Optional[float],
    kept_duration: Optional[float],
) -> Optional[RevisionParams]:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        import anthropic
    except ImportError:
        return None

    prompt = f"""당신은 영상 자동 컷편집 파라미터를 조정하는 도우미입니다.
사용자가 방금 만들어진 결과물에 대해 아래와 같은 수정 요청을 했습니다.

수정 요청: "{feedback}"

현재 파라미터:
- noise_db (무음 판정 데시벨 기준, 작을수록/음수로 클수록 민감하게 무음으로 판정): {current['noise_db']}
- min_silence_len (이 길이(초) 이상 조용하면 무음으로 판정해서 잘라냄): {current['min_silence_len']}
- max_clip_factor (레퍼런스 평균 클립 길이의 몇 배까지 한 컷을 길게 유지할지, 클수록 컷이 느려지고 작을수록 컷이 빨라짐): {current['max_clip_factor']}
- target_duration (목표 최종 길이, 초 단위, null이면 길이 제한 없음): {current.get('target_duration')}
- 원본 영상 길이(초): {original_duration}
- 방금 만들어진 결과물 길이(초): {kept_duration}

사용자의 요청 의도를 반영해서 새 파라미터 값을 제안하세요.
다른 설명 없이 아래 JSON 형식으로만 답하세요:
{{"noise_db": 숫자, "min_silence_len": 숫자, "max_clip_factor": 숫자, "target_duration": 숫자 또는 null, "explanation": "무엇을 어떻게 왜 바꿨는지 한국어로 1~2문장"}}
"""
    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    out_text = resp.content[0].text
    m = re.search(r"\{.*\}", out_text, re.S)
    if not m:
        return None
    data = json.loads(m.group(0))

    target_duration = data.get("target_duration")
    return RevisionParams(
        noise_db=float(data.get("noise_db", current["noise_db"])),
        min_silence_len=float(data.get("min_silence_len", current["min_silence_len"])),
        max_clip_factor=float(data.get("max_clip_factor", current["max_clip_factor"])),
        target_duration=(float(target_duration) if target_duration is not None else None),
        explanation=data.get("explanation") or "AI가 요청을 반영해 파라미터를 조정했습니다.",
    )


def interpret_revision(
    feedback: str,
    current: dict,
    original_duration: Optional[float] = None,
    kept_duration: Optional[float] = None,
) -> RevisionParams:
    """수정 요청 텍스트 -> 새 자동 컷편집 파라미터.

    ANTHROPIC_API_KEY가 설정돼 있으면 AI로 문맥을 이해해서 해석하고,
    없거나 호출이 실패하면 규칙 기반 휴리스틱으로 대체한다
    (spellcheck.py의 auto 백엔드 폴백과 같은 안전망 패턴).
    """
    try:
        result = _claude_interpret(feedback, current, original_duration, kept_duration)
    except Exception:
        result = None
    if result is not None:
        return result
    return _rule_based_interpret(feedback, current, original_duration, kept_duration)
