#!/usr/bin/env python3
"""
바이브 코딩 숏폼 파이프라인 - 뼈대 버전

사용 예:
  python3 main.py --video my_video.mp4
  python3 main.py --video my_video.mp4 --srt my_video.srt
  python3 main.py --video my_video.mp4 --clip-len 20 --top-n 5 --spellcheck-backend auto

동작:
  1) 자막 확보 (SRT 있으면 그대로, 없으면 자동 생성)
  2) 자막 맞춤법 검사 -> 리포트 생성
  3) 인트로 유무 판단 -> 없으면 알림
  4) 숏폼 하이라이트 구간 추출 (9:16 세로 변환, 자막은 안 구움)
  5) 전체 영상도 9:16으로 별도 변환 (원하면)
  6) 프리미어 프로용 마커 CSV + SRT들 output/ 폴더에 정리
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from modules.transcribe import ensure_subtitles
from modules.spellcheck import check_subtitles, write_report
from modules.intro_check import analyze_intro
from modules.shorts import (
    probe_duration, select_highlights, export_highlight_clips, reformat_vertical,
)
from modules.premiere_export import (
    build_markers_from_pipeline, write_premiere_markers_csv, write_readable_markers,
)


def main():
    parser = argparse.ArgumentParser(description="영상 -> 숏폼/인트로체크/맞춤법검사 파이프라인")
    parser.add_argument("--video", required=True, help="원본 영상 파일 경로")
    parser.add_argument("--srt", default=None, help="기존 SRT 자막 파일 경로 (없으면 자동 생성)")
    parser.add_argument("--output-dir", default="output", help="결과물 저장 폴더")
    parser.add_argument("--clip-len", type=float, default=30.0, help="숏폼 하이라이트 길이(초)")
    parser.add_argument("--top-n", type=int, default=3, help="추출할 숏폼 개수")
    parser.add_argument("--whisper-model", default="small", help="STT 모델 크기 (tiny/base/small/medium/large-v3)")
    parser.add_argument("--language", default="ko", help="자막 언어 코드")
    parser.add_argument("--spellcheck-backend", default="auto", choices=["auto", "naver", "claude", "offline"])
    parser.add_argument("--fps", type=float, default=30.0, help="마커 타임코드 계산용 fps (프리미어 시퀀스 fps와 맞추세요)")
    parser.add_argument("--skip-full-vertical", action="store_true", help="전체 영상 9:16 변환 생략(시간 절약)")
    args = parser.parse_args()

    if not os.path.exists(args.video):
        print(f"[오류] 영상 파일을 찾을 수 없습니다: {args.video}")
        sys.exit(1)

    base_name = os.path.splitext(os.path.basename(args.video))[0]
    os.makedirs(args.output_dir, exist_ok=True)

    print("=" * 60)
    print(f"[1/6] 자막 확보 중... ({'기존 SRT 사용' if args.srt else 'STT로 자동 생성'})")
    srt_out = os.path.join(args.output_dir, f"{base_name}.srt")
    try:
        lines = ensure_subtitles(
            args.video, args.srt, srt_out,
            model_size=args.whisper_model, language=args.language,
        )
    except RuntimeError as e:
        print(f"[오류] {e}")
        sys.exit(1)
    print(f"   -> 자막 {len(lines)}줄 확보. 저장: {srt_out}")

    print(f"[2/6] 맞춤법 검사 중... (backend={args.spellcheck_backend})")
    spell_result = check_subtitles(lines, backend=args.spellcheck_backend)
    report_path = os.path.join(args.output_dir, f"{base_name}_spellcheck_report.txt")
    write_report(report_path, spell_result)
    print(f"   -> 엔진: {spell_result.backend_used}, 이슈 {len(spell_result.issues)}건. 저장: {report_path}")

    print("[3/6] 인트로 유무 분석 중...")
    intro_result = analyze_intro(args.video, lines)
    status = "있음(정상)" if intro_result.has_probable_intro else "없음(추가 권장)"
    print(f"   -> 인트로: {status}")
    print(f"   -> 이유: {intro_result.reason}")
    if not intro_result.has_probable_intro:
        print(f"   -> 제안: {intro_result.suggestion}")

    print(f"[4/6] 숏폼 하이라이트 {args.top_n}개 추출 중... (클립 길이 {args.clip_len}초)")
    duration = probe_duration(args.video)
    highlights = select_highlights(lines, duration, clip_len=args.clip_len, top_n=args.top_n)
    shorts_dir = os.path.join(args.output_dir, f"{base_name}_shorts")
    if highlights:
        clip_infos = export_highlight_clips(args.video, lines, highlights, shorts_dir, base_name)
        for c in clip_infos:
            print(f"   -> {c['file']} ({c['start']:.1f}s~{c['end']:.1f}s, 점수 {c['score']:.1f})")
    else:
        clip_infos = []
        print("   -> 하이라이트 후보를 찾지 못했습니다 (자막이 부족하거나 영상이 너무 짧음).")

    if not args.skip_full_vertical:
        print("[5/6] 전체 영상 9:16 세로 변환 중...")
        vertical_out = os.path.join(args.output_dir, f"{base_name}_vertical.mp4")
        reformat_vertical(args.video, vertical_out)
        print(f"   -> 저장: {vertical_out}")
    else:
        print("[5/6] 전체 영상 세로 변환 생략(--skip-full-vertical)")

    print("[6/6] 프리미어 프로용 마커 파일 생성 중...")
    markers = build_markers_from_pipeline(intro_result, highlights, spell_result.issues)
    csv_path = os.path.join(args.output_dir, f"{base_name}_markers.csv")
    txt_path = os.path.join(args.output_dir, f"{base_name}_markers_readable.txt")
    write_premiere_markers_csv(csv_path, markers, fps=args.fps)
    write_readable_markers(txt_path, markers)
    print(f"   -> 저장: {csv_path}, {txt_path}")

    print("=" * 60)
    print("완료! output 폴더를 확인하세요:")
    print(f"  - 자막: {srt_out}")
    print(f"  - 맞춤법 리포트: {report_path}")
    print(f"  - 숏폼 클립: {shorts_dir}/ (각 클립별 SRT 포함)")
    print(f"  - 프리미어 마커: {csv_path} (Marker 패널 > Import Markers)")
    print("=" * 60)


if __name__ == "__main__":
    main()
