"""
자막 맞춤법/문법 검사 모듈.

3가지 백엔드를 지원하고 auto 모드는 아래 순서로 자동 폴백한다.
  1) naver  : 네이버 맞춤법 검사기 (인터넷 필요, 가장 정확함)
  2) claude : Anthropic API (ANTHROPIC_API_KEY 환경변수 필요, 문맥까지 봐줌)
  3) offline: 흔한 오타/표기 규칙 기반 검사 (인터넷 불필요, 최후의 보루)

주의: 이 샌드박스 환경은 외부망이 제한되어 있어 naver 백엔드가 여기서는
차단될 수 있다. 사용자의 로컬 PC(프리미어 작업 환경)에서 실행하면
정상적으로 인터넷에 접근할 수 있으므로 그대로 동작한다.
"""
import json
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import List, Optional

from .srt_utils import SubtitleLine

_NAVER_URL = (
    "https://m.search.naver.com/p/csearch/ocontent/util/SpellerProxy"
    "?_callback=window.__jindo2_callback._spellingCheck_0"
    "&q={query}&where=nexearch&color_blindness=0"
)

# 자주 틀리는 표현 (오타 -> 올바른 표기). 필요시 자유롭게 추가하세요.
_COMMON_MISTAKES = {
    "있읍니다": "있습니다",
    "됬다": "됐다",
    "됬습니다": "됐습니다",
    "안되요": "안 돼요",
    "안됀다": "안 된다",
    "웬지": "왠지",
    "몇일": "며칠",
    "어의없다": "어이없다",
    "금새": "금세",
    "설레임": "설렘",
    "낳다": "낫다",  # 문맥 필요 - 참고용
    "곰곰히": "곰곰이",
    "역활": "역할",
    "가르켜": "가르쳐",
    "부딪히다": "부딪치다",  # 문맥 필요 - 참고용
    "왠만하면": "웬만하면",
    "든지든지": "든지",
    "예기하다": "얘기하다",
    "희안하다": "희한하다",
}


@dataclass
class SpellIssue:
    line_index: int
    timestamp: str
    original: str
    suggestion: str
    reason: str


@dataclass
class SpellCheckResult:
    backend_used: str
    issues: List[SpellIssue] = field(default_factory=list)
    checked_lines: int = 0


def _check_naver(text: str) -> Optional[str]:
    query = urllib.parse.quote(text)
    url = _NAVER_URL.format(query=query)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=6) as r:
        raw = r.read().decode("utf-8", errors="ignore")
    # JSONP -> JSON 추출
    m = re.search(r"\((\{.*\})\)", raw)
    if not m:
        return None
    data = json.loads(m.group(1))
    html = data["message"]["result"]["html"]
    # <span class='re_red'>틀린표현</span> 같은 마크업 제거하면서 교정문 복원
    corrected = re.sub(r"<[^>]+>", "", html)
    return corrected


def _check_offline(text: str) -> List[tuple]:
    """규칙 기반 오프라인 검사. (오류위치설명, 교정안) 리스트 반환."""
    findings = []
    for wrong, right in _COMMON_MISTAKES.items():
        if wrong in text:
            findings.append((wrong, right, "흔한 오타/표기 오류"))
    # 이중 공백
    if "  " in text:
        findings.append(("  ", " ", "공백이 두 번 이상 연속됨"))
    # 문장부호 중복 (마침표/느낌표/물음표 3개 이상은 자막에서 흔히 실수)
    if re.search(r"[.]{4,}", text):
        findings.append(("....+", "...", "마침표가 과도하게 반복됨"))
    return findings


def _check_claude(texts: List[str]) -> Optional[List[str]]:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        import anthropic
    except ImportError:
        return None

    client = anthropic.Anthropic(api_key=api_key)
    numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(texts))
    prompt = (
        "다음은 영상 자막 목록입니다. 각 줄의 맞춤법과 띄어쓰기만 교정해서, "
        "번호와 함께 교정된 문장만 한 줄씩 출력하세요. 오류가 없으면 원문 그대로 출력하세요.\n\n"
        f"{numbered}"
    )
    resp = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    out_text = resp.content[0].text
    corrected = {}
    for line in out_text.splitlines():
        m = re.match(r"\s*(\d+)\.\s*(.*)", line)
        if m:
            corrected[int(m.group(1))] = m.group(2).strip()
    return [corrected.get(i + 1, texts[i]) for i in range(len(texts))]


def check_subtitles(
    lines: List[SubtitleLine], backend: str = "auto"
) -> SpellCheckResult:
    from .srt_utils import seconds_to_ts

    # auto 모드는 문서화된 우선순위(naver -> claude -> offline)를 그대로 따른다.
    # naver는 무료면서 가장 정확하므로 먼저 시도하고, 네트워크가 막혀서 실패할
    # 때만 claude(유료 API)로 넘어간다. (예전엔 순서가 반대로 되어 있어서,
    # ANTHROPIC_API_KEY가 다른 용도로 secrets에 등록돼 있기만 해도 auto 모드가
    # 매번 조용히 Claude API를 써버리는 문제가 있었다.)
    if backend in ("auto", "naver"):
        issues = []
        naver_failed = False
        for line in lines:
            try:
                corrected = _check_naver(line.text)
            except Exception:
                naver_failed = True
                break
            if corrected and corrected != line.text:
                issues.append(
                    SpellIssue(
                        line.index,
                        seconds_to_ts(line.start),
                        line.text,
                        corrected,
                        "네이버 맞춤법 검사기 제안",
                    )
                )
        if not naver_failed:
            return SpellCheckResult("naver", issues, len(lines))
        if backend == "naver":
            return SpellCheckResult("naver(실패: 네트워크 접근 불가)", [], 0)

    if backend in ("auto", "claude"):
        texts = [l.text for l in lines]
        try:
            corrected = _check_claude(texts)
        except Exception:
            corrected = None
        if corrected is not None:
            issues = []
            for line, fixed in zip(lines, corrected):
                if fixed and fixed != line.text:
                    issues.append(
                        SpellIssue(
                            line.index,
                            seconds_to_ts(line.start),
                            line.text,
                            fixed,
                            "AI 문맥 교정 제안",
                        )
                    )
            return SpellCheckResult("claude", issues, len(lines))
        if backend == "claude":
            return SpellCheckResult("claude(실패: API 키 없음/오류)", [], 0)

    # offline fallback
    issues = []
    for line in lines:
        for wrong, right, reason in _check_offline(line.text):
            issues.append(
                SpellIssue(
                    line.index,
                    seconds_to_ts(line.start),
                    line.text,
                    line.text.replace(wrong, right),
                    reason,
                )
            )
    return SpellCheckResult("offline", issues, len(lines))


def write_report(path: str, result: SpellCheckResult) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"맞춤법 검사 결과 (사용 엔진: {result.backend_used})\n")
        f.write(f"검사한 자막 줄 수: {result.checked_lines}\n")
        f.write(f"발견된 이슈 수: {len(result.issues)}\n")
        f.write("=" * 50 + "\n\n")
        if not result.issues:
            f.write("문제가 발견되지 않았습니다.\n")
            return
        for issue in result.issues:
            f.write(f"[{issue.timestamp}] (자막 #{issue.line_index})\n")
            f.write(f"  원문: {issue.original}\n")
            f.write(f"  제안: {issue.suggestion}\n")
            f.write(f"  사유: {issue.reason}\n\n")
