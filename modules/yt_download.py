"""
유튜브 링크로부터 레퍼런스 영상을 다운로드하는 모듈.
서버(로컬 PC 또는 Streamlit Cloud)가 유튜브에 접속 가능한 환경에서만
동작한다 (유튜브 접속이 막힌 네트워크에서는 실패한다).

주의: Streamlit Cloud 같은 클라우드 호스팅은 데이터센터 IP를 쓰기 때문에,
유튜브가 이를 "봇"으로 간주해서 HTTP 403(Forbidden)으로 다운로드 자체를
막는 경우가 흔하다. 이럴 때는 cookies_path에 본인 유튜브 로그인 쿠키
파일(cookies.txt, Netscape 형식)을 넘겨주면, 로그인된 사용자의 요청처럼
보이게 되어 우회에 성공하는 경우가 있다 (단, 100% 보장되지는 않는다 -
유튜브가 봇 탐지를 계속 강화하고 있어서 쿠키를 넣어도 막힐 수 있다).
"""
import os

# 레퍼런스 영상은 컷 리듬(pace)만 참고하면 되므로, 굳이 길거나 고화질일 필요가
# 없다. 링크로 실수로 몇 시간짜리 영상(영화, 풀 스트리밍 등)을 넣는 경우를
# 대비해서 다운로드 자체를 짧은 영상으로 제한한다 (클라우드 무료 티어의
# 제한된 디스크/시간/CPU를 아끼기 위함). analyze_reference()도 어차피 앞
# max_analyze_sec(기본 120초)만 분석하므로, 그보다 넉넉히 여유를 준 값이다.
REFERENCE_MAX_DURATION_SEC = 20 * 60  # 20분


def download_youtube_video(
    url: str,
    output_dir: str,
    cookies_path: str = None,
    max_duration_sec: int = REFERENCE_MAX_DURATION_SEC,
    max_height: int = 720,
) -> str:
    """유튜브 URL의 영상을 다운로드해서 output_dir 안에 mp4로 저장하고,
    저장된 파일의 경로를 반환한다. 실패하면 RuntimeError를 던진다.

    브라우저로 직접 파일을 업로드하면 Streamlit이 영상 전체를 먼저 서버
    메모리에 올리기 때문에, 큰 영상(1GB+)에서는 메모리 부족으로 서버가
    죽을 수 있다. yt-dlp는 디스크에 스트리밍으로 바로 저장하므로 이 문제를
    피할 수 있어서, 큰 영상은 파일 업로드 대신 유튜브 링크로 지정하는 것을
    권장한다.

    cookies_path: (선택) 유튜브 로그인 쿠키가 담긴 cookies.txt(Netscape 형식)
    경로. 클라우드 서버 IP가 봇으로 차단(403)당할 때 우회를 시도하는 용도.
    max_duration_sec: 이보다 긴 영상은 다운로드를 거부한다 (레퍼런스 영상은
    짧게, 숏폼 소스 영상은 길게 등 호출하는 쪽에서 용도에 맞게 조절).
    max_height: 다운로드할 최대 해상도(세로 픽셀). 낮출수록 다운로드 용량과
    이후 처리(STT, 인코딩) 부담이 줄어든다.
    """
    try:
        import yt_dlp
    except ImportError as e:
        raise RuntimeError(
            "yt-dlp가 설치되어 있지 않습니다. requirements.txt에 yt-dlp가 "
            "포함되어 있는지 확인하고 다시 배포/설치해주세요."
        ) from e

    os.makedirs(output_dir, exist_ok=True)
    outtmpl = os.path.join(output_dir, "%(id)s.%(ext)s")

    def _reject_too_long(info):
        duration = info.get("duration")
        if duration and duration > max_duration_sec:
            minutes = max_duration_sec // 60
            return (
                f"영상이 너무 깁니다 ({duration/60:.0f}분). {minutes}분 "
                f"이하 영상만 지원합니다."
            )
        return None

    ydl_opts = {
        "outtmpl": outtmpl,
        "format": (
            f"bv*[ext=mp4][height<={max_height}]+ba[ext=m4a]/"
            f"b[ext=mp4][height<={max_height}]/b"
        ),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "match_filter": _reject_too_long,
        # 유튜브가 클라우드 서버를 실제 브라우저처럼 보이게 하기 위한 값.
        # (봇 탐지 우회에 도움이 될 수 있으나 완전한 해결책은 아니다.)
        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
    }
    if cookies_path and os.path.exists(cookies_path):
        ydl_opts["cookiefile"] = cookies_path

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filepath = ydl.prepare_filename(info)
    except Exception as e:
        hint = ""
        if "403" in str(e) and not cookies_path:
            hint = (
                " (유튜브가 서버 접속을 차단한 것으로 보입니다 - 쿠키 파일을 "
                "함께 넣으면 우회가 될 수도 있습니다.)"
            )
        raise RuntimeError(f"유튜브 영상 다운로드에 실패했습니다: {e}{hint}") from e

    base, _ext = os.path.splitext(filepath)
    mp4_path = base + ".mp4"
    if os.path.exists(mp4_path):
        return mp4_path
    if os.path.exists(filepath):
        return filepath
    raise RuntimeError("다운로드된 파일을 찾을 수 없습니다.")
