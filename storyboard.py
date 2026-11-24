#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
storyboard.py —— 把一条视频压成一张「可读宫格图」，让纯文本 AI 也能看懂镜头结构。

原理：
  1. ffmpeg scene 检测找出镜头切换点（画面突变处）
  2. 每个镜头抽 1 帧
  3. 拼成宫格，每格烧上「编号 + 时间码」
  4. 输出 jpg（给 AI 看）+ json（给程序用）

依赖：ffmpeg（必须，且在 PATH）、Pillow（pip install pillow）

用法：
  python storyboard.py 视频.mp4
  python storyboard.py 视频.mp4 --threshold 0.35 --cols 4 --max 24
  python storyboard.py 视频.mp4 -o D:\\输出目录

输出：
  <视频名>_storyboard.jpg   宫格图
  <视频名>_shots.json       镜头表（序号/起止/时长）

注意：
  threshold 越低越敏感（0.2 会把轻微变化也当切换），越高越保守（0.5 只抓大切换）。
  默认 0.3，适合大多数成片。
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    sys.exit("缺少 Pillow。请先运行：  pip install pillow")

# Windows 中文/英文可用字体（优先中文字体，时间码里的数字也一样好看）
FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def find_ffmpeg():
    for name in ("ffmpeg", "ffmpeg.exe"):
        p = shutil.which(name)
        if p:
            return p
    sys.exit("找不到 ffmpeg。请安装 ffmpeg 并确保在 PATH 里。")


def load_font(size):
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def probe_duration(ffmpeg, video):
    """用 ffmpeg 读时长（不依赖 ffprobe，少一个依赖）"""
    out = subprocess.run([ffmpeg, "-i", video], capture_output=True, text=True, errors="ignore")
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", out.stderr)
    if not m:
        return 0.0
    h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
    return h * 3600 + mi * 60 + s


def detect_scenes(ffmpeg, video, threshold):
    """返回镜头的起始时间点列表（秒），不含 0"""
    cmd = [
        ffmpeg, "-hide_banner", "-i", video,
        "-vf", f"select='gt(scene,{threshold})',showinfo",
        "-f", "null", "-",
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, errors="ignore")
    times = [float(x) for x in re.findall(r"pts_time:([0-9.]+)", out.stderr)]
    return times


def build_shots(scene_times, duration):
    """把切换点变成 (起, 止) 区间"""
    bounds = [0.0] + sorted(t for t in scene_times if 0.05 < t < duration)
    shots = []
    for i, start in enumerate(bounds):
        end = bounds[i + 1] if i + 1 < len(bounds) else duration
        if end - start < 0.15:      # 太短的碎片丢掉（闪白、转场杂点）
            continue
        shots.append((start, end))
    return shots


def grab_frame(ffmpeg, video, at, out_png):
    """在 at 秒处抓一帧（取偏后一点，避开转场的第一帧黑场）"""
    cmd = [
        ffmpeg, "-y", "-v", "error",
        "-ss", f"{at:.3f}", "-i", video,
        "-frames:v", "1", "-q:v", "2", out_png,
    ]
    subprocess.run(cmd, capture_output=True)


def fmt_time(sec):
    m, s = divmod(sec, 60)
    return f"{int(m):02d}:{s:04.1f}"


def make_sheet(frames, shots, out_jpg, cols=4, cell_w=480, pad=10, header=56):
    """拼宫格，每格上方烧「编号 + 起始时间 + 时长」"""
    if not frames:
        sys.exit("没抓到任何帧")

    # 以第一帧的宽高比决定单元格高度
    with Image.open(frames[0]) as im0:
        ratio = im0.height / im0.width
    cell_h = int(cell_w * ratio)

    rows = (len(frames) + cols - 1) // cols
    width = cols * cell_w + (cols + 1) * pad
    height = header + rows * (cell_h + header) + (rows + 1) * pad

    sheet = Image.new("RGB", (width, height), (18, 18, 20))
    draw = ImageDraw.Draw(sheet)
    font_title = load_font(26)
    font_label = load_font(22)
    font_idx = load_font(34)

    draw.text((pad + 4, 14), "STORYBOARD  ·  shot / start / duration",
              fill=(150, 150, 155), font=font_title)

    for i, (png, (s, e)) in enumerate(zip(frames, shots)):
        r, c = divmod(i, cols)
        x = pad + c * (cell_w + pad)
        y = header + pad + r * (cell_h + header + pad)

        with Image.open(png) as im:
            im = im.convert("RGB")
            im = im.resize((cell_w, cell_h), Image.LANCZOS)
            sheet.paste(im, (x, y))

        # 大编号（左上角压在画面上）
        draw.rectangle([x, y, x + 62, y + 46], fill=(0, 0, 0))
        draw.text((x + 12, y + 4), f"{i+1:02d}", fill=(255, 200, 60), font=font_idx)

        # 时间码（画面下方）
        label = f"start {fmt_time(s)}   dur {e-s:.1f}s"
        draw.text((x + 4, y + cell_h + 6), label, fill=(210, 210, 215), font=font_label)

    sheet.save(out_jpg, quality=88)
    return width, height


