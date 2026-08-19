"""
유튜브 링크로부터 레퍼런스 영상을 다운로드하는 모듈.
서버(로컬 PC 또는 Streamlit Cloud)가 유튜브에 접속 가능한 환경에서만
동작한다 (유튜브 접속이 막힌 네트워크에서는 실패한다).
"""
import os


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

    ydl_opts = {
        "outtmpl": outtmpl,
        "format": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
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
