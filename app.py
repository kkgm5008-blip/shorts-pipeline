"""
웹앱 버전 - 브라우저에서 파일 올리고 버튼 눌러서 쓰는 버전.
로컬(내 PC)에서 돌릴 수도 있고, Streamlit Community Cloud 같은 곳에
배포해서 링크로 언제든 접속 가능한 사이트로 쓸 수도 있습니다.
(배포 방법은 README.md의 "온라인 사이트로 배포하기" 참고)

로컬 실행법:
  python -m streamlit run app.py
(윈도우면 run_app.bat 더블클릭해도 됨)

그러면 브라우저가 자동으로 열리면서 http://localhost:8501 로 접속됩니다.
"""
import io
import os
import sys
import traceback
import zipfile

import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from modules.srt_utils import parse_srt
from modules.transcribe import ensure_subtitles
from modules.spellcheck import check_subtitles, write_report
from modules.intro_check import analyze_intro
from modules.shorts import probe_duration, select_highlights, export_highlight_clips, reformat_vertical
from modules.premiere_export import (
    build_markers_from_pipeline, write_premiere_markers_csv, write_readable_markers,
    write_autocut_edl, write_autocut_premiere_xml, write_autocut_segments_csv, write_autocut_report,
)
from modules.reference_style import analyze_reference
from modules.autocut import build_segments, apply_target_duration, render_autocut, parse_target_duration
from modules.yt_download import download_youtube_video
from modules.revision import interpret_revision

UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

# 한 번에 올릴 수 있는 원본(raw) 영상 최대 개수 (tab2).
MAX_RAW_VIDEOS = 5


def _check_password() -> bool:
    """온라인에 공개 배포했을 때, 아무나 링크로 들어와서 쓰는 걸 막기 위한
    간단한 비밀번호 게이트. Streamlit Cloud의 'Secrets'에 APP_PASSWORD를
    설정해두면 그때만 활성화되고, 로컬(내 PC)에서 그냥 돌릴 때는
    설정 안 했을 테니 자동으로 건너뜁니다."""
    try:
        correct = st.secrets["APP_PASSWORD"]
    except Exception:
        return True  # 비밀번호 설정 안 함 -> 로컬 사용으로 간주, 그냥 통과

    if st.session_state.get("authed"):
        return True

    st.title("🔒 비밀번호 입력")
    pw = st.text_input("이 앱은 비밀번호로 보호되어 있습니다.", type="password")
    if st.button("입장"):
        if pw == correct:
            st.session_state["authed"] = True
            st.rerun()
        else:
            st.error("비밀번호가 틀렸습니다.")
    return False


st.set_page_config(page_title="숏폼 자동화 도구", page_icon="🎬", layout="wide")

# Streamlit Cloud의 Secrets에 ANTHROPIC_API_KEY를 넣어뒀다면,
# 맞춤법 검사 모듈(os.environ 기반)이 쓸 수 있게 환경변수로도 복사해준다.
try:
    if "ANTHROPIC_API_KEY" in st.secrets and not os.environ.get("ANTHROPIC_API_KEY"):
        os.environ["ANTHROPIC_API_KEY"] = st.secrets["ANTHROPIC_API_KEY"]
except Exception:
    pass

if not _check_password():
    st.stop()


def save_upload(uploaded_file, subdir=""):
    if uploaded_file is None:
        return None
    target_dir = os.path.join(UPLOAD_DIR, subdir) if subdir else UPLOAD_DIR
    os.makedirs(target_dir, exist_ok=True)
    path = os.path.join(target_dir, uploaded_file.name)
    with open(path, "wb") as f:
        f.write(uploaded_file.getbuffer())
    return path


# zip으로 자동 묶기를 허용할 최대 결과 폴더 크기(MB). zip은 실제로 다운로드
# 버튼을 누른 시점에만 만들어지지만(아래 download_button_for_file/deferred 방식
# 참고), 그 순간에는 폴더 전체를 메모리에 올리게 된다. Streamlit Community
# Cloud 무료 티어는 RAM이 최대 약 2.7GB 정도로 알려져 있어서, 그보다 지나치게
# 크면 zip 생성 자체가 실패하거나 앱이 느려질 수 있다. 그런 경우엔 zip을
# 건너뛰고 이미 있는 개별 다운로드 버튼을 쓰도록 안내한다.
ZIP_AUTO_MAX_MB = 3000


def folder_size_mb(folder_path: str) -> float:
    total = 0
    for root, _, files in os.walk(folder_path):
        for name in files:
            total += os.path.getsize(os.path.join(root, name))
    return total / (1024 * 1024)


def zip_folder(folder_path: str) -> bytes:
    buf = io.BytesIO()
    # ZIP_STORED(무압축)를 쓰는 이유: 결과물 대부분이 이미 압축된 mp4라서
    # DEFLATE로 다시 압축해봐야 용량은 거의 안 줄고 CPU만 더 쓴다(실측상 5배+ 느림).
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for root, _, files in os.walk(folder_path):
            for name in files:
                full = os.path.join(root, name)
                arcname = os.path.relpath(full, folder_path)
                zf.write(full, arcname)
    buf.seek(0)
    return buf.read()


