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

        # 时间码
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


def transcribe_cpp(video, cli, model, ffmpeg, lang="zh", hint=None, out_dir=None):
    """用 whisper.cpp 转录。先把音频转成 16k 单声道 wav，再喂给 whisper-cli。"""
    work = out_dir or tempfile.mkdtemp(prefix="sb_wcpp_")
    wav = os.path.join(work, "_audio16k.wav")
    prefix = os.path.join(work, "_wcpp_out")
    try:
        r = subprocess.run(
            [ffmpeg, "-y", "-v", "error", "-i", video, "-ar", "16000", "-ac", "1",
             "-c:a", "pcm_s16le", wav],
            capture_output=True, text=True, errors="ignore", timeout=600,
        )
        if r.returncode != 0 or not os.path.isfile(wav):
            return None, "提取音频失败：" + (r.stderr or "")[-200:]

        cmd = [cli, "-m", model, "-f", wav, "-l", lang, "-oj", "-of", prefix]
        if hint:
            cmd += ["--prompt", hint]      # 引导专有名词
        r = subprocess.run(cmd, capture_output=True, text=True, errors="ignore", timeout=3600)
        js = prefix + ".json"
        if not os.path.isfile(js):
            tail = (r.stderr or "")[-300:]
            return None, "whisper.cpp 没产出结果：" + tail
        import json as _json
        with open(js, encoding="utf-8") as f:
            data = _json.load(f)
        segs = []
        for s in data.get("transcription", []):
            t = (s.get("text") or "").strip()
            if not t:
                continue
            off = s.get("offsets") or {}
            segs.append({
                "start": round(off.get("from", 0) / 1000.0, 2),
                "end": round(off.get("to", 0) / 1000.0, 2),
                "text": t,
            })
        return segs, None
    except subprocess.TimeoutExpired:
        return None, "whisper.cpp 转录超时"
    except Exception as e:
        return None, f"whisper.cpp 出错：{e}"
    finally:
        for p in (wav, prefix + ".json", prefix + ".txt", prefix + ".srt", prefix + ".vtt"):
            try:
                os.remove(p)
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

    print(f"[2/4] 场景检测 (threshold={args.threshold})")
    scenes = detect_scenes(ffmpeg, video, args.threshold)
    shots = build_shots(scenes, duration)
    print(f"      检测到 {len(scenes)} 个切换点 → {len(shots)} 个镜头")

    if args.max and args.max > 0 and len(shots) > args.max:
        # 只有用户显式要求总数上限时才抽样（会丢镜头）
        step = len(shots) / args.max
        shots = [shots[int(i * step)] for i in range(args.max)]
        print(f"      按 --max {args.max} 等间隔抽样（注意：抽样会丢镜头）")

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
    for i, (s, e) in enumerate(shots):
        png = os.path.join(tmp, f"f{i:03d}.png")
        grab_frame(ffmpeg, video, s + min(0.35, (e - s) * 0.35), png)
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
    print(f"镜头表 : {js}")
    print(f"镜头数 : {len(shots)}")
    print()
    if page_count > 1:
        print("图比较多时，按 p1、p2… 顺序看完再下结论。")
    print("把宫格图交给 AI，它就能说出：有几个镜头、每个多长、怎么切、节奏怎么样。")


if __name__ == "__main__":
    main()
