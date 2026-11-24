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
import shutil
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
    per_page = int(args.get("per_page", 24))
    max_shots = int(args.get("max_shots", 0))
    outdir = args.get("outdir")
    at_times = args.get("at_times")
    crop_spec = args.get("crop")
    zoom = float(args.get("zoom", 1.0) or 1.0)
    auto_keys = int(args.get("auto_keys", 0) or 0)
    scan_spec = args.get("scan")

    # at_times 和 auto_keys 同时给会静默丢掉后者，这里明确提醒一下
    if at_times and auto_keys:
        log("同时给了 at_times 和 auto_keys，以 at_times 为准，auto_keys 被忽略")

    if not (0.0 < threshold < 1.0):
        raise ValueError("threshold 必须在 0 和 1 之间（建议 0.2~0.5）")
    if not (1 <= cols <= 8):
        raise ValueError("cols 必须在 1~8 之间")
    if not (1 <= per_page <= 100):
        raise ValueError("per_page 必须在 1~100 之间")
    if max_shots < 0:
        raise ValueError("max_shots 不能为负（0 表示不限制）")
    if zoom < 1.0:
        raise ValueError("zoom 不能小于 1（要缩小就别裁）")

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

    # ---- 指定时刻 / 自动挑帧：绕过场景检测，直接抽这几帧 ----
    fixed_times = None
    if at_times:
        if isinstance(at_times, str):
            fixed_times = sb.parse_time_list(at_times)
        else:
            fixed_times = [float(t) for t in at_times]
        fixed_times = [t for t in fixed_times if 0 <= t <= duration]
        if not fixed_times:
            raise ValueError(f"at_times 里的时刻都不在 0~{duration:.1f}s 范围内")
        shots = [(t, t) for t in fixed_times]
    elif auto_keys and auto_keys > 0:
        scan_from, scan_to = 0.0, duration
        if scan_spec:
            try:
                a, b = [float(x) for x in str(scan_spec).split(",")[:2]]
                scan_from, scan_to = a, b
            except Exception:
                raise ValueError("scan 要写成「起,止」秒，例 180,400")
        log(f"自动挑关键帧: {scan_from:.0f}s~{scan_to:.0f}s 要 {auto_keys} 个")
        picked = sb.find_busy_times(ffmpeg, video, duration, want=auto_keys,
                                    scan_from=scan_from, scan_to=scan_to)
        if not picked:
            raise ValueError("自动挑帧失败，请改用 at_times 指定时间点")
        fixed_times = picked
        shots = [(t, t) for t in picked]

    crop = sb.parse_region(crop_spec) if crop_spec else None
    if crop and zoom <= 1.0:
        zoom = 2.0
    if crop:
        vw, vh = sb.probe_size(ffmpeg, video)
        if vw and vh:
            l, t, w, h = crop
            if l >= vw or t >= vh:
                raise ValueError(f"crop 起点超出画面（画面 {vw}x{vh}）")
            w = min(w, vw - l)
            h = min(h, vh - t)
            if w < 64 or h < 64:
                raise ValueError(f"crop 截出来只剩 {int(w)}x{int(h)} 像素，太小了读不出字")
            crop = (l, t, w, h)

    truncated = False
    detected = len(shots)
    if fixed_times is None and max_shots and len(shots) > max_shots:
        step = len(shots) / max_shots
        shots = [shots[int(i * step)] for i in range(max_shots)]
        truncated = True

    tmp = tempfile.mkdtemp(prefix="sb_mcp_")
    frames, kept, kept_lines = [], [], []
    try:
        # 先转录（要在截断后的 shots 上切分台词）
        shot_lines = None
        segments = None
        transcribe = args.get("transcribe", True)
        if transcribe:
            cpp = sb.find_whisper_cpp()
            if cpp:
                log(f"转录中: {os.path.basename(cpp[1])}")
                segs, err = sb.transcribe_cpp(video, cpp[0], cpp[1], ffmpeg,
                                              "zh", args.get("hint"), tmp)
                if segs:
                    segs, dropped = sb.clean_segments(segs, duration=duration)
                    if dropped:
                        log(f"过滤 {dropped} 条疑似幻觉")
                    segments = segs
                    shot_lines = sb.split_lines_by_shot(segs, shots)
                else:
                    log(f"转录跳过: {err}")
            else:
                log("跳过台词：没找到 whisper.cpp（工具目录下要有 whisper-cpp/）")

        for i, (s, e) in enumerate(shots):
            png = os.path.join(tmp, f"f{i:03d}.png")
            if fixed_times is not None:
                sb.grab_frame(ffmpeg, video, s, png, crop=crop, crop_scale=zoom)
            else:
                sb.grab_frame(ffmpeg, video, sb.pick_frame_time(s, e, shot_lines[i] if shot_lines and i < len(shot_lines) else None), png,
                              crop=crop, crop_scale=zoom)
            if os.path.exists(png) and os.path.getsize(png) > 0:
                frames.append(png)
                kept.append((s, e))
                kept_lines.append(shot_lines[i] if shot_lines and i < len(shot_lines) else None)
        if not frames:
            raise ValueError("没抓到任何帧，视频可能是纯黑或无法解码")

        # 指定时刻模式：额外落一份单张高清图，方便逐张细读
        saved_singles = []
        if fixed_times is not None:
            sdir = os.path.join(outdir, f"{stem}_frames")
            os.makedirs(sdir, exist_ok=True)
            for i, (s, _e) in enumerate(kept):
                tag = f"{int(s // 3600):02d}h{int(s % 3600 // 60):02d}m{int(s % 60):02d}s"
                dst = os.path.join(sdir, f"t{i + 1:02d}_{tag}.png")
                try:
                    shutil.copyfile(frames[i], dst)
                    w0, h0 = sb.report_image_size(dst)
                    saved_singles.append({"file": os.path.relpath(dst, outdir).replace("\\", "/"),
                                          "at": round(s, 3), "width": w0, "height": h0})
                except OSError:
                    pass

        # 分页：不是「一页固定塞 N 格」，而是「一页的图不能大到被压缩」。
        # 图超过上限会被等比缩小，每格从 480 掉到两三百，字就废了 —— 宁可多分页。
        total = len(frames)
        if frames:
            w0, h0 = sb.report_image_size(frames[0])
            if w0 > 0:
                cell_h = int(sb.CELL_W * (h0 / w0))
                fit_cols, fit_rows = sb.fit_page(cols, cell_h, bool(segments), MAX_IMAGE_SIDE)
                cap = fit_cols * fit_rows
                if per_page > cap:
                    log(f"每页 {per_page} 格会超出 {MAX_IMAGE_SIDE}px 上限，自动改为 {cap} 格"
                        f"（{fit_cols} 列 × {fit_rows} 行）")
                    per_page = cap
                if cols > fit_cols:
                    log(f"{cols} 列会超出宽度上限，自动改为 {fit_cols} 列")
                    cols = fit_cols
        page_count = (total + per_page - 1) // per_page
        pages = []
        for pi in range(page_count):
            a = pi * per_page
            b = min(a + per_page, total)
            name = (f"{stem}_storyboard.jpg" if page_count == 1
                    else f"{stem}_storyboard_p{pi+1}.jpg")
            jpg = os.path.join(outdir, name)
            sb.make_sheet(frames[a:b], kept[a:b], jpg, cols=cols,
                          shot_lines=(kept_lines[a:b] if segments else None), start_index=a,
                          page_info=((pi + 1, page_count, total) if page_count > 1 else None))
            _shrink(jpg, MAX_IMAGE_SIDE)
            pages.append((jpg, a + 1, b))

        data = {
            "video": video,
            "duration": round(duration, 2),
            "threshold": threshold,
            "shot_count": len(kept),
            "page_count": page_count,
            "per_page": per_page,
            "mode": ("at" if args.get("at_times") else
                     ("auto-keys" if auto_keys else "scene")),
            "crop": list(crop) if crop else None,
            "zoom": (zoom if crop else 1.0),
            "truncated": truncated,
            "detected_before_limit": detected,
            "storyboard_image": pages[0][0] if pages else None,
            "pages": [{"file": os.path.basename(j), "shots": f"{a}-{b}"} for j, a, b in pages],
            "dialogue_source": ("whisper.cpp 语音识别（可能不准）" if segments else "无"),
            "shots": [],
        }
        if saved_singles:
            data["frames"] = saved_singles
            data["frames_dir"] = f"{stem}_frames"
        for i, (s, e) in enumerate(kept):
            item = {"no": i + 1, "start": round(s, 2), "end": round(e, 2), "duration": round(e - s, 2)}
            if segments and i < len(kept_lines) and kept_lines[i]:
                item["lines"] = [x["text"] for x in kept_lines[i]]
            data["shots"].append(item)
        if segments:
            data["transcript"] = segments
        js = os.path.join(outdir, f"{stem}_shots.json")
        with open(js, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        data["shots_json"] = js
        return data, pages
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
                shutil.rmtree(tmp_dl, ignore_errors=True)
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
        base = f"  #{s['no']:02d}  {s['start']:6.2f}s → {s['end']:6.2f}s  ({s['duration']:.2f}s)"
        if s.get("lines"):
            base += "  「" + " ".join(s["lines"]) + "」"
        lines.append(base)
    if d.get("dialogue_source"):
        lines += ["", f"台词来源：{d['dialogue_source']}"]
    lines += [
        "",
        f"宫格图：{d.get('page_count', 1)} 张，第一张 {d.get('pages', [{}])[0].get('file', '')}",
        f"镜头表：{d['shots_json']}",
        "",
        "判断节奏时可以看：镜头平均时长、有没有长镜头拖沓、切换是否规律。",
        "台词是语音识别，专有名词可能听错、短句可能漏，引用关键台词前最好对一下原片。",
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
                    "description": "总共最多保留几个镜头（抽样会丢镜头）。0 或不填 = 不限制，长视频靠分页。默认 0",
                    "default": 0,
                },
                "per_page": {
                    "type": "integer",
                    "description": "每张宫格图最多放几个镜头，超出自动分成多张图。默认 24",
                    "default": 24,
                },
                "max_images": {
                    "type": "integer",
                    "description": "本次最多返回几张图，默认 1（只给第一页，先判断值不值得继续读）。",
                    "default": 1,
                },
                "page": {
                    "type": "integer",
                    "description": (
                        "要第几页（从 1 开始），默认 1。"
                        "默认只返回第 1 页——先看全局再决定要不要后续页，"
                        "避免一次性把整条长视频的图都塞进上下文。"
                    ),
                    "default": 1,
                },
                "at_times": {
                    "type": "string",
                    "description": (
                        "只抽这些时间点，逗号分隔，单位秒或 分:秒，例如 \"95,110,3:20\"。"
                        "给了它就不做场景检测，直接按指定时刻出图，并额外落一份单张高清图。"
                        "适合教程、PPT、代码演示——读屏幕上的字比九宫格均分准得多。"
                    ),
                },
                "crop": {
                    "type": "string",
                    "description": (
                        "裁剪区域，四个数「左,上,宽,高」，也可写「左,上,右,下」。"
                        "配合 at_times 用，只截屏幕某一块再放大，专治小字看不清。"
                        "例：\"400,250,1200,500\""
                    ),
                },
                "zoom": {
                    "type": "number",
                    "description": "裁剪后放大倍数，默认 1。给了 crop 又没给 zoom 时自动按 2 倍放大。",
                    "default": 1,
                },
                "auto_keys": {
                    "type": "integer",
                    "description": (
                        "自动挑 N 个画面信息量最大的时刻，不用手填 at_times。"
                        "教学视频 / PPT 演示用它比九宫格均分更容易落在内容页上。默认 0=不用"
                    ),
                    "default": 0,
                },
                "scan": {
                    "type": "string",
                    "description": "限制 auto_keys 的搜索范围，写成「起,止」秒，例如 \"180,400\"。",
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
            data, pages = tool_make_storyboard(args)
            total_pages = data["page_count"]
            # 默认只给第 1 页：先看全局，判断这片子值不值得继续读。
            # 一次糊 3 张上去，等于替调用方把钱花掉了 —— 看不清的片子应该在这里就停。
            page = int(args.get("page", 1))
            if page < 1 or page > total_pages:
                return reply(msg_id, {
                    "content": [{"type": "text",
                                 "text": f"page 超范围：共 {total_pages} 页，你给的是 {page}"}],
                    "isError": True,
                })
            limit = int(args.get("max_images", 1))
            start = page - 1
            content = [{"type": "text", "text": _human_summary(data)}]
            shown = 0
            for idx in range(start, len(pages)):
                if shown >= limit:
                    break
                jpg, a, b = pages[idx]
                with open(jpg, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode()
                if total_pages > 1:
                    content.append({"type": "text",
                                    "text": f"第 {idx+1}/{total_pages} 张（镜头 {a}-{b}）"})
                content.append({"type": "image", "data": b64, "mimeType": "image/jpeg"})
                shown += 1
            if total_pages > page + shown - 1:
                content.append({"type": "text",
                                "text": (f"还有 {total_pages - (page + shown - 1)} 页没发。"
                                         f"\n先判断这一页够不够用：够就回答，别再要；"
                                         f"不够再传 page={page + shown} 取下一页。"
                                         f"\n如果这一页里字都糊、明显读不出，而片子又全是小字内容，"
                                         f"别硬要后续页——说明这片子不适合宫格，改用 at_times + crop，"
                                         f"或者直接告诉用户「这类视频要靠原图裁切」。")})
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