def zip_multiple_folders(folder_paths: list) -> bytes:
    """여러 결과 폴더를 하나의 zip으로 묶는다. 각 폴더의 내용물은 그 폴더
    이름으로 된 하위 폴더 안에 들어가서, 여러 영상을 한번에 처리했을 때
    결과물끼리 파일명이 겹쳐도 서로 덮어쓰지 않는다."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for folder_path in folder_paths:
            subfolder_name = os.path.basename(folder_path.rstrip(os.sep))
            for root, _, files in os.walk(folder_path):
                for name in files:
                    full = os.path.join(root, name)
                    rel = os.path.relpath(full, folder_path)
                    arcname = os.path.join(subfolder_name, rel)
                    zf.write(full, arcname)
    buf.seek(0)
    return buf.read()


def download_button_for_file(path: str, label: str = None, key: str = None):
    """다운로드 버튼을 만든다.

    중요: 파일 내용을 바로 읽어서 넘기지 않고, "콜백(callable)"을 넘긴다.
    이렇게 하면 스트림릿이 파일을 실제로 읽는 시점을 사용자가 그 버튼을
    "클릭했을 때"로 미뤄준다(deferred). 만약 바로 bytes를 읽어서 넘기면,
    이 함수가 호출될 때마다(=화면이 다시 그려질 때마다, 즉 다른 버튼을 눌러도
    매번!) 큰 영상 파일 전체를 메모리에 올리게 되어서 용량이 큰 파일에서는
    다운로드가 매우 느려지거나 무한 대기하는 것처럼 보이는 원인이 된다.
    """
    if not path or not os.path.exists(path):
        return
    st.download_button(
        label or f"⬇ {os.path.basename(path)}",
        data=lambda p=path: open(p, "rb").read(),
        file_name=os.path.basename(path),
        key=key or f"dl_{path}",
    )


st.title("🎬 숏폼 자동화 도구")
st.caption("영상을 올리면 숏폼 추출 / 인트로 체크 / 맞춤법 검사 / 자동 컷편집을 해줍니다. "
           "자막은 절대 영상에 굽지 않아서 프리미어 프로에서 자유롭게 다시 수정할 수 있습니다.")

tab1, tab2, tab3 = st.tabs([
    "📹 숏폼 만들기 (main.py 기능)",
    "✂️ 자동 컷편집 (autocut.py 기능)",
    "📝 맞춤법 검사",
])

# ============================================================
# TAB 1: 숏폼 만들기
# ============================================================
with tab1:
    st.subheader("1. 영상 업로드")
    col1, col2 = st.columns(2)
    with col1:
        video_file = st.file_uploader("원본 영상 (필수)", type=["mp4", "mov", "mkv", "avi"], key="t1_video")
    with col2:
        srt_file = st.file_uploader("자막 SRT (선택, 없으면 자동 생성)", type=["srt"], key="t1_srt")

    with st.expander("⚙️ 옵션 (기본값 그대로 써도 됩니다)"):
        c1, c2, c3 = st.columns(3)
        with c1:
            clip_len = st.number_input("숏폼 클립 길이(초)", value=30.0, min_value=3.0, key="t1_cliplen")
            top_n = st.number_input("숏폼 개수", value=3, min_value=1, step=1, key="t1_topn")
        with c2:
            whisper_model = st.selectbox("STT 모델 크기", ["tiny", "base", "small", "medium", "large-v3"], index=2, key="t1_model")
            language = st.text_input("자막 언어 코드", value="ko", key="t1_lang")
        with c3:
            spellcheck_backend = st.selectbox("맞춤법 검사 엔진", ["auto", "naver", "claude", "offline"], index=0, key="t1_spell")
            fps = st.number_input("마커용 fps (프리미어 시퀀스와 맞추세요)", value=30.0, key="t1_fps")
        skip_vertical = st.checkbox("전체 영상 9:16 변환 생략 (시간 절약)", value=True, key="t1_skipvert")

    run1 = st.button("🚀 실행", type="primary", key="t1_run")

    if run1:
        if not video_file:
            st.error("영상 파일을 먼저 올려주세요.")
        else:
            # 새로 실행하는 것이므로 이전 결과/에러는 지운다.
            st.session_state.pop("tab1_result", None)
            st.session_state.pop("tab1_error", None)

            video_path = save_upload(video_file)
            srt_path = save_upload(srt_file) if srt_file else None
            base_name = os.path.splitext(video_file.name)[0]
            out_dir = os.path.join(OUTPUT_DIR, base_name)
            os.makedirs(out_dir, exist_ok=True)

            try:
                with st.status("자막 확보 중...", expanded=True) as status:
                    srt_out = os.path.join(out_dir, f"{base_name}.srt")
                    lines = ensure_subtitles(video_path, srt_path, srt_out, model_size=whisper_model, language=language)
                    st.write(f"자막 {len(lines)}줄 확보")

                    status.update(label="맞춤법 검사 중...")
                    spell_result = check_subtitles(lines, backend=spellcheck_backend)
                    report_path = os.path.join(out_dir, f"{base_name}_spellcheck_report.txt")
                    write_report(report_path, spell_result)
                    st.write(f"엔진: {spell_result.backend_used} / 이슈 {len(spell_result.issues)}건")

                    status.update(label="인트로 유무 분석 중...")
                    intro_result = analyze_intro(video_path, lines)
                    st.write(("✅ 인트로 있음" if intro_result.has_probable_intro else "⚠️ 인트로 없음(추가 권장)") + f" - {intro_result.reason}")

                    status.update(label=f"숏폼 하이라이트 {top_n}개 추출 중...")
                    duration = probe_duration(video_path)
                    highlights = select_highlights(lines, duration, clip_len=clip_len, top_n=int(top_n))
                    shorts_dir = os.path.join(out_dir, f"{base_name}_shorts")
                    clip_infos = export_highlight_clips(video_path, lines, highlights, shorts_dir, base_name) if highlights else []
                    st.write(f"숏폼 클립 {len(clip_infos)}개 생성" if clip_infos else "하이라이트 후보를 찾지 못했습니다 (자막 부족 또는 영상이 너무 짧음)")

                    vertical_path = None
                    if not skip_vertical:
                        status.update(label="전체 영상 9:16 변환 중...")
                        vertical_path = os.path.join(out_dir, f"{base_name}_vertical.mp4")
                        reformat_vertical(video_path, vertical_path)

                    status.update(label="프리미어 마커 생성 중...")
                    markers = build_markers_from_pipeline(intro_result, highlights, spell_result.issues)
                    csv_path = os.path.join(out_dir, f"{base_name}_markers.csv")
                    txt_path = os.path.join(out_dir, f"{base_name}_markers_readable.txt")
                    write_premiere_markers_csv(csv_path, markers, fps=fps)
                    write_readable_markers(txt_path, markers)

                    status.update(label="완료!", state="complete")

                # 결과를 session_state에 저장해두면, 이후에 다운로드 버튼을
                # 눌러서 스크립트가 다시 실행되어도(=스트림릿 rerun) 결과가
                # 사라지지 않고 계속 화면에 남아있는다.
                st.session_state["tab1_result"] = {
                    "base_name": base_name,
                    "out_dir": out_dir,
                    "srt_out": srt_out,
                    "report_path": report_path,
                    "intro_result": intro_result,
                    "clip_infos": clip_infos,
                    "shorts_dir": shorts_dir,
                    "vertical_path": vertical_path,
                    "csv_path": csv_path,
                    "txt_path": txt_path,
                }

            except Exception as e:
                st.session_state["tab1_error"] = {"message": str(e), "traceback": traceback.format_exc()}

    # ---- 결과 표시 (버튼 클릭으로 인한 rerun에도 사라지지 않도록,
    #      run1 버튼의 True/False와 무관하게 항상 session_state에서 읽어온다) ----
    if st.session_state.get("tab1_error"):
        err = st.session_state["tab1_error"]
        st.error(f"처리 중 오류가 발생했습니다: {err['message']}")
        with st.expander("자세한 오류 내용 보기"):
            st.code(err["traceback"], language="text")

    if st.session_state.get("tab1_result"):
        r = st.session_state["tab1_result"]
        base_name = r["base_name"]

        st.success("처리 완료! 아래에서 결과를 확인/다운로드하세요.")

        st.markdown("#### 결과")
        r1, r2, r3 = st.columns(3)
        with r1:
            st.markdown("**자막 / 맞춤법**")
            download_button_for_file(r["srt_out"], "⬇ 자막 SRT", key="t1_srt_out")
            download_button_for_file(r["report_path"], "⬇ 맞춤법 리포트", key="t1_report")
        with r2:
            st.markdown("**인트로 판단**")
            intro_result = r["intro_result"]
            st.write(intro_result.reason)
            if not intro_result.has_probable_intro:
                st.info(intro_result.suggestion)
        with r3:
            st.markdown("**프리미어 마커**")
            download_button_for_file(r["csv_path"], "⬇ 마커 CSV (Import Markers용)", key="t1_csv")
            download_button_for_file(r["txt_path"], "⬇ 마커 읽기용 텍스트", key="t1_txt")

        if r["clip_infos"]:
            st.markdown("#### 숏폼 클립 미리보기")
            for idx, c in enumerate(r["clip_infos"]):
                cc1, cc2 = st.columns([2, 1])
                clip_path = os.path.join(r["shorts_dir"], c["file"])
                srt_clip_path = os.path.join(r["shorts_dir"], c["srt"])
                with cc1:
                    st.video(clip_path)
                    st.caption(f"{c['start']:.1f}s ~ {c['end']:.1f}s / 점수 {c['score']:.1f} / {c['preview']}")
                with cc2:
                    download_button_for_file(clip_path, "⬇ 영상 다운로드", key=f"t1_clip_{idx}_video")
                    download_button_for_file(srt_clip_path, "⬇ 이 클립 자막 SRT", key=f"t1_clip_{idx}_srt")

        if r["vertical_path"]:
            st.markdown("#### 9:16 전체 변환본")
            st.video(r["vertical_path"])
            download_button_for_file(r["vertical_path"], "⬇ 세로 영상 다운로드", key="t1_vertical")

        st.markdown("---")
        out_size_mb = folder_size_mb(r["out_dir"])
        if out_size_mb <= ZIP_AUTO_MAX_MB:
            st.download_button(
                "📦 전체 결과 zip으로 한번에 다운로드",
                data=lambda d=r["out_dir"]: zip_folder(d),
                file_name=f"{base_name}_결과.zip", key="t1_zip_all",
            )
        else:
            st.info(
                f"결과 폴더가 {out_size_mb:.0f}MB로 커서 zip 묶음은 생략했습니다 "
                "(메모리 절약 목적). 위의 개별 다운로드 버튼을 이용해주세요."
            )

# ============================================================
# TAB 2: 자동 컷편집
# ============================================================
with tab2:
    st.subheader("1. 영상 업로드")
    col1, col2, col3 = st.columns(3)
    with col1:
        raw_video_files = st.file_uploader(
            f"편집할 원본(raw) 영상 (필수, 최대 {MAX_RAW_VIDEOS}개까지 한번에)",
            type=["mp4", "mov", "mkv", "avi"],
            key="t2_video",
            accept_multiple_files=True,
        )
        if raw_video_files and len(raw_video_files) > MAX_RAW_VIDEOS:
            st.warning(f"영상은 최대 {MAX_RAW_VIDEOS}개까지만 처리됩니다. (현재 {len(raw_video_files)}개 선택됨)")
    with col2:
        ref_video_file = st.file_uploader("레퍼런스 영상 파일 (선택, 편집 스타일 참고용)", type=["mp4", "mov", "mkv", "avi"], key="t2_ref")
        ref_youtube_url = st.text_input(
            "또는 유튜브 링크로 대신 지정 (선택)",
            value="",
            key="t2_ref_url",
            placeholder="https://www.youtube.com/watch?v=...",
        )
        st.caption("파일과 링크를 둘 다 입력하면 업로드한 파일이 우선 사용됩니다. "
                   "레퍼런스는 원본 영상이 여러 개여도 1개만 지정하며, 모든 원본 영상에 동일하게 적용됩니다.")
    with col3:
        raw_srt_file = st.file_uploader("원본 영상 자막 SRT (선택)", type=["srt"], key="t2_srt")
        st.caption("원본 영상을 1개만 올렸을 때만 적용됩니다 (여러 개일 땐 무시됩니다).")

    with st.expander("⚙️ 옵션 (기본값 그대로 써도 됩니다)"):
        c1, c2, c3 = st.columns(3)
        with c1:
            noise_db = st.number_input("무음 판정 데시벨 기준 (작을수록 민감)", value=-35.0, key="t2_noisedb")
            min_silence_len = st.number_input("이 길이(초) 이상 조용하면 무음", value=0.5, key="t2_minsil")
        with c2:
            target_duration = st.text_input(
                "목표 최종 길이 - 비워두면 압축 안 함",
                value="", key="t2_target",
                placeholder="예: 420, 7분, 7분30초, 7:30, 70%",
                help="초 단위 숫자 외에도 '7분', '7분30초', '7:30', '70%'(원본 길이의 70%) "
                     "형식으로 적을 수 있습니다. 예: 10분짜리 영상을 7분으로 줄이고 싶으면 "
                     "'7분'이라고 적으면 됩니다.",
            )
            max_clip_factor = st.number_input(
                "컷 리듬(편집 속도) - 레퍼런스 평균 클립 길이의 몇 배까지 허용",
                value=1.8, key="t2_maxfactor",
                help="한 컷을 얼마나 길게 유지할지 정합니다. 값을 낮추면(예: 1.0) 컷이 더 "
                     "빨리빨리 바뀌고, 값을 높이면(예: 2.5) 한 장면을 더 길게 유지해서 "
                     "장면 전환이 느려지고 덜 끊기는 느낌을 줍니다.",
            )
        with c3:
            fps2 = st.number_input("EDL용 fps (프리미어 시퀀스와 맞추세요)", value=30.0, key="t2_fps")

    run2 = st.button("🚀 실행", type="primary", key="t2_run")

    if run2:
        if not raw_video_files:
            st.error("편집할 원본 영상을 먼저 올려주세요.")
        elif len(raw_video_files) > MAX_RAW_VIDEOS:
            st.error(f"원본 영상은 최대 {MAX_RAW_VIDEOS}개까지만 올릴 수 있습니다. 일부를 제거한 뒤 다시 실행해주세요. (현재 {len(raw_video_files)}개)")
        else:
            # 새로 실행하는 것이므로 이전 결과/에러는 지운다.
            st.session_state.pop("tab2_results", None)
            st.session_state.pop("tab2_errors", None)

            # target_duration(목표 최종 길이) 파싱은 "70%" 같은 상대값을 지원하기
            # 위해, 영상마다 원본 길이를 알고 난 뒤(아래 반복문 안) 개별적으로 한다.

            # ---- 레퍼런스는 영상이 여러 개여도 딱 한 번만 준비해서 재사용한다 ----
            reference_style = None
            ref_desc = "사용 안 함 (레퍼런스 미지정)"
            ref_video_path = None
            with st.status("레퍼런스 준비 중...", expanded=True) as ref_status:
                if ref_video_file:
                    ref_video_path = save_upload(ref_video_file)
                elif ref_youtube_url and ref_youtube_url.strip():
                    ref_status.update(label="유튜브 레퍼런스 영상 다운로드 중...")
                    try:
                        ref_video_path = download_youtube_video(ref_youtube_url.strip(), UPLOAD_DIR)
                        st.write(f"유튜브 영상 다운로드 완료: {os.path.basename(ref_video_path)}")
                    except Exception as e:
                        st.warning(f"유튜브 영상을 다운로드하지 못해 레퍼런스 없이 진행합니다: {e}")
                        ref_video_path = None

                if ref_video_path:
                    ref_status.update(label="레퍼런스 영상 컷 리듬 분석 중...")
                    try:
                        reference_style = analyze_reference(ref_video_path)
                        ref_desc = reference_style.describe()
                        st.write(ref_desc)
                    except Exception as e:
                        st.warning(f"레퍼런스 분석에 실패해 레퍼런스 없이 진행합니다: {e}")
                        reference_style = None
                ref_status.update(label="레퍼런스 준비 완료", state="complete")

            # SRT는 원본 영상이 1개일 때만 의미가 있다.
            raw_srt_path = None
            if raw_srt_file and len(raw_video_files) == 1:
                raw_srt_path = save_upload(raw_srt_file)

            results = []
            errors = []

            for i, raw_video_file in enumerate(raw_video_files):
                video_label = raw_video_file.name
                st.markdown(f"### 🎞️ [{i + 1}/{len(raw_video_files)}] {video_label}")
                try:
                    # 파일명이 같은 영상을 여러 개 올려도 서로 덮어쓰지 않도록
                    # 영상마다 별도의 하위 폴더에 저장한다.
                    raw_video_path = save_upload(raw_video_file, subdir=f"t2_{i}")
                    base_name = os.path.splitext(raw_video_file.name)[0]
                    out_dir = os.path.join(OUTPUT_DIR, f"{base_name}_autocut_{i}")
                    os.makedirs(out_dir, exist_ok=True)

                    with st.status(f"[{video_label}] 분석 중...", expanded=True) as status:
                        subtitle_lines = parse_srt(raw_srt_path) if raw_srt_path else None
                        if subtitle_lines:
                            st.write(f"자막 {len(subtitle_lines)}줄을 재미 점수 계산에 활용합니다.")

                        # "70%"처럼 원본 길이에 상대적인 목표 길이를 지원하려면
                        # 이 영상의 원본 길이를 먼저 알아야 한다.
                        original_duration = probe_duration(raw_video_path)
                        target_dur_val = parse_target_duration(target_duration, original_duration)

                        status.update(label="무음 구간 분석 + 구간별 점수 계산 중...")
                        segments = build_segments(
                            raw_video_path, subtitle_lines=subtitle_lines,
                            noise_db=noise_db, min_silence_len=min_silence_len,
                            reference_style=reference_style, max_clip_factor=max_clip_factor,
                        )
                        segments = apply_target_duration(segments, target_dur_val)
                        kept_duration = sum(s.duration for s in segments)
                        st.write(f"최종 {len(segments)}개 구간 유지, 총 {kept_duration:.1f}초 "
                                 f"(원본 {original_duration:.1f}초 대비 {kept_duration/original_duration*100:.1f}%)")

                        if not segments:
                            raise ValueError("남는 구간이 없습니다. 무음 판정 데시벨 값을 낮추거나(-40 등) 최소 무음 길이를 늘려보세요.")

                        status.update(label=f"최종 영상 렌더링 중... (0/{len(segments)} 구간)")
                        autocut_mp4 = os.path.join(out_dir, f"{base_name}_autocut.mp4")

                        def _on_render_progress(done, total, _status=status):
                            # 구간마다 상태 라벨을 갱신해서 진행 상황을 보여주고,
                            # 프론트엔드로 계속 업데이트를 보내 연결이 끊기는 것을 줄인다.
                            _status.update(label=f"최종 영상 렌더링 중... ({done}/{total} 구간)")

                        render_autocut(raw_video_path, segments, autocut_mp4, progress_callback=_on_render_progress)

                        status.update(label="프리미어용 EDL / XML / 리포트 생성 중...")
                        edl_path = os.path.join(out_dir, f"{base_name}_autocut.edl")
                        xml_path = os.path.join(out_dir, f"{base_name}_autocut.xml")
                        csv_path = os.path.join(out_dir, f"{base_name}_autocut_segments.csv")
                        report_path = os.path.join(out_dir, f"{base_name}_autocut_report.txt")
                        write_autocut_edl(edl_path, segments, source_reel_name=os.path.abspath(raw_video_path), fps=fps2)
                        write_autocut_premiere_xml(xml_path, segments, raw_video_path, fps=fps2, title=base_name)
                        write_autocut_segments_csv(csv_path, segments)
                        write_autocut_report(report_path, original_duration, segments, ref_desc, noise_db, min_silence_len)

                        status.update(label="완료!", state="complete")

                    results.append({
                        "base_name": base_name,
                        "out_dir": out_dir,
                        "autocut_mp4": autocut_mp4,
                        "xml_path": xml_path,
                        "edl_path": edl_path,
                        "csv_path": csv_path,
                        "report_path": report_path,
                        # ---- 아래는 화면 표시용이 아니라, "수정 요청하기"로 같은
                        #      영상을 다른 파라미터로 다시 편집할 때 재사용하기
                        #      위해 저장해두는 값들이다. ----
                        "raw_video_path": raw_video_path,
                        "subtitle_lines": subtitle_lines,
                        "reference_style": reference_style,
                        "ref_desc": ref_desc,
                        "fps2": fps2,
                        "original_duration": original_duration,
                        "kept_duration": kept_duration,
                        "params": {
                            "noise_db": noise_db,
                            "min_silence_len": min_silence_len,
                            "max_clip_factor": max_clip_factor,
                            "target_duration": target_dur_val,
                        },
                        "revision_history": [],
                    })
                    st.success(f"[{video_label}] 처리 완료!")
                except Exception as e:
                    errors.append({
                        "video_label": video_label,
                        "message": str(e),
                        "traceback": traceback.format_exc(),
                    })
                    st.error(f"[{video_label}] 처리 중 오류가 발생했습니다: {e}")

            st.session_state["tab2_results"] = results
            st.session_state["tab2_errors"] = errors

    # ---- 결과 표시 (버튼 클릭으로 인한 rerun에도 사라지지 않도록,
    #      run2 버튼의 True/False와 무관하게 항상 session_state에서 읽어온다) ----
    if st.session_state.get("tab2_errors"):
        for err in st.session_state["tab2_errors"]:
            st.error(f"[{err['video_label']}] 처리 중 오류가 발생했습니다: {err['message']}")
            with st.expander(f"[{err['video_label']}] 자세한 오류 내용 보기"):
                st.code(err["traceback"], language="text")

    if st.session_state.get("tab2_results"):
        results = st.session_state["tab2_results"]
        st.markdown("---")
        st.markdown(f"## 📋 결과 ({len(results)}개 영상)")

        for idx, r in enumerate(results):
            base_name = r["base_name"]
            st.markdown(f"### 🎬 {base_name}")
            st.video(r["autocut_mp4"])
            st.caption(
                "**프리미어로 원본 화질 그대로 가져오려면** ⬇ 프리미어 XML을 다운받아서 "
                "프리미어에서 File > Import로 불러오세요. 원본 영상 파일의 경로를 그대로 "
                "담고 있어서, 같은 컴퓨터에 원본이 있으면 보통 자동으로 연결됩니다 "
                "(재인코딩 없이 원본 화질 그대로 컷 편집 시퀀스가 만들어져요). "
                "XML이 안 열리면 EDL을 대신 시도해보세요 (이건 클립 이름 매칭 방식이라 "
                "가끔 수동으로 미디어를 다시 연결해줘야 할 수 있어요)."
            )
            dc1, dc2, dc3, dc4 = st.columns(4)
            with dc1:
                download_button_for_file(r["autocut_mp4"], "⬇ 완성 영상 mp4", key=f"t2_{idx}_mp4")
            with dc2:
                download_button_for_file(r["xml_path"], "⬇ 프리미어 XML (권장)", key=f"t2_{idx}_xml")
            with dc3:
                download_button_for_file(r["edl_path"], "⬇ 프리미어 EDL (대안)", key=f"t2_{idx}_edl")
            with dc4:
                download_button_for_file(r["csv_path"], "⬇ 구간 목록 CSV", key=f"t2_{idx}_csv")
            download_button_for_file(r["report_path"], "⬇ 요약 리포트", key=f"t2_{idx}_report")

            r_out_size_mb = folder_size_mb(r["out_dir"])
            if r_out_size_mb <= ZIP_AUTO_MAX_MB:
                st.download_button(
                    "📦 이 영상 결과 zip으로 한번에 다운로드",
                    data=lambda d=r["out_dir"]: zip_folder(d),
                    file_name=f"{base_name}_autocut_결과.zip", key=f"t2_{idx}_zip",
                )
            else:
                st.info(
                    f"결과 폴더가 {r_out_size_mb:.0f}MB로 커서 zip 묶음은 생략했습니다 "
                    "(메모리 절약 목적). 위의 개별 다운로드 버튼을 이용해주세요."
                )

            # ---- 수정 요청하기: 자유 텍스트로 피드백을 적으면 파라미터를
            #      자동으로 조정해서 이 영상만 다시 편집한다. ----
            if r.get("raw_video_path"):
                with st.expander("🔧 수정 요청하기 (예: '7분으로 줄여줘', '너무 안 이어지는 것 같다')"):
                    if r.get("revision_history"):
                        st.caption("지금까지의 수정 요청 내역:")
                        for h in r["revision_history"]:
                            st.write(f"- \"{h['feedback']}\" → {h['explanation']}")
                        st.markdown("&nbsp;", unsafe_allow_html=True)

                    feedback = st.text_area(
                        "수정하고 싶은 점을 자유롭게 적어주세요",
                        key=f"t2_{idx}_feedback",
                        placeholder="예: 분수가 마음에 안든다, 7분으로 줄여줘 / 너무 안 이어지는 것 같다 / 컷이 너무 빨라요",
                    )
                    st.caption(
                        "입력한 내용을 해석해서 무음 판정 기준, 컷 리듬, 목표 길이 같은 "
                        "파라미터를 자동으로 조정한 뒤 이 영상만 다시 편집합니다. "
                        "(ANTHROPIC_API_KEY가 설정돼 있으면 AI가 문맥까지 이해해서 조정하고, "
                        "없으면 자주 쓰는 표현을 규칙 기반으로 해석합니다.)"
                    )
                    if st.button("🔄 이 요청대로 다시 편집", key=f"t2_{idx}_revise_btn"):
                        if not feedback.strip():
                            st.warning("수정하고 싶은 내용을 먼저 입력해주세요.")
                        else:
                            try:
                                with st.spinner("요청을 해석하고 다시 편집하는 중..."):
                                    new_params = interpret_revision(
                                        feedback, r["params"],
                                        original_duration=r["original_duration"],
                                        kept_duration=r["kept_duration"],
                                    )

                                    new_segments = build_segments(
                                        r["raw_video_path"], subtitle_lines=r["subtitle_lines"],
                                        noise_db=new_params.noise_db, min_silence_len=new_params.min_silence_len,
                                        reference_style=r["reference_style"], max_clip_factor=new_params.max_clip_factor,
                                    )
                                    new_segments = apply_target_duration(new_segments, new_params.target_duration)
                                    if not new_segments:
                                        raise ValueError(
                                            "남는 구간이 없습니다. 요청 내용을 조금 다르게 바꿔서 다시 시도해주세요."
                                        )
                                    new_kept_duration = sum(s.duration for s in new_segments)

                                    render_autocut(r["raw_video_path"], new_segments, r["autocut_mp4"])
                                    write_autocut_edl(
                                        r["edl_path"], new_segments,
                                        source_reel_name=os.path.abspath(r["raw_video_path"]), fps=r["fps2"],
                                    )
                                    write_autocut_premiere_xml(
                                        r["xml_path"], new_segments, r["raw_video_path"],
                                        fps=r["fps2"], title=base_name,
                                    )
                                    write_autocut_segments_csv(r["csv_path"], new_segments)
                                    write_autocut_report(
                                        r["report_path"], r["original_duration"], new_segments, r["ref_desc"],
                                        new_params.noise_db, new_params.min_silence_len,
                                    )

                                    r["params"] = {
                                        "noise_db": new_params.noise_db,
                                        "min_silence_len": new_params.min_silence_len,
                                        "max_clip_factor": new_params.max_clip_factor,
                                        "target_duration": new_params.target_duration,
                                    }
                                    r["kept_duration"] = new_kept_duration
                                    r["revision_history"].append(
                                        {"feedback": feedback, "explanation": new_params.explanation}
                                    )
                                    st.session_state["tab2_results"][idx] = r

                                st.success(
                                    f"다시 편집 완료! 적용된 조정: {new_params.explanation} "
                                    f"(총 {new_kept_duration:.1f}초, 원본 대비 "
                                    f"{new_kept_duration / r['original_duration'] * 100:.1f}%)"
                                )
                                st.rerun()
                            except Exception as e:
                                st.error(f"다시 편집하는 중 오류가 발생했습니다: {e}")

            st.markdown("---")

        if len(results) > 1:
            all_out_dirs = [r["out_dir"] for r in results]
            total_size_mb = sum(folder_size_mb(d) for d in all_out_dirs)
            if total_size_mb <= ZIP_AUTO_MAX_MB:
                st.download_button(
                    f"📦📦 전체 {len(results)}개 영상 결과 한번에 zip 다운로드",
                    data=lambda dirs=tuple(all_out_dirs): zip_multiple_folders(list(dirs)),
                    file_name="전체_autocut_결과.zip", key="t2_zip_all",
                )
            else:
                st.info(
                    f"전체 결과 폴더가 {total_size_mb:.0f}MB로 커서 통합 zip 묶음은 생략했습니다 "
                    "(메모리 절약 목적). 각 영상별 개별 zip/다운로드 버튼을 이용해주세요."
                )

# ============================================================
# TAB 3: 맞춤법 검사 (숏폼/컷편집 없이 맞춤법만 빠르게 확인하고 싶을 때)
# ============================================================
with tab3:
    st.subheader("1. 영상 업로드")
    st.caption(
        "다른 탭과 별도로, 자막 맞춤법/문법만 빠르게 확인하고 싶을 때 쓰는 탭입니다. "
        "숏폼 추출이나 자동 컷편집 없이 자막 확보 + 맞춤법 검사만 진행합니다. "
        "업로드 용량 제한은 다른 탭과 동일합니다."
    )
    col1, col2 = st.columns(2)
    with col1:
        spell_video_file = st.file_uploader(
            "맞춤법을 확인할 영상 (필수)", type=["mp4", "mov", "mkv", "avi"], key="t3_video"
        )
    with col2:
        spell_srt_file = st.file_uploader(
            "자막 SRT (선택, 없으면 자동 생성)", type=["srt"], key="t3_srt"
        )

    with st.expander("⚙️ 옵션 (기본값 그대로 써도 됩니다)"):
        c1, c2 = st.columns(2)
        with c1:
            spell_whisper_model = st.selectbox(
                "STT 모델 크기 (자막을 자동 생성할 때만 사용)",
                ["tiny", "base", "small", "medium", "large-v3"], index=2, key="t3_model",
            )
            spell_language = st.text_input("자막 언어 코드", value="ko", key="t3_lang")
        with c2:
            spell_backend = st.selectbox(
                "맞춤법 검사 엔진", ["auto", "naver", "claude", "offline"], index=0, key="t3_spell"
            )

    run3 = st.button("🚀 맞춤법 검사 실행", type="primary", key="t3_run")

    if run3:
        if not spell_video_file:
            st.error("영상 파일을 먼저 올려주세요.")
        else:
            # 새로 실행하는 것이므로 이전 결과/에러는 지운다.
            st.session_state.pop("tab3_result", None)
            st.session_state.pop("tab3_error", None)

            spell_video_path = save_upload(spell_video_file, subdir="t3")
            spell_srt_path = save_upload(spell_srt_file, subdir="t3") if spell_srt_file else None
            spell_base_name = os.path.splitext(spell_video_file.name)[0]
            spell_out_dir = os.path.join(OUTPUT_DIR, f"{spell_base_name}_spellcheck")
            os.makedirs(spell_out_dir, exist_ok=True)

            try:
                with st.status("자막 확보 중...", expanded=True) as status:
                    spell_srt_out = os.path.join(spell_out_dir, f"{spell_base_name}.srt")
                    spell_lines = ensure_subtitles(
                        spell_video_path, spell_srt_path, spell_srt_out,
                        model_size=spell_whisper_model, language=spell_language,
                    )
                    st.write(f"자막 {len(spell_lines)}줄 확보")

                    status.update(label="맞춤법 검사 중...")
                    spell_result = check_subtitles(spell_lines, backend=spell_backend)
                    spell_report_path = os.path.join(spell_out_dir, f"{spell_base_name}_spellcheck_report.txt")
                    write_report(spell_report_path, spell_result)
                    status.update(label="완료!", state="complete")

                st.session_state["tab3_result"] = {
                    "base_name": spell_base_name,
                    "srt_out": spell_srt_out,
                    "report_path": spell_report_path,
                    "spell_result": spell_result,
                }
            except Exception as e:
                st.session_state["tab3_error"] = {"message": str(e), "traceback": traceback.format_exc()}

    # ---- 결과 표시 (다른 탭들과 같은 이유로 session_state에서 읽어온다) ----
    if st.session_state.get("tab3_error"):
        err = st.session_state["tab3_error"]
        st.error(f"처리 중 오류가 발생했습니다: {err['message']}")
        with st.expander("자세한 오류 내용 보기"):
            st.code(err["traceback"], language="text")

    if st.session_state.get("tab3_result"):
        t3r = st.session_state["tab3_result"]
        spell_result = t3r["spell_result"]
        st.markdown("---")
        st.markdown(f"## 📋 결과: {t3r['base_name']}")
        st.write(
            f"사용 엔진: **{spell_result.backend_used}** / "
            f"검사한 자막 줄 수: {spell_result.checked_lines} / "
            f"발견된 이슈: **{len(spell_result.issues)}건**"
        )

        if spell_result.issues:
            for issue in spell_result.issues:
                with st.expander(f"[{issue.timestamp}] {issue.original}  →  {issue.suggestion}"):
                    st.write(f"**원문**: {issue.original}")
                    st.write(f"**제안**: {issue.suggestion}")
                    st.write(f"**사유**: {issue.reason}")
        else:
            st.success("맞춤법/문법 문제가 발견되지 않았습니다.")

        dc1, dc2 = st.columns(2)
        with dc1:
            download_button_for_file(t3r["srt_out"], "⬇ 자막 SRT", key="t3_dl_srt")
        with dc2:
            download_button_for_file(t3r["report_path"], "⬇ 맞춤법 검사 리포트", key="t3_dl_report")
