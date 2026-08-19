"""
유튜브 링크로부터 레퍼런스 영상을 다운로드하는 모듈.
서버(로컬 PC 또는 Streamlit Cloud)가 유튜브에 접속 가능한 환경에서만
동작한다 (유튜브 접속이 막힌 네트워크에서는 실패한다).
"""
import os

# 레퍼런스 영상은 컷 리듬(pace)만 참고하면 되므로, 굳이 길거나 고화질일 필요가
# 없다. 링크로 실수로 몇 시간짜리 영상(영화, 풀 스트리밍 등)을 넣는 경우를
# 대비해서 다운로드 자체를 짧은 영상으로 제한한다 (클라우드 무료 티어의
# 제한된 디스크/시간/CPU를 아끼기 위함). analyze_reference()도 어차피 앞
# max_analyze_sec(기본 120초)만 분석하므로, 그보다 넉넉히 여유를 준 값이다.
REFERENCE_MAX_DURATION_SEC = 20 * 60  # 20분


def download_youtube_video(url: str, output_dir: str) -> str:
    """유튜브 URL의 영상을 다운로드해서 output_dir 안에 mp4로 저장하고,
    저장된 파일의 경로를 반환한다. 실패하면 RuntimeError를 던진다."""
    try:
        import yt_dlp
    except ImportError as e:
        raise RuntimeError(
            "yt-dlp가 설치되어 있지 않습니다. requirements.txt에 yt-dlp가 "
            "포함되어 있는지 확인하고 다시 배포/설치해주세요."
        ) from e

    os.makedirs(output_dir, exist_ok=True)
    outtmpl = os.path.join(output_dir, "%(id)s_reference.%(ext)s")

    def _reject_too_long(info):
        duration = info.get("duration")
        if duration and duration > REFERENCE_MAX_DURATION_SEC:
            minutes = REFERENCE_MAX_DURATION_SEC // 60
            return (
                f"영상이 너무 깁니다 ({duration/60:.0f}분). 레퍼런스 영상은 "
                f"컷 리듬만 참고하므로 {minutes}분 이하 영상을 사용해주세요."
            )
        return None

    ydl_opts = {
        "outtmpl": outtmpl,
        # 레퍼런스용이므로 720p 이하로 제한해서 다운로드 용량/시간을 아낀다.
        "format": "bv*[ext=mp4][height<=720]+ba[ext=m4a]/b[ext=mp4][height<=720]/b",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "match_filter": _reject_too_long,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filepath = ydl.prepare_filename(info)
    except Exception as e:
        raise RuntimeError(f"유튜브 영상 다운로드에 실패했습니다: {e}") from e

    base, _ext = os.path.splitext(filepath)
    mp4_path = base + ".mp4"
    if os.path.exists(mp4_path):
        return mp4_path
    if os.path.exists(filepath):
        return filepath
    raise RuntimeError("다운로드된 파일을 찾을 수 없습니다.")
