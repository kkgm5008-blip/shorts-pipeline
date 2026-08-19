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
    write_autocut_edl, write_autocut_segments_csv, write_autocut_report,
)
from modules.reference_style import analyze_reference
from modules.autocut import build_segments, apply_target_duration, render_autocut
from modules.yt_download import download_youtube_video

UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)


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


def zip_folder(folder_path: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(folder_path):
            for name in files:
                full = os.path.join(root, name)
                arcname = os.path.relpath(full, folder_path)
                zf.write(full, arcname)
    buf.seek(0)
    return buf.read()


def download_button_for_file(path: str, label: str = None):
    if not path or not os.path.exists(path):
        return
    with open(path, "rb") as f:
        data = f.read()
    st.download_button(label or f"⬇ {os.path.basename(path)}", data, file_name=os.path.basename(path))


st.title("🎬 숏폼 자동화 도구")
st.caption("영상을 올리면 숏폼 추출 / 인트로 체크 / 맞춤법 검사 / 자동 컷편집을 해줍니다. "
           "자막은 절대 영상에 굽지 않아서 프리미어 프로에서 자유롭게 다시 수정할 수 있습니다.")

tab1, tab2 = st.tabs(["📹 숏폼 만들기 (main.py 기능)", "✂️ 자동 컷편집 (autocut.py 기능)"])

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

                st.success("처리 완료! 아래에서 결과를 확인/다운로드하세요.")

                st.markdown("#### 결과")
                r1, r2, r3 = st.columns(3)
                with r1:
                    st.markdown("**자막 / 맞춤법**")
                    download_button_for_file(srt_out, "⬇ 자막 SRT")
                    download_button_for_file(report_path, "⬇ 맞춤법 리포트")
                with r2:
                    st.markdown("**인트로 판단**")
                    st.write(intro_result.reason)
                    if not intro_result.has_probable_intro:
                        st.info(intro_result.suggestion)
                with r3:
                    st.markdown("**프리미어 마커**")
                    download_button_for_file(csv_path, "⬇ 마커 CSV (Import Markers용)")
                    download_button_for_file(txt_path, "⬇ 마커 읽기용 텍스트")

                if clip_infos:
                    st.markdown("#### 숏폼 클립 미리보기")
                    for c in clip_infos:
                        cc1, cc2 = st.columns([2, 1])
                        clip_path = os.path.join(shorts_dir, c["file"])
                        srt_clip_path = os.path.join(shorts_dir, c["srt"])
                        with cc1:
                            st.video(clip_path)
                            st.caption(f"{c['start']:.1f}s ~ {c['end']:.1f}s / 점수 {c['score']:.1f} / {c['preview']}")
                        with cc2:
                            download_button_for_file(clip_path, "⬇ 영상 다운로드")
                            download_button_for_file(srt_clip_path, "⬇ 이 클립 자막 SRT")

                if vertical_path:
                    st.markdown("#### 9:16 전체 변환본")
                    st.video(vertical_path)
                    download_button_for_file(vertical_path, "⬇ 세로 영상 다운로드")

                st.markdown("---")
                zip_bytes = zip_folder(out_dir)
                st.download_button("📦 전체 결과 zip으로 한번에 다운로드", zip_bytes, file_name=f"{base_name}_결과.zip")

            except Exception as e:
                st.error(f"처리 중 오류가 발생했습니다: {e}")
                st.exception(e)

# ============================================================
# TAB 2: 자동 컷편집
# ============================================================
with tab2:
    st.subheader("1. 영상 업로드")
    col1, col2, col3 = st.columns(3)
    with col1:
        raw_video_file = st.file_uploader("편집할 원본(raw) 영상 (필수)", type=["mp4", "mov", "mkv", "avi"], key="t2_video")
    with col2:
        ref_video_file = st.file_uploader("레퍼런스 영상 파일 (선택, 편집 스타일 참고용)", type=["mp4", "mov", "mkv", "avi"], key="t2_ref")
        ref_youtube_url = st.text_input(
            "또는 유튜브 링크로 대신 지정 (선택)",
            value="",
            key="t2_ref_url",
            placeholder="https://www.youtube.com/watch?v=...",
        )
        st.caption("파일과 링크를 둘 다 입력하면 업로드한 파일이 우선 사용됩니다.")
    with col3:
        raw_srt_file = st.file_uploader("원본 영상 자막 SRT (선택)", type=["srt"], key="t2_srt")

    with st.expander("⚙️ 옵션 (기본값 그대로 써도 됩니다)"):
        c1, c2, c3 = st.columns(3)
        with c1:
            noise_db = st.number_input("무음 판정 데시벨 기준 (작을수록 민감)", value=-35.0, key="t2_noisedb")
            min_silence_len = st.number_input("이 길이(초) 이상 조용하면 무음", value=0.5, key="t2_minsil")
        with c2:
            target_duration = st.text_input("목표 최종 길이(초) - 비워두면 압축 안 함", value="", key="t2_target")
            max_clip_factor = st.number_input("레퍼런스 평균 클립 길이의 몇 배까지 허용", value=1.8, key="t2_maxfactor")
        with c3:
            fps2 = st.number_input("EDL용 fps (프리미어 시퀀스와 맞추세요)", value=30.0, key="t2_fps")

    run2 = st.button("🚀 실행", type="primary", key="t2_run")

    if run2:
        if not raw_video_file:
            st.error("편집할 원본 영상을 먼저 올려주세요.")
        else:
            raw_video_path = save_upload(raw_video_file)
            raw_srt_path = save_upload(raw_srt_file) if raw_srt_file else None
            base_name = os.path.splitext(raw_video_file.name)[0]
            out_dir = os.path.join(OUTPUT_DIR, f"{base_name}_autocut")
            os.makedirs(out_dir, exist_ok=True)

            target_dur_val = float(target_duration) if target_duration.strip() else None

            try:
                with st.status("분석 준비 중...", expanded=True) as status:
                    reference_style = None
                    ref_desc = "사용 안 함 (레퍼런스 미지정)"

                    ref_video_path = None
                    if ref_video_file:
                        ref_video_path = save_upload(ref_video_file)
                    elif ref_youtube_url and ref_youtube_url.strip():
                        status.update(label="유튜브 레퍼런스 영상 다운로드 중...")
                        try:
                            ref_video_path = download_youtube_video(ref_youtube_url.strip(), UPLOAD_DIR)
                            st.write(f"유튜브 영상 다운로드 완료: {os.path.basename(ref_video_path)}")
                        except Exception as e:
                            st.warning(f"유튜브 영상을 다운로드하지 못해 레퍼런스 없이 진행합니다: {e}")
                            ref_video_path = None

                    if ref_video_path:
                        status.update(label="레퍼런스 영상 컷 리듬 분석 중...")
                        reference_style = analyze_reference(ref_video_path)
                        ref_desc = reference_style.describe()
                        st.write(ref_desc)

                    subtitle_lines = parse_srt(raw_srt_path) if raw_srt_path else None
                    if subtitle_lines:
                        st.write(f"자막 {len(subtitle_lines)}줄을 재미 점수 계산에 활용합니다.")

                    status.update(label="무음 구간 분석 + 구간별 점수 계산 중...")
                    segments = build_segments(
                        raw_video_path, subtitle_lines=subtitle_lines,
                        noise_db=noise_db, min_silence_len=min_silence_len,
                        reference_style=reference_style, max_clip_factor=max_clip_factor,
                    )
                    segments = apply_target_duration(segments, target_dur_val)
                    original_duration = probe_duration(raw_video_path)
                    kept_duration = sum(s.duration for s in segments)
                    st.write(f"최종 {len(segments)}개 구간 유지, 총 {kept_duration:.1f}초 "
                             f"(원본 {original_duration:.1f}초 대비 {kept_duration/original_duration*100:.1f}%)")

                    if not segments:
                        raise ValueError("남는 구간이 없습니다. 무음 판정 데시벨 값을 낮추거나(-40 등) 최소 무음 길이를 늘려보세요.")

                    status.update(label=f"최종 영상 렌더링 중... (0/{len(segments)} 구간)")
                    autocut_mp4 = os.path.join(out_dir, f"{base_name}_autocut.mp4")

                    def _on_render_progress(done, total):
                        # 구간마다 상태 라벨을 갱신해서 진행 상황을 보여주고,
                        # 프론트엔드로 계속 업데이트를 보내서 연결이 끓기는 것을 줄인다.
                        status.update(label=f"최종 영상 렌더링 중... ({done}/{total} 구간)")

                    render_autocut(raw_video_path, segments, autocut_mp4, progress_callback=_on_render_progress)

                    status.update(label="프리미어용 EDL / 리포트 생성 중...")
                    edl_path = os.path.join(out_dir, f"{base_name}_autocut.edl")
                    csv_path = os.path.join(out_dir, f"{base_name}_autocut_segments.csv")
                    report_path = os.path.join(out_dir, f"{base_name}_autocut_report.txt")
                    write_autocut_edl(edl_path, segments, source_reel_name=os.path.abspath(raw_video_path), fps=fps2)
                    write_autocut_segments_csv(csv_path, segments)
                    write_autocut_report(report_path, original_duration, segments, ref_desc, noise_db, min_silence_len)

                    status.update(label="완료!", state="complete")

                st.success("처리 완료!")

                st.markdown("#### 완성 영상")
                st.video(autocut_mp4)
                dc1, dc2, dc3 = st.columns(3)
                with dc1:
                    download_button_for_file(autocut_mp4, "⬇ 완성 영상 mp4")
                with dc2:
                    download_button_for_file(edl_path, "⬇ 프리미어 EDL (실험적)")
                with dc3:
                    download_button_for_file(csv_path, "⬇ 구간 목록 CSV")
                download_button_for_file(report_path, "⬇ 요약 리포트")

                st.markdown("---")
                zip_bytes = zip_folder(out_dir)
                st.download_button("📦 전체 결과 zip으로 한번에 다운로드", zip_bytes, file_name=f"{base_name}_autocut_결과.zip")

            except Exception as e:
                st.error(f"처리 중 오류가 발생했습니다: {e}")
                st.exception(e)
