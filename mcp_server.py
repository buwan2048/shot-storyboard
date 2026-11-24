#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mcp_server.py —— 把 storyboard 包装成 MCP 工具，让 AI 能自己调用。

用法（在 MCP 客户端里配置）：
{
  "mcpServers": {
    "storyboard": {
      "command": "python",
      "args": ["D:/path/to/mcp_server.py"]
    }
  }
}

提供给 AI 的工具：
  make_storyboard(video_path, threshold=0.3, cols=4, max_shots=24)
    → 返回宫格图（AI 直接看到）+ 镜头时间码表

零依赖：只用 Python 标准库 + ffmpeg + Pillow（和 storyboard.py 一样的依赖）。

重要：MCP 用 stdout 传输协议，本文件绝不能往 stdout print 任何东西。
所有日志走 stderr。
"""

import base64
import io
import json
import os
import sys
import tempfile

# 保证和 storyboard.py 同目录时能 import
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import storyboard as sb

SERVER_NAME = "storyboard"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2024-11-05"

MAX_IMAGE_SIDE = 2000      # 返回给 AI 的图长边上限，太大浪费 token


def log(msg):
    """所有日志走 stderr，绝不污染 stdout 的协议流"""
    print(f"[storyboard-mcp] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- 工具实现

def tool_make_storyboard(args):
    """执行一次抽帧宫格，返回 (数据字典, 图片路径)"""
    video = args.get("video_path") or args.get("video") or args.get("url")
    if not video:
        raise ValueError("缺少参数 video_path（本地路径或视频链接）")
    video = str(video).strip()

    threshold = float(args.get("threshold", 0.3))
    cols = int(args.get("cols", 4))
    max_shots = int(args.get("max_shots", args.get("max", 24)))
    outdir = args.get("outdir")

    if not (0.0 < threshold < 1.0):
        raise ValueError("threshold 必须在 0 和 1 之间（建议 0.2~0.5）")
    if not (1 <= cols <= 8):
        raise ValueError("cols 必须在 1~8 之间")
    if not (1 <= max_shots <= 100):
        raise ValueError("max_shots 必须在 1~100 之间")

    ffmpeg = sb.find_ffmpeg()

    tmp_dl = None
    if sb.is_url(video):
        # 网络链接（B站/YouTube 等）：先下载
        tmp_dl = tempfile.mkdtemp(prefix="sb_mcp_dl_")
        log(f"下载中: {video}")
        video = sb.download_video(video, tmp_dl, ffmpeg)
        stem = "网络视频"
        outdir = os.path.abspath(outdir) if outdir else os.path.join(tempfile.gettempdir(), "storyboard_out")
    else:
        video = os.path.abspath(video)
        if not os.path.isfile(video):
            raise ValueError(f"找不到视频文件：{video}")
        stem = os.path.splitext(os.path.basename(video))[0]
        outdir = os.path.abspath(outdir) if outdir else os.path.dirname(video)

    os.makedirs(outdir, exist_ok=True)

    duration = sb.probe_duration(ffmpeg, video)
    if duration <= 0:
        raise ValueError("读不出视频时长，文件可能损坏或不是视频")

    scenes = sb.detect_scenes(ffmpeg, video, threshold)
    shots = sb.build_shots(scenes, duration)

    truncated = False
    detected = len(shots)
    if len(shots) > max_shots:
        step = len(shots) / max_shots
        shots = [shots[int(i * step)] for i in range(max_shots)]
        truncated = True

    tmp = tempfile.mkdtemp(prefix="sb_mcp_")
    frames, kept = [], []
    try:
        for i, (s, e) in enumerate(shots):
            png = os.path.join(tmp, f"f{i:03d}.png")
            sb.grab_frame(ffmpeg, video, s + min(0.35, (e - s) * 0.35), png)
            if os.path.exists(png) and os.path.getsize(png) > 0:
                frames.append(png)
                kept.append((s, e))
        if not frames:
            raise ValueError("没抓到任何帧，视频可能是纯黑或无法解码")

        jpg = os.path.join(outdir, f"{stem}_storyboard.jpg")
        sb.make_sheet(frames, kept, jpg, cols=cols)

        # 压到 AI 友好尺寸，别把 300KB 的图原样塞进上下文
        _shrink(jpg, MAX_IMAGE_SIDE)

        data = {
            "video": video,
            "duration": round(duration, 2),
            "threshold": threshold,
            "shot_count": len(kept),
            "truncated": truncated,
            "detected_before_limit": detected,
            "storyboard_image": jpg,
            "shots": [
                {"no": i + 1, "start": round(s, 2), "end": round(e, 2), "duration": round(e - s, 2)}
                for i, (s, e) in enumerate(kept)
            ],
        }
        js = os.path.join(outdir, f"{stem}_shots.json")
        with open(js, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        data["shots_json"] = js
        return data, jpg
    finally:
        for p in frames:
            try:
                os.remove(p)
            except OSError:
                pass
        try:
            os.rmdir(tmp)
        except OSError:
            pass
        if tmp_dl:
            try:
                import shutil as _sh
                _sh.rmtree(tmp_dl, ignore_errors=True)
            except Exception:
                pass


def _shrink(jpg_path, max_side):
    try:
        from PIL import Image
        with Image.open(jpg_path) as im:
            w, h = im.size
            if max(w, h) <= max_side:
                return
            scale = max_side / max(w, h)
            im = im.convert("RGB").resize((int(w * scale), int(h * scale)), Image.LANCZOS)
            im.save(jpg_path, quality=85, optimize=True, progressive=True)
    except Exception as e:
        log(f"压缩图片失败（不影响结果）: {e}")


def _human_summary(d):
    lines = [
        f"已生成镜头宫格图。",
        f"视频时长 {d['duration']}s，共 {d['shot_count']} 个镜头"
        + ("（原片镜头更多，已等间隔抽样）" if d.get("truncated") else ""),
        "",
        "镜头表：",
    ]
    for s in d["shots"]:
        lines.append(f"  #{s['no']:02d}  {s['start']:6.2f}s → {s['end']:6.2f}s  ({s['duration']:.2f}s)")
    lines += [
        "",
        f"宫格图：{d['storyboard_image']}",
        f"镜头表：{d['shots_json']}",
        "",
        "判断节奏时可以看：镜头平均时长、有没有长镜头拖沓、切换是否规律。",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- MCP 协议

TOOLS = [
    {
        "name": "make_storyboard",
        "description": (
            "把一条视频压成一张镜头宫格图，让只能读文字的模型也能看懂画面结构。"
            "当用户让你分析/审阅/评价一条视频、或问片子怎么剪的时候，先用这个工具，"
            "拿到宫格图再回答，不要凭空猜视频内容。"
            "接受本地视频路径，也接受网络视频链接（B站/YouTube 等，会自动下载）。"
            "返回：宫格图（每格带镜头编号、起始时间码、时长）+ 精确镜头时间表。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "video_path": {
                    "type": "string",
                    "description": (
                        "视频来源。可以是本地文件绝对路径（如 D:/片子.mp4），"
                        "也可以是网络链接（如 https://www.bilibili.com/video/BV... 、"
                        "https://b23.tv/xxx 、YouTube 链接），链接会自动下载后抽帧。"
                    ),
                },
                "threshold": {
                    "type": "number",
                    "description": "场景检测灵敏度 0~1。默认 0.3。快切视频调低到 0.2，画面剧烈运动调高到 0.45",
                    "default": 0.3,
                },
                "cols": {
                    "type": "integer",
                    "description": "宫格列数，默认 4。镜头多于 16 个可用 5~6",
                    "default": 4,
                },
                "max_shots": {
                    "type": "integer",
                    "description": "最多保留几个镜头，默认 24，超出会等间隔抽样",
                    "default": 24,
                },
            },
            "required": ["video_path"],
        },
    }
]


def reply(msg_id, result=None, error=None):
    if error is not None:
        payload = {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32000, "message": str(error)}}
    else:
        payload = {"jsonrpc": "2.0", "id": msg_id, "result": result}
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(msg):
    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        return reply(msg_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })

    if method in ("notifications/initialized", "initialized"):
        return  # 通知，不回复

    if method == "ping":
        return reply(msg_id, {})

    if method == "tools/list":
        return reply(msg_id, {"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if name != "make_storyboard":
            return reply(msg_id, error=f"未知工具: {name}")
        try:
            data, jpg = tool_make_storyboard(args)
            with open(jpg, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            size_kb = os.path.getsize(jpg) // 1024
            content = [
                {"type": "text", "text": _human_summary(data)},
                {"type": "image", "data": b64, "mimeType": "image/jpeg"},
                {"type": "text", "text": f"（宫格图约 {size_kb} KB，{data['shot_count']} 格）"},
            ]
            return reply(msg_id, {"content": content, "isError": False})
        except Exception as e:
            log(f"tool error: {e}")
            return reply(msg_id, {
                "content": [{"type": "text", "text": f"抽帧失败：{e}"}],
                "isError": True,
            })

    # 未知方法：不回复比乱回复安全（有些客户端会因错误回复断连）
    log(f"未处理的方法: {method}")
    if msg_id is not None:
        reply(msg_id, error=f"不支持的方法: {method}")


def main():
    sb.fix_console_encoding()
    # MCP 规定用 UTF-8 传输，但 Windows 上 sys.stdin 默认按系统代码页（GBK）解码，
    # 中文路径会直接变乱码。这里强制 UTF-8。
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    # 关键：把 storyboard 的进度输出全部改到 stderr，绝不能碰 stdout（那是协议流）
    sb.set_logger(lambda m: log(m))
    log(f"启动，协议版本 {PROTOCOL_VERSION}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            log(f"收到非 JSON 行，忽略: {line[:80]}")
            continue
        try:
            handle(msg)
        except Exception as e:
            log(f"处理消息出错: {e}")
            if isinstance(msg, dict) and msg.get("id") is not None:
                reply(msg.get("id"), error=e)


if __name__ == "__main__":
    main()
