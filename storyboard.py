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

def fix_console_encoding():
    """
    Windows 上 Python 的输出编码和终端代码页经常对不上，导致中文变乱码。
    这里读一下真实控制台代码页，把 stdout/stderr 调成同一个编码。
    输出被重定向（管道/文件）时不动，保持调用方期望的编码。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        cp = ctypes.windll.kernel32.GetConsoleOutputCP()
        if cp in (0, None):
            # 输出被重定向（管道/文件）时没有控制台，统一用 UTF-8 更通用
            try:
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
                sys.stderr.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
            return
        target = {936: "gbk", 950: "big5", 65001: "utf-8", 54936: "gb18030"}.get(cp)
        if not target:
            return
        cur = (sys.stdout.encoding or "").lower().replace("-", "")
        if cur and cur != target.replace("-", ""):
            try:
                sys.stdout.reconfigure(encoding=target, errors="replace")
                sys.stderr.reconfigure(encoding=target, errors="replace")
            except Exception:
                pass
    except Exception:
        pass


fix_console_encoding()

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
    # 1. 先看 PATH
    for name in ("ffmpeg", "ffmpeg.exe"):
        p = shutil.which(name)
        if p:
            return p

    # 2. 再看环境变量
    for var in ("FFMPEG_PATH", "FFMPEG_BIN", "FFMPEG_HOME"):
        v = os.environ.get(var)
        if v:
            for cand in (v, os.path.join(v, "ffmpeg.exe"), os.path.join(v, "bin", "ffmpeg.exe"),
                         os.path.join(v, "bin", "ffmpeg")):
                if os.path.isfile(cand):
                    return cand

    # 3. 常见安装位置（Windows）
    if sys.platform == "win32":
        import glob
        patterns = [
            r"C:\ffmpeg\bin\ffmpeg.exe",
            r"D:\ffmpeg\bin\ffmpeg.exe",
            r"D:\Tools\ffmpeg\*\bin\ffmpeg.exe",
            r"C:\Tools\ffmpeg\*\bin\ffmpeg.exe",
            r"D:\软件\ffmpeg\*\bin\ffmpeg.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*\*\bin\ffmpeg.exe"),
            os.path.expandvars(r"%USERPROFILE%\scoop\apps\ffmpeg\current\bin\ffmpeg.exe"),
            os.path.expandvars(r"%ProgramFiles%\ffmpeg\bin\ffmpeg.exe"),
        ]
        for pat in patterns:
            hits = sorted(glob.glob(pat))
            if hits:
                return hits[-1]
    else:
        for cand in ("/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg", "/usr/bin/ffmpeg"):
            if os.path.isfile(cand):
                return cand

    # 4. 实在找不到，给出分平台指引
    if sys.platform == "win32":
        hint = (
            "没有找到 ffmpeg。三选一：\n"
            "  ① 命令行装：  winget install Gyan.FFmpeg\n"
            "  ② 手动下：    https://www.gyan.dev/ffmpeg/builds/  下 essentials 版，\n"
            "     解压到 D:\\Tools\\ffmpeg\\ ，脚本会自动找到。\n"
            "  ③ 设环境变量： set FFMPEG_PATH=D:\\你的\\ffmpeg\\bin\\ffmpeg.exe\n"
            "装完验证：  ffmpeg -version"
        )
    elif sys.platform == "darwin":
        hint = (
            "没有找到 ffmpeg。装一下：\n"
            "  brew install ffmpeg\n"
            "装完验证：  ffmpeg -version"
        )
    else:
        hint = (
            "没有找到 ffmpeg。装一下：\n"
            "  sudo apt install ffmpeg      （Debian/Ubuntu）\n"
            "  sudo dnf install ffmpeg      （Fedora）\n"
            "装完验证：  ffmpeg -version"
        )
    sys.exit(hint)


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


def grab_frame(ffmpeg, video, at, out_png, crop=None, crop_scale=1.0):
    """在 at 秒处抓一帧（取偏后一点，避开转场的第一帧黑场）

    crop = (左, 上, 宽, 高) 且 crop_scale > 1 时，只截这块区域并放大，
    用于读屏幕、字幕、代码、表格上的小字。
    """
    vf = None
    if crop:
        l, t, w, h = crop
        parts = [f"crop={int(w)}:{int(h)}:{int(max(0, l))}:{int(max(0, t))}"]
        if crop_scale and abs(crop_scale - 1.0) > 0.01:
            parts.append(f"scale=iw*{crop_scale:.3f}:ih*{crop_scale:.3f}:flags=lanczos")
        vf = ",".join(parts)
    cmd = [
        ffmpeg, "-y", "-v", "error",
        "-ss", f"{at:.3f}", "-i", video,
    ]
    if vf:
        cmd += ["-vf", vf]
    cmd += ["-frames:v", "1", "-q:v", "2", out_png]
    subprocess.run(cmd, capture_output=True)


def probe_size(ffmpeg, video):
    """读画面宽高（读不出返回 (0,0)）"""
    out = subprocess.run([ffmpeg, "-hide_banner", "-i", video],
                         capture_output=True, text=True, errors="ignore")
    m = re.search(r",\s*(\d{2,5})x(\d{2,5})\s*[\[,]", out.stderr)
    if not m:
        m = re.search(r"(\d{2,5})x(\d{2,5})", out.stderr)
    if not m:
        return (0, 0)
    return (int(m.group(1)), int(m.group(2)))


def parse_time_list(spec):
    """把 "95,110,200" 或 "1:35,3:20" 解析成秒列表；非法项直接报错退出"""
    out = []
    for raw in str(spec).split(","):
        tok = raw.strip()
        if not tok:
            continue
        try:
            if ":" in tok:
                mm, ss = tok.split(":", 1)
                out.append(int(mm) * 60 + float(ss))
            else:
                out.append(float(tok))
        except ValueError:
            sys.exit(f"--at 里有看不懂的时间：{raw!r}（写成 95 或 1:35 都可以）")
    if not out:
        sys.exit("--at 没给有效时间点")
    return out


def parse_region(spec):
    """把 "0,0,960,540" 解析成裁剪区域；也接受 "x0,y0,x1,y1" 的右下角写法"""
    if not spec:
        return None
    parts = [p.strip() for p in str(spec).split(",") if p.strip()]
    if len(parts) != 4:
        sys.exit(f"--crop 要四个数：左,上,宽,高（或 左,上,右,下）。收到 {spec!r}")
    try:
        vals = [float(p) for p in parts]
    except ValueError:
        sys.exit(f"--crop 里有不是数字的：{spec!r}")
    a, b, c, d = vals
    if c > a and d > b and (c - a) > 1 and (d - b) > 1 and a >= 0 and b >= 0:
        return (a, b, c - a, d - b)     # 左下角-右下角写法
    return (a, b, c, d)


def find_busy_times(ffmpeg, video, duration, want=1, sample_step=2.0, scan_from=None, scan_to=None):
    """挑画面最"满"的时刻：文字/表格/代码教学类视频，画面信息量越大往往越是关键页。

    逐帧算一下边缘能量（粗略信息量），取峰值附近时刻。比固定间隔撒点更可能落在内容页上。
    """
    lo = 0.0 if scan_from is None else max(0.0, float(scan_from))
    hi = duration if scan_to is None else min(duration, float(scan_to))
    if hi <= lo:
        return []
    n = int(max(1, min(120, (hi - lo) / max(0.5, sample_step))))
    step = (hi - lo) / n
    cmd = [
        ffmpeg, "-hide_banner", "-i", video,
        "-vf", f"fps=1/{step:.4f},scale=320:-1,edgedetect=low=0.1:high=0.3",
        "-f", "null", "-",
    ]
    # ffmpeg 本身不给每个采样帧的信息量，改用信号统计近似：转成灰度再取标准差
    cmd = [
        ffmpeg, "-hide_banner", "-i", video,
        "-vf", f"fps=1/{step:.4f},scale=320:-1,format=gray",
        "-f", "rawvideo", "-",
    ]
    out = subprocess.run(cmd, capture_output=True)
    data = out.stdout
    if not data:
        return [(lo + hi) / 2.0]
    # rawvideo 帧长 = 宽*高，但 scale 后高度未知；用总长/帧数反推
    frame_bytes = len(data) // n if n else len(data)
    if frame_bytes <= 0:
        return [(lo + hi) / 2.0]
    scores = []
    for i in range(n):
        chunk = data[i * frame_bytes:(i + 1) * frame_bytes]
        if not chunk:
            continue
        # 信息量近似：字节方差（画面越复杂/字越多，方差越大）
        mean = sum(chunk) / len(chunk)
        var = sum((b - mean) ** 2 for b in chunk) / len(chunk)
        scores.append((var, lo + i * step + step * 0.5))
    if not scores:
        return [(lo + hi) / 2.0]
    scores.sort(reverse=True)
    picked = []
    min_gap = max(2.0, (hi - lo) / max(2, want * 3))
    for _score, t in scores:
        if all(abs(t - p) >= min_gap for p in picked):
            picked.append(t)
        if len(picked) >= want:
            break
    return sorted(picked)


def report_image_size(png):
    try:
        with Image.open(png) as im:
            return im.size
    except Exception:
        return (0, 0)



def fmt_time(sec):
    m, s = divmod(sec, 60)
    return f"{int(m):02d}:{s:04.1f}"


def make_sheet(frames, shots, out_jpg, cols=4, cell_w=480, pad=10, header=56, shot_lines=None,
               page_info=None, start_index=0):
    """拼宫格，每格上方烧「编号 + 起始时间 + 时长」，可选下方加台词
    page_info = (当前页, 总页数, 镜头总数) 时，标题会显示页码
    start_index = 本页第一个镜头在全片中的序号偏移（分页时保证编号连续）"""
    if not frames:
        sys.exit("没抓到任何帧")

    # 以第一帧的宽高比决定单元格高度
    with Image.open(frames[0]) as im0:
        ratio = im0.height / im0.width
    cell_h = int(cell_w * ratio)

    has_lines = bool(shot_lines) and any(shot_lines)
    label_h = 46 if not has_lines else 78          # 有台词就多留一行
    rows = (len(frames) + cols - 1) // cols
    width = cols * cell_w + (cols + 1) * pad
    height = header + rows * (cell_h + label_h) + (rows + 1) * pad

    sheet = Image.new("RGB", (width, height), (18, 18, 20))
    draw = ImageDraw.Draw(sheet)
    font_title = load_font(26)
    font_label = load_font(22)
    font_idx = load_font(34)
    font_line = load_font(21)

    title = "STORYBOARD  ·  shot / start / duration"
    if has_lines:
        title += "  /  dialogue"
    if page_info:
        cur, total_pages, total_shots = page_info
        title += f"   ·   page {cur}/{total_pages}   ·   {total_shots} shots total"
    draw.text((pad + 4, 14), title, fill=(150, 150, 155), font=font_title)

    for i, (png, (s, e)) in enumerate(zip(frames, shots)):
        r, c = divmod(i, cols)
        x = pad + c * (cell_w + pad)
        y = header + pad + r * (cell_h + label_h + pad)

        with Image.open(png) as im:
            im = im.convert("RGB")
            im = im.resize((cell_w, cell_h), Image.LANCZOS)
            sheet.paste(im, (x, y))

        # 大编号（左上角压在画面上）—— 分页时保持全片连续编号
        draw.rectangle([x, y, x + 62, y + 46], fill=(0, 0, 0))
        draw.text((x + 12, y + 4), f"{i + 1 + start_index:02d}", fill=(255, 200, 60), font=font_idx)

        # 时间码：指定时刻模式（起止相同）只报时刻，不装作有持续时长
        if abs(e - s) < 0.05:
            label = f"at {fmt_time(s)}   (指定时刻)"
        else:
            label = f"start {fmt_time(s)}   dur {e-s:.1f}s"
        draw.text((x + 4, y + cell_h + 6), label, fill=(210, 210, 215), font=font_label)

        # 台词
        if has_lines:
            segs = shot_lines[i] if i < len(shot_lines) else []
            if segs:
                text = " ".join(x2["text"] for x2 in segs).strip()
                if len(text) > 30:
                    text = text[:29] + "…"
                fill = (255, 214, 130)
            else:
                text = "（无台词）"
                fill = (110, 112, 120)
            draw.text((x + 4, y + cell_h + 36), text, fill=fill, font=font_line)

    sheet.save(out_jpg, quality=88)
    return width, height


def _log(msg):
    """进度输出。CLI 下打到 stdout；被 MCP 导入时会换成 stderr（否则污染协议流）"""
    print(msg)


def set_logger(fn):
    global _log
    _log = fn


def find_whisper_cpp():
    """
    找工具自带的 whisper.cpp（解压即用，零 Python 依赖）。
    返回 (whisper-cli 路径, 模型路径) 或 None。

    查找顺序：
      1. 环境变量 WHISPER_CPP_DIR
      2. 本脚本同目录下的 whisper-cpp/
      3. 常见安装位置
    """
    def probe(root):
        if not root or not os.path.isdir(root):
            return None
        # whisper-cli.exe 可能在根目录，也可能在 Release/ 子目录
        cli = None
        for rel in ("whisper-cli.exe", os.path.join("Release", "whisper-cli.exe"),
                    "whisper-cli", "main", "main.exe"):
            p = os.path.join(root, rel)
            if os.path.isfile(p):
                cli = p
                break
        if not cli:
            return None
        mdir = os.path.join(root, "models")
        model = None
        for name in ("ggml-base.bin", "ggml-small.bin", "ggml-tiny.bin",
                     "ggml-base.en.bin", "ggml-medium.bin"):
            p = os.path.join(mdir, name)
            if os.path.isfile(p):
                model = p
                break
        if not model:
            import glob as _g
            hits = sorted(_g.glob(os.path.join(mdir, "ggml-*.bin")))
            if hits:
                model = hits[0]
        if not model:
            return None
        return cli, model

    here = os.path.dirname(os.path.abspath(__file__))
    for root in (os.environ.get("WHISPER_CPP_DIR"),
                 os.path.join(here, "whisper-cpp"),
                 os.path.join(here, "tools", "whisper-cpp"),
                 r"D:\Tools\whisper-cpp"):
        r = probe(root)
        if r:
            return r
    return None


def detect_speech_ranges(ffmpeg, wav, noise_db=-32, min_sil=0.55, min_speech=0.35, merge_gap=1.2):
    """
    用 ffmpeg 的 silencedetect 找出「有声音」的区间。
    这是防止 whisper 时间戳漂移的关键：只把有声段喂给它，静音段不喂。

    返回 [(start_sec, end_sec), ...]
    """
    r = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", wav, "-af",
         f"silencedetect=noise={noise_db}dB:d={min_sil}", "-f", "null", "-"],
        capture_output=True, text=True, errors="ignore",
    )
    text = r.stderr or ""
    sil_starts = [float(x) for x in re.findall(r"silence_start:\s*([0-9.]+)", text)]
    sil_ends = [float(x) for x in re.findall(r"silence_end:\s*([0-9.]+)", text)]

    # 总时长
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", text)
    total = 0.0
    if m:
        total = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))

    # 由静音区间反推有声区间：
    # 语音 = 上一个「静音结束」到这一个「静音开始」之间
    events = sorted([(t, 0) for t in sil_starts] + [(t, 1) for t in sil_ends])
    speech = []
    cursor = 0.0
    for t, kind in events:
        if kind == 0:          # 静音开始 → cursor..t 是语音
            if t - cursor >= min_speech:
                speech.append((cursor, t))
            cursor = t
        else:                  # 静音结束 → 更新 cursor
            cursor = t
    if total - cursor >= min_speech:
        speech.append((cursor, total))
    if not sil_starts and total > 0:
        speech = [(0.0, total)]

    # 合并间隔很近的段，减少 whisper 启动次数
    merged = []
    for a, b in speech:
        if merged and a - merged[-1][1] < merge_gap:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    return merged


def transcribe_cpp(video, cli, model, ffmpeg, lang="zh", hint=None, out_dir=None, use_vad_slice=True):
    """
    用 whisper.cpp 转录。
    先把音频转成 16k 单声道 wav，再按「有声区间」切片逐段识别，时间加回偏移。

    为什么要切片：整段喂进去时，开头的静音会让 whisper 时间戳整体前移
    （实测 5 秒的安静开头，能把第一句从 5.7s 标到 0.0s）。
    """
    work = out_dir or tempfile.mkdtemp(prefix="sb_wcpp_")
    wav = os.path.join(work, "_audio16k.wav")
    try:
        r = subprocess.run(
            [ffmpeg, "-y", "-v", "error", "-i", video, "-ar", "16000", "-ac", "1",
             "-c:a", "pcm_s16le", wav],
            capture_output=True, text=True, errors="ignore", timeout=600,
        )
        if r.returncode != 0 or not os.path.isfile(wav):
            return None, "提取音频失败：" + (r.stderr or "")[-200:]

        # 切出有声区间
        ranges = detect_speech_ranges(ffmpeg, wav) if use_vad_slice else [(0.0, 0.0)]
        if not ranges:
            return [], None            # 整段没声音

        all_segs = []
        for idx, (a, b) in enumerate(ranges):
            piece = os.path.join(work, f"_piece_{idx}.wav")
            prefix = os.path.join(work, f"_wseg_{idx}")
            try:
                rr = subprocess.run(
                    [ffmpeg, "-y", "-v", "error", "-ss", f"{a:.3f}", "-to", f"{b:.3f}",
                     "-i", wav, "-c:a", "pcm_s16le", piece],
                    capture_output=True, text=True, errors="ignore", timeout=300,
                )
                if rr.returncode != 0 or not os.path.isfile(piece):
                    continue
                cmd = [cli, "-m", model, "-f", piece, "-l", lang, "-oj", "-of", prefix]
                if hint:
                    cmd += ["--prompt", hint]
                subprocess.run(cmd, capture_output=True, text=True, errors="ignore", timeout=1800)
                js = prefix + ".json"
                if not os.path.isfile(js):
                    continue
                import json as _json
                with open(js, encoding="utf-8") as f:
                    data = _json.load(f)
                for s in data.get("transcription", []):
                    t = (s.get("text") or "").strip()
                    if not t:
                        continue
                    off = s.get("offsets") or {}
                    st = off.get("from", 0) / 1000.0 + a
                    en = off.get("to", 0) / 1000.0 + a
                    all_segs.append({"start": round(st, 2), "end": round(en, 2), "text": t})
            finally:
                for p in (piece, prefix + ".json", prefix + ".txt", prefix + ".srt", prefix + ".vtt"):
                    try:
                        os.remove(p)
                    except OSError:
                        pass

        all_segs.sort(key=lambda x: x["start"])
        return all_segs, None
    except subprocess.TimeoutExpired:
        return None, "whisper.cpp 转录超时"
    except Exception as e:
        return None, f"whisper.cpp 出错：{e}"
    finally:
        try:
            os.remove(wav)
        except OSError:
            pass


def find_whisper_python():
    """
    找一个能 import whisper 的 Python 解释器。
    优先当前解释器，其次环境变量 WHISPER_PYTHON，最后常见安装位置。
    返回 None 表示没找到。
    """
    try:
        import whisper  # noqa: F401
        return sys.executable
    except ImportError:
        pass

    cands = []
    v = os.environ.get("WHISPER_PYTHON")
    if v:
        cands.append(v)
    if sys.platform == "win32":
        import glob
        cands += [
            os.path.expandvars(r"%USERPROFILE%\.local\bin\python.exe"),
            r"E:\CosyVoice\venv\Scripts\python.exe",
            r"D:\CosyVoice\venv\Scripts\python.exe",
        ]
        for pat in (r"C:\Users\*\AppData\Local\Programs\Python\Python3*\python.exe",):
            cands += sorted(glob.glob(pat), reverse=True)
    else:
        cands += ["/usr/local/bin/python3", "/usr/bin/python3"]

    probe = "import whisper" if True else ""
    for exe in cands:
        if not exe or not os.path.isfile(exe):
            continue
        try:
            r = subprocess.run([exe, "-c", "import whisper;print(whisper.__version__ if hasattr(whisper,'__version__') else 'ok')"],
                               capture_output=True, text=True, timeout=60)
            if r.returncode == 0:
                return exe
        except Exception:
            continue
    return None


_TRANSCRIBE_RUNNER = r'''
import json, sys, os

video = sys.argv[1]
model_name = sys.argv[2] if len(sys.argv) > 2 else "base"
lang = sys.argv[3] if len(sys.argv) > 3 else "zh"

import whisper
import torch

device = "cuda" if torch.cuda.is_available() else "cpu"
model = whisper.load_model(model_name, device=device)
result = model.transcribe(
    video,
    language=lang,
    fp16=(device == "cuda"),
    condition_on_previous_text=False,
    verbose=False,
)
segs = [{"start": round(s["start"], 2), "end": round(s["end"], 2), "text": s["text"].strip()}
        for s in result.get("segments", []) if s.get("text", "").strip()]
sys.stdout.write("@@JSON@@" + json.dumps(segs, ensure_ascii=False))
'''


def transcribe_video(video, python_exe, model="base", lang="zh", ffmpeg=None):
    """
    用 whisper 转录音频，返回 [{"start":秒, "end":秒, "text":"..."}]。
    找不到 whisper 时返回 (None, 原因)。
    """
    if not python_exe:
        return None, "没找到装 whisper 的 Python"

    # whisper 内部会调 ffmpeg，必须让它能找到
    env = dict(os.environ)
    if ffmpeg:
        fdir = os.path.dirname(os.path.abspath(ffmpeg))
        env["PATH"] = fdir + os.pathsep + env.get("PATH", "")

    fd, runner = tempfile.mkstemp(suffix=".py", prefix="sb_whisper_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(_TRANSCRIBE_RUNNER)
        r = subprocess.run(
            [python_exe, runner, video, model, lang],
            capture_output=True, text=True, errors="replace", env=env, timeout=3600,
        )
        if r.returncode != 0:
            tail = (r.stderr or "").strip().splitlines()[-3:]
            return None, "whisper 转录失败：" + " / ".join(tail)
        out = r.stdout or ""
        marker = out.find("@@JSON@@")
        if marker < 0:
            return None, "whisper 没返回结果"
        import json as _json
        return _json.loads(out[marker + len("@@JSON@@"):]), None
    except subprocess.TimeoutExpired:
        return None, "whisper 转录超时（视频太长）"
    except Exception as e:
        return None, f"whisper 转录出错：{e}"
    finally:
        try:
            os.remove(runner)
        except OSError:
            pass


def pick_frame_time(s, e, lines=None):
    """
    决定这一刻该抽哪一帧。
    有台词 → 抽「台词在本镜头内覆盖区间的中点」，这样抽到的是正在说话的画面。
    无台词 → 抽镜头内 35% 位置（和原来一致）。

    whisper 的句首时间常偏早 0.3~1 秒，所以不能直接用句首，用中点更稳。
    """
    if lines:
        best, best_len = None, 0.0
        for seg in lines:
            a = max(seg["start"], s)
            b = min(seg["end"], e)
            if b - a > best_len:
                best_len, best = b - a, (a, b)
        if best and best_len > 0.2:
            return (best[0] + best[1]) / 2.0
    return s + min(0.35, (e - s) * 0.35)


def split_lines_by_shot(segments, shots):
    """
    把台词按镜头归属，每句只归给「重叠时长最长」的那个镜头，避免跨边界重复。
    返回：[[seg, ...], ...]，与 shots 一一对应
    """
    if not segments:
        return [[] for _ in shots]
    per = [[] for _ in shots]
    for seg in segments:
        best_i, best_ov = None, 0.0
        for i, (s, e) in enumerate(shots):
            ov = min(seg["end"], e) - max(seg["start"], s)
            if ov > best_ov:
                best_ov, best_i = ov, i
        if best_i is not None:
            per[best_i].append(seg)
    return per


def clean_segments(segs, min_dur=0.15, max_repeat=3, duration=None):
    """
    清理 whisper 幻觉：
      - 丢掉过短的碎片
      - 丢掉超出视频长度的段（whisper 常在结尾凭空生成）
      - 相邻完全相同的文本合并
      - 同一句反复出现远超正常次数（BGM/无声段的典型幻觉）整句丢掉
    返回 (清理后的列表, 丢掉的条数)
    """
    if not segs:
        return [], 0
    raw_n = len(segs)

    merged = []
    for s in segs:
        t = (s.get("text") or "").strip()
        if not t or (s["end"] - s["start"]) < min_dur:
            continue
        if duration and s["start"] >= duration - 0.2:
            continue                       # 超出片长，必是幻觉
        # whisper 的典型幻觉：整句被括号包住、带「字幕:」字样、纯标点/音乐符号
        if re.fullmatch(r"[\(（\[【].*[\)）\]】]", t):
            continue
        if re.search(r"字幕[:：]|subtitle|字幕由|识别字幕", t, re.I):
            continue
        if re.fullmatch(r"[♪♫\s\.\,\!\?\-\—…·。，、！？]+", t):
            continue
        # 强特征：播放器/字幕组水印，出现即丢（不看长度）
        if re.search(r"独播剧场|獨播劇場|Exclusive|Television Series|字幕組|字幕组|"
                     r"本字幕|仅供学习|僅供學習|敬请期待|敬請期待", t, re.I):
            continue
        # 片尾/播放器字幕信息被当成歌词：「詞曲:XX」「作詞/作曲/編曲/演唱」这类
        # 判据：整句很短（<28 字）且命中这些制作信息词
        if len(t) <= 28 and re.search(
            r"詞曲|词曲|作詞|作词|作曲|編曲|编曲|演唱|原唱|混音|母带|母帶|录音|錄音|"
            r"翻译|翻譯|校对|校對|提供|Powered|Copyright",
            t, re.I,
        ):
            continue
        # 「曲:蝕雲 / 詞:蝕雲」「詞:蝕雲」这种制作人员列表：整句以「词/曲/编/唱:」开头
        if re.match(r"^\s*[詞词曲編编唱混录制監监]\s*[:：]", t):
            continue
        # 「曲:蝕雲 / 詞:蝕雲」这种用斜杠分隔的制作人员列表
        if len(t) <= 40 and len(re.findall(r"[詞词曲编編唱]\s*[:：]", t)) >= 1 and "/" in t:
            continue
        if merged and merged[-1]["text"] == t and s["start"] - merged[-1]["end"] < 0.3:
            merged[-1]["end"] = s["end"]
            continue
        merged.append({"start": s["start"], "end": s["end"], "text": t})

    from collections import Counter
    cnt = Counter(m["text"] for m in merged)
    out = [m for m in merged if cnt[m["text"]] <= max_repeat]
    return out, raw_n - len(out)


def find_ytdlp():
    """找 yt-dlp（用于下载网络视频链接）"""
    for name in ("yt-dlp", "yt-dlp.exe", "yt_dlp"):
        p = shutil.which(name)
        if p:
            return p
    if sys.platform == "win32":
        import glob
        cands = [
            os.path.expandvars(r"%USERPROFILE%\.local\bin\yt-dlp.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Python3*\Scripts\yt-dlp.exe"),
            os.path.expandvars(r"%APPDATA%\Python\Python3*\Scripts\yt-dlp.exe"),
        ]
        for pat in cands:
            hits = sorted(glob.glob(pat))
            if hits:
                return hits[-1]
    else:
        for cand in ("/usr/local/bin/yt-dlp", "/opt/homebrew/bin/yt-dlp", "/usr/bin/yt-dlp"):
            if os.path.isfile(cand):
                return cand
    return None


def is_url(s):
    s = str(s).strip()
    return s.lower().startswith(("http://", "https://", "www.", "bilibili.com", "b23.tv", "youtube.com", "youtu.be"))


def download_video(url, outdir, ffmpeg, max_height=1080):
    """
    用 yt-dlp 把网络视频下到本地临时目录，返回本地文件路径。
    支持 B 站、YouTube 等 yt-dlp 能处理的站点。
    """
    ytdlp = find_ytdlp()
    if not ytdlp:
        raise RuntimeError(
            "这是网络链接，需要 yt-dlp 才能下载。装一下：\n"
            "  pip install -U yt-dlp\n"
            "或者直接把视频下载到本地，然后传本地文件路径。"
        )

    ffdir = os.path.dirname(os.path.abspath(ffmpeg))
    tmpl = os.path.join(outdir, "_dl.%(ext)s")
    cmd = [
        ytdlp,
        "--no-playlist",
        "--no-warnings",
        "--ffmpeg-location", ffdir,
        "-f", f"bv*[height<={max_height}]+ba/b[height<={max_height}]/b",
        "--merge-output-format", "mp4",
        "-o", tmpl,
        url,
    ]
    _log(f"[下载] {url}")
    r = subprocess.run(cmd, capture_output=True, text=True, errors="ignore")
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-4:]
        hint = ""
        low = (r.stderr or "").lower()
        if "cookies" in low or "premium" in low or "login" in low:
            hint = (
                "\n提示：这条视频需要登录才能下。可以在命令后面加 cookies，"
                "例如命令行用 yt-dlp --cookies-from-browser chrome；"
                "或把视频下载到本地后再传文件路径。"
            )
        raise RuntimeError("下载失败：\n" + "\n".join(tail) + hint)

    cand = os.path.join(outdir, "_dl.mp4")
    if os.path.isfile(cand):
        return cand
    # 有些站点合出来是 mkv/webm
    for ext in (".mkv", ".webm", ".flv", ".mp4"):
        p = os.path.join(outdir, "_dl" + ext)
        if os.path.isfile(p):
            return p
    hits = [f for f in os.listdir(outdir) if f.startswith("_dl.")]
    if hits:
        return os.path.join(outdir, hits[0])
    raise RuntimeError("下载完成但找不到输出文件")


def main():
    ap = argparse.ArgumentParser(description="视频 → 镜头宫格图（给 AI 读）")
    ap.add_argument("video", help="输入视频路径，或网络视频链接（B站/YouTube 等）")
    ap.add_argument("-o", "--outdir", default=None, help="输出目录，默认与视频同目录")
    ap.add_argument("--threshold", type=float, default=0.3, help="场景检测灵敏度，默认 0.3")
    ap.add_argument("--cols", type=int, default=4, help="宫格列数，默认 4")
    ap.add_argument("--per-page", type=int, default=24,
                    help="每张宫格图最多放几个镜头，超出自动分成多张。默认 24")
    ap.add_argument("--max", type=int, default=0,
                    help="总共最多保留几个镜头（抽样）。0=不限制，长视频靠分页而不是丢弃。默认 0")
    ap.add_argument("--transcribe", choices=["auto", "on", "off"], default="auto",
                    help="识别台词：auto=有 whisper 就转，on=必须转，off=不转（默认 auto）")
    ap.add_argument("--whisper-model", default="base",
                    help="whisper 模型大小：tiny/base/small/medium，默认 base（越大越准越慢）")
    ap.add_argument("--hint", default=None,
                    help="给语音识别的提示词，写上视频里可能出现的专有名词，能显著提升准确率。"
                         "例：--hint \"DeepSeek, 剪映, 提示词\"")
    ap.add_argument("--at", default=None,
                    help="只抽这些时间点（秒或 分:秒），逗号分隔。例：--at 95,110,3:20\n"
                         "给了 --at 就不做场景检测，直接按你指定的时刻出图（教程/幻灯片/代码演示最有用）")
    ap.add_argument("--crop", default=None,
                    help="裁剪区域「左,上,宽,高」，也可写成「左,上,右,下」。配合 --at 读屏幕上小字。"
                         "例：--crop 300,200,900,500")
    ap.add_argument("--zoom", type=float, default=1.0,
                    help="裁剪后放大倍数，默认 1.0（配合 --crop 用，2 就是放两倍）")
    ap.add_argument("--auto-keys", type=int, default=0, metavar="N",
                    help="自动挑 N 个画面信息量最大的时刻（不用手填 --at）。"
                         "教学视频/ PPT 演示用它比均分九宫格准。默认 0=不用")
    ap.add_argument("--scan", default=None,
                    help="限制 --auto-keys 的搜索范围，写成「起,止」秒。例：--scan 180,400")
    args = ap.parse_args()

    ffmpeg = find_ffmpeg()

    # 网络链接：先下载到临时目录
    tmp_dl = None
    if is_url(args.video):
        tmp_dl = tempfile.mkdtemp(prefix="sb_dl_")
        try:
            video = download_video(args.video, tmp_dl, ffmpeg)
        except RuntimeError as e:
            sys.exit(str(e))
        stem = "网络视频"
    else:
        if not os.path.isfile(args.video):
            sys.exit(f"找不到视频：{args.video}")
        video = os.path.abspath(args.video)
        stem = os.path.splitext(os.path.basename(video))[0]

    outdir = os.path.abspath(args.outdir or (tmp_dl or os.path.dirname(video)))
    os.makedirs(outdir, exist_ok=True)

    print(f"[1/4] 读时长: {video}")
    duration = probe_duration(ffmpeg, video)
    if duration <= 0:
        sys.exit("读不出视频时长，文件可能损坏或不是视频。")
    print(f"      时长 {duration:.2f}s")

    # ---- 指定时间点模式：给了 --at 就跳过场景检测，直接按时刻出图 ----
    fixed_times = None
    if args.at:
        fixed_times = [t for t in parse_time_list(args.at) if 0 <= t <= duration]
        if not fixed_times:
            sys.exit(f"--at 给的时间都不在 0~{duration:.1f}s 范围内")
        bad = [t for t in parse_time_list(args.at) if not (0 <= t <= duration)]
        if bad:
            print(f"      忽略超出时长的时刻：{bad}")
        print(f"[2/4] 指定时间点模式：{len(fixed_times)} 个时刻，跳过场景检测")
        shots = [(t, t) for t in fixed_times]
        segments = None
        shot_lines = None
    elif args.auto_keys and args.auto_keys > 0:
        scan_from, scan_to = 0.0, duration
        if args.scan:
            try:
                a, b = [float(x) for x in str(args.scan).split(",")[:2]]
                scan_from, scan_to = a, b
            except Exception:
                sys.exit(f"--scan 要写成「起,止」秒，例 --scan 180,400。收到 {args.scan!r}")
        print(f"[2/4] 自动挑关键帧：扫描 {scan_from:.0f}s~{scan_to:.0f}s，要 {args.auto_keys} 个")
        fixed_times = find_busy_times(ffmpeg, video, duration, want=args.auto_keys,
                                      scan_from=scan_from, scan_to=scan_to)
        if not fixed_times:
            sys.exit("自动挑帧失败，改用 --at 指定时间点")
        print("      选中时刻：" + ", ".join(f"{t:.1f}s" for t in fixed_times))
        shots = [(t, t) for t in fixed_times]
        segments = None
        shot_lines = None
    else:
        print(f"[2/4] 场景检测 (threshold={args.threshold})")
        scenes = detect_scenes(ffmpeg, video, args.threshold)
        shots = build_shots(scenes, duration)
        print(f"      检测到 {len(scenes)} 个切换点 → {len(shots)} 个镜头")

        # 兜底：没有明显镜头切换的视频（连续动画、长镜头、幻灯片、AI 逐帧生成）
        # 场景检测会返回 1 个镜头，整段抽一帧＝抽到黑场，等于白干。
        # 这种情况改用等间隔抽帧，至少把整条片子采样出来。
        if len(shots) < 3 and duration >= 6:
            n = min(12, max(4, int(duration // 5)))
            step = duration / n
            shots = [(i * step, min((i + 1) * step, duration)) for i in range(n)]
            print(f"      无镜头切换，改用等间隔抽 {n} 帧（每 {step:.1f}s 一帧）")

        if args.max and args.max > 0 and len(shots) > args.max:
            # 只有用户显式要求总数上限时才抽样（会丢镜头）
            step = len(shots) / args.max
            shots = [shots[int(i * step)] for i in range(args.max)]
            print(f"      按 --max {args.max} 等间隔抽样（注意：抽样会丢镜头）")

    crop = parse_region(args.crop)
    zoom = max(1.0, float(args.zoom or 1.0))
    if crop and zoom == 1.0:
        zoom = 2.0      # 只给 --crop 不给 --zoom 时默认放大两倍，否则截出来更看不清
    if crop:
        vw, vh = probe_size(ffmpeg, video)
        l, t, w, h = crop
        if vw and vh:
            if l >= vw or t >= vh:
                sys.exit(f"--crop 起点超出画面（画面 {vw}x{vh}，你给的是 {args.crop}）")
            w = min(w, vw - l)
            h = min(h, vh - t)
            if w < 64 or h < 64:
                sys.exit(f"--crop 截出来只剩 {int(w)}x{int(h)} 像素，太小了读不出字。"
                         f"画面是 {vw}x{vh}，检查一下左上角坐标和宽高")
            crop = (l, t, w, h)
        print(f"      裁剪区域 {int(l)},{int(t)} {int(w)}x{int(h)}，放大 {zoom:g} 倍")


    # 台词（可选，按镜头切分）
    shot_lines = None
    segments = None
    if args.transcribe != "off":
        cpp = find_whisper_cpp()
        if cpp:
            print(f"[转录] whisper.cpp（{os.path.basename(cpp[1])}）...")
            segments, err = transcribe_cpp(video, cpp[0], cpp[1], ffmpeg, "zh", args.hint)
            if segments is not None:
                raw_n = len(segments)
                segments, dropped = clean_segments(segments, duration=duration)
                if dropped:
                    print(f"      过滤掉 {dropped} 条疑似幻觉/碎片（原 {raw_n} 条）")
            else:
                print(f"      跳过：{err}")
                if args.transcribe == "on":
                    sys.exit(err)
        else:
            wpy = find_whisper_python()
            if wpy:
                print(f"[转录] openai-whisper {args.whisper_model} ...（首次加载模型会慢一点）")
                segments, err = transcribe_video(video, wpy, args.whisper_model, "zh", ffmpeg)
                if segments is not None:
                    raw_n = len(segments)
                    segments, dropped = clean_segments(segments, duration=duration)
                    if dropped:
                        print(f"      过滤掉 {dropped} 条疑似幻觉/碎片（原 {raw_n} 条）")
                else:
                    print(f"      跳过：{err}")
                    if args.transcribe == "on":
                        sys.exit(err)
            else:
                msg = ("没有语音识别可用。两个办法：\n"
                       "  ① 用工具自带的 whisper.cpp：把 whisper-cpp 文件夹放到本目录，\n"
                       "     里面有 whisper-cli.exe 和 models/ggml-base.bin 就行\n"
                       "  ② 或者 pip install -U openai-whisper（体积大很多）")
                print(f"[转录] 跳过：{msg}")
                if args.transcribe == "on":
                    sys.exit(msg)

        if segments:
            shot_lines = split_lines_by_shot(segments, shots)
            got = sum(1 for l in shot_lines if l)
            print(f"      识别到 {len(segments)} 句台词，落在 {got}/{len(shots)} 个镜头里")

    print("[3/4] 抽帧")
    tmp = tempfile.mkdtemp(prefix="sb_")
    frames = []
    is_fixed = fixed_times is not None
    for i, (s, e) in enumerate(shots):
        png = os.path.join(tmp, f"f{i:03d}.png")
        if is_fixed:
            grab_frame(ffmpeg, video, s, png, crop=crop, crop_scale=zoom)
        else:
            grab_frame(ffmpeg, video, pick_frame_time(s, e, shot_lines[i] if shot_lines and i < len(shot_lines) else None), png,
                       crop=crop, crop_scale=zoom)
        if os.path.exists(png):
            frames.append(png)
        else:
            shots[i] = None
    triples = []
    for i, (f, sh) in enumerate(zip(frames, shots)):
        if sh:
            triples.append((f, sh, shot_lines[i] if shot_lines and i < len(shot_lines) else None))
    frames = [t[0] for t in triples]
    shots = [t[1] for t in triples]
    if shot_lines:
        shot_lines = [t[2] or [] for t in triples]

    # ---- 分页：长视频切成多张图，而不是丢掉镜头 ----
    per_page = max(1, args.per_page)
    total = len(frames)
    page_count = (total + per_page - 1) // per_page if total else 0
    print(f"[4/4] 拼宫格（{total} 个镜头 → {page_count} 张图，每张最多 {per_page} 格）")

    pages = []          # [(jpg路径, 宽, 高, 起始镜号, 结束镜号)]
    for pi in range(page_count):
        a = pi * per_page
        b = min(a + per_page, total)
        pf = frames[a:b]
        ps = shots[a:b]
        pl = shot_lines[a:b] if shot_lines else None
        if page_count == 1:
            name = f"{stem}_storyboard.jpg"
        else:
            name = f"{stem}_storyboard_p{pi+1}.jpg"
        jpg = os.path.join(outdir, name)
        w, h = make_sheet(pf, ps, jpg, cols=args.cols, shot_lines=pl, start_index=a,
                          page_info=(pi + 1, page_count, total) if page_count > 1 else None)
        pages.append((jpg, w, h, a + 1, b))

    data = {
        "video": video,
        "duration": round(duration, 3),
        "threshold": args.threshold,
        "shot_count": len(shots),
        "page_count": page_count,
        "per_page": per_page,
        "mode": "at" if args.at else ("auto-keys" if args.auto_keys else "scene"),
        "crop": list(crop) if crop else None,
        "zoom": zoom if crop else 1.0,
        "pages": [
            {"file": os.path.basename(j), "width": w, "height": h, "shots": f"{a}-{b}"}
            for (j, w, h, a, b) in pages
        ],
        "shots": [
            dict(
                {"no": i + 1, "start": round(s, 3), "end": round(e, 3), "duration": round(e - s, 3)},
                **({"lines": [x2["text"] for x2 in shot_lines[i]]} if shot_lines else {})
            )
            for i, (s, e) in enumerate(shots)
        ],
    }

    # ---- 指定时间点 / 自动挑帧：额外落一份单张高清图，方便逐张细读 ----
    saved_singles = []
    if is_fixed:
        sdir = os.path.join(outdir, f"{stem}_frames")
        os.makedirs(sdir, exist_ok=True)
        for i, (s, _e) in enumerate(shots):
            tag = f"{int(s // 3600):02d}h{int(s % 3600 // 60):02d}m{int(s % 60):02d}s"
            name = f"t{i + 1:02d}_{tag}.png"
            dst = os.path.join(sdir, name)
            png = os.path.join(tmp, f"f{i:03d}.png")
            if os.path.exists(png):
                try:
                    import shutil
                    shutil.copyfile(png, dst)
                    saved_singles.append({"file": os.path.relpath(dst, outdir),
                                          "at": round(s, 3),
                                          "width": report_image_size(dst)[0],
                                          "height": report_image_size(dst)[1]})
                except OSError:
                    pass
        if saved_singles:
            data["frames"] = saved_singles
    if segments:
        data["transcript"] = segments
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
    for (j, w, h, a, b) in pages:
        print(f"宫格图 : {j}  ({w}x{h})  镜头 {a}-{b}")
    if saved_singles:
        print(f"单张图 : {os.path.join(outdir, stem + '_frames')}  ({len(saved_singles)} 张，原尺寸)")
        for it in saved_singles:
            print(f"         {it['file']}   {it['at']}s   {it['width']}x{it['height']}")
    print(f"镜头表 : {js}")
    print(f"镜头数 : {len(shots)}")
    print()
    if page_count > 1:
        print("图比较多时，按 p1、p2… 顺序看完再下结论。")
    print("把宫格图交给 AI，它就能说出：有几个镜头、每个多长、怎么切、节奏怎么样。")


if __name__ == "__main__":
    main()
