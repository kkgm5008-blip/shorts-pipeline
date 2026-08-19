#!/usr/bin/env python3
"""
레퍼런스 스타일 기반 자동 컷편집 - 뼈대 버전

사용 예:
  # 레퍼런스 없이: 무음만 제거
  python3 autocut.py --video raw.mp4

  # 레퍼런스 영상의 컷 리듬을 참고해서 편집
  python3 autocut.py --video raw.mp4 --reference cool_reference.mp4

  # 자막이 있으면 '재미 점수'에 자막 밀도도 반영됨
  python3 autocut.py --video raw.mp4 --srt raw.srt --reference cool_reference.mp4

  # 최종 길이를 90초로 압축 (점수 낮은 구간부터 제거)
  python3 autocut.py --video raw.mp4 --reference cool_reference.mp4 --target-duration 90

결과물 (output/ 폴더):
  {name}_autocut.mp4          -> 무음 제거 + 재미있는 부분 위주로 이어붙인 완성 영상
  {name}_autocut.edl          -> 프리미어 프로 Import용 EDL (실험적, README 참고)
  {name}_autocut_segments.csv -> 어떤 구간을 왜 살렸는지 목록 (EDL이 안 맞을 때 수동 참고용)
  {name}_autocut_report.txt   -> 요약 리포트 (원본 대비 얼마나 줄었는지 등)
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from modules.srt_utils import parse_srt
from modules.shorts import probe_duration
from modules.reference_style import analyze_reference
from modules.autocut import build_segments, apply_target_duration, render_autocut
from modules.premiere_export import (
    write_autocut_edl, write_autocut_segments_csv, write_autocut_report,
)


def main():
    parser = argparse.ArgumentParser(description="레퍼런스 스타일 기반 자동 컷편집 (무음 제거 + 재미있는 부분 살리기)")
    parser.add_argument("--video", required=True, help="편집할 원본(raw) 영상")
    parser.add_argument("--reference", default=None, help="편집 스타일을 참고할 레퍼런스 영상 (선택)")
    parser.add_argument("--srt", default=None, help="원본 영상의 자막 SRT (있으면 재미 점수 계산에 활용)")
    parser.add_argument("--output-dir", default="output", help="결과물 저장 폴더")
    parser.add_argument("--noise-db", type=float, default=-35.0, help="무음 판정 데시벨 기준 (작을수록 민감)")
    parser.add_argument("--min-silence-len", type=float, default=0.5, help="이 길이(초) 이상 조용하면 무음으로 판정")
    parser.add_argument("--target-duration", type=float, default=None, help="목표 최종 길이(초). 지정하면 점수 낮은 구간부터 제거")
    parser.add_argument("--max-clip-factor", type=float, default=1.8, help="레퍼런스 평균 클립 길이의 몇 배까지 한 구간을 허용할지")
    parser.add_argument("--fps", type=float, default=30.0, help="EDL 타임코드 계산용 fps (프리미어 시퀀스와 맞추세요)")
    args = parser.parse_args()

    if not os.path.exists(args.video):
        print(f"[오류] 영상 파일을 찾을 수 없습니다: {args.video}")
        sys.exit(1)

    base_name = os.path.splitext(os.path.basename(args.video))[0]
    os.makedirs(args.output_dir, exist_ok=True)

    reference_style = None
    ref_desc = "사용 안 함 (레퍼런스 미지정)"
    if args.reference:
        if not os.path.exists(args.reference):
            print(f"[오류] 레퍼런스 영상을 찾을 수 없습니다: {args.reference}")
            sys.exit(1)
        print("[1/4] 레퍼런스 영상 컷 리듬 분석 중...")
        reference_style = analyze_reference(args.reference)
        ref_desc = reference_style.describe()
        print(f"   -> {ref_desc}")
    else:
        print("[1/4] 레퍼런스 미지정 - 무음 제거만 수행하고, 클립 길이 제한은 적용하지 않습니다.")

    subtitle_lines = None
    if args.srt:
        if not os.path.exists(args.srt):
            print(f"[경고] SRT 파일을 찾을 수 없어 자막 없이 진행합니다: {args.srt}")
        else:
            subtitle_lines = parse_srt(args.srt)
            print(f"   -> 자막 {len(subtitle_lines)}줄을 재미 점수 계산에 활용합니다.")

    print("[2/4] 무음 구간 분석 + 구간별 점수 계산 중... (영상 길이에 따라 시간이 걸릴 수 있습니다)")
    segments = build_segments(
        args.video,
        subtitle_lines=subtitle_lines,
        noise_db=args.noise_db,
        min_silence_len=args.min_silence_len,
        reference_style=reference_style,
        max_clip_factor=args.max_clip_factor,
    )
    print(f"   -> 무음 제거 후 후보 구간 {len(segments)}개")

    segments = apply_target_duration(segments, args.target_duration)
    kept_duration = sum(s.duration for s in segments)
    original_duration = probe_duration(args.video)
    print(f"   -> 최종 {len(segments)}개 구간 유지, 총 {kept_duration:.1f}초 "
          f"(원본 {original_duration:.1f}초 대비 {kept_duration/original_duration*100:.1f}%)")

    if not segments:
        print("[오류] 남는 구간이 없습니다. --noise-db 값을 낮추거나 --min-silence-len을 늘려보세요.")
        sys.exit(1)

    print("[3/4] 최종 영상 렌더링 중... (구간 수가 많으면 시간이 걸립니다)")
    autocut_mp4 = os.path.join(args.output_dir, f"{base_name}_autocut.mp4")
    render_autocut(args.video, segments, autocut_mp4)
    print(f"   -> 저장: {autocut_mp4}")

    print("[4/4] 프리미어용 EDL / 리포트 생성 중...")
    edl_path = os.path.join(args.output_dir, f"{base_name}_autocut.edl")
    csv_path = os.path.join(args.output_dir, f"{base_name}_autocut_segments.csv")
    report_path = os.path.join(args.output_dir, f"{base_name}_autocut_report.txt")

    write_autocut_edl(edl_path, segments, source_reel_name=os.path.abspath(args.video), fps=args.fps)
    write_autocut_segments_csv(csv_path, segments)
    write_autocut_report(report_path, original_duration, segments, ref_desc, args.noise_db, args.min_silence_len)

    print("=" * 60)
    print("완료!")
    print(f"  - 완성 영상 (바로 쓸 수 있음): {autocut_mp4}")
    print(f"  - 프리미어 EDL (실험적, File > Import): {edl_path}")
    print(f"  - 구간 목록 (EDL 안 맞을 때 수동 참고): {csv_path}")
    print(f"  - 요약 리포트: {report_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