def main():
    ap = argparse.ArgumentParser(description="视频 → 镜头宫格图（给 AI 读）")
    ap.add_argument("video", help="输入视频路径")
    ap.add_argument("-o", "--outdir", default=None, help="输出目录，默认与视频同目录")
    ap.add_argument("--threshold", type=float, default=0.3, help="场景检测灵敏度，默认 0.3")
    ap.add_argument("--cols", type=int, default=4, help="宫格列数，默认 4")
    ap.add_argument("--max", type=int, default=24, help="最多保留几个镜头，默认 24")
    args = ap.parse_args()

    if not os.path.isfile(args.video):
        sys.exit(f"找不到视频：{args.video}")

    ffmpeg = find_ffmpeg()
    video = os.path.abspath(args.video)
    stem = os.path.splitext(os.path.basename(video))[0]
    outdir = os.path.abspath(args.outdir or os.path.dirname(video))
    os.makedirs(outdir, exist_ok=True)

    print(f"[1/4] 读时长: {video}")
    duration = probe_duration(ffmpeg, video)
    if duration <= 0:
        sys.exit("读不出视频时长，文件可能损坏或不是视频。")
    print(f"      时长 {duration:.2f}s")

    print(f"[2/4] 场景检测 (threshold={args.threshold})")
    scenes = detect_scenes(ffmpeg, video, args.threshold)
    shots = build_shots(scenes, duration)
    print(f"      检测到 {len(scenes)} 个切换点 → {len(shots)} 个镜头")

    if len(shots) > args.max:
        # 太多镜头时按等间隔抽，保证覆盖全片
        step = len(shots) / args.max
        shots = [shots[int(i * step)] for i in range(args.max)]
        print(f"      超过 {args.max} 个，已等间隔抽到 {args.max} 个")

    print("[3/4] 抽帧")
    tmp = tempfile.mkdtemp(prefix="sb_")
    frames = []
    for i, (s, e) in enumerate(shots):
        png = os.path.join(tmp, f"f{i:03d}.png")
        grab_frame(ffmpeg, video, s + min(0.35, (e - s) * 0.35), png)
        if os.path.exists(png):
            frames.append(png)
        else:
            shots[i] = None
    pairs = [(f, sh) for f, sh in zip(frames, shots) if sh]
    frames = [p[0] for p in pairs]
    shots = [p[1] for p in pairs]

    print("[4/4] 拼宫格")
    jpg = os.path.join(outdir, f"{stem}_storyboard.jpg")
    w, h = make_sheet(frames, shots, jpg, cols=args.cols)

    data = {
        "video": video,
        "duration": round(duration, 3),
        "threshold": args.threshold,
        "shot_count": len(shots),
        "shots": [
            {"no": i + 1, "start": round(s, 3), "end": round(e, 3), "duration": round(e - s, 3)}
            for i, (s, e) in enumerate(shots)
        ],
    }
    js = os.path.join(outdir, f"{stem}_shots.json")
    with open(js, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    for p in frames:
        try:
            os.remove(p)
        except OSError:
            pass
    try:
        os.rmdir(tmp)
    except OSError:
        pass

    print()
    print(f"宫格图 : {jpg}  ({w}x{h})")
    print(f"镜头表 : {js}")
    print(f"镜头数 : {len(shots)}")
    print()
    print("把宫格图交给 AI，它就能说出：有几个镜头、每个多长、怎么切、节奏怎么样。")


if __name__ == "__main__":
    main()
