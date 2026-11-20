# -*- coding: utf-8 -*-
"""
快手随机自动评论模块

规则：
  - 由刷视频主流程在"刷了若干条后"不定时触发
  - 从常规语料里随机挑一条（不重复、不全部一样）
  - 只发友好/中性的短评论，不发敏感、引战、广告内容
  - 发完自动关闭评论区回到视频流

本模块为纯新增文件；是否启用由 GUI 的"随机评论"开关控制。
"""

from __future__ import annotations

import random
import time
from typing import Optional

# 常规、友好、多样的短评论语料（随机取，尽量不重复）
COMMENT_POOL = [
    "哈哈这个太真实了",
    "说得有道理",
    "支持一下",
    "有点意思",
    "学到了",
    "不错不错",
    "看了好几遍了",
    "这个我喜欢",
    "加油加油",
    "笑死我了",
    "有点东西啊",
    "收藏了慢慢看",
    "这个可以",
    "原来是这样",
    "太有才了",
    "说得对",
    "哈哈哈哈",
    "第一次见这种",
    "涨知识了",
    "路过点个赞",
    "这个必须转",
    "看哭了",
    "好有意思",
    "确实如此",
    "_mark_placeholder_",  # 占位，运行时过滤
]
COMMENT_POOL = [c for c in COMMENT_POOL if not c.startswith("_")]

# 评论按钮在右侧的比例位置（1080x2340 上的相对坐标）
COMMENT_BTN_RATIO = (0.915, 0.605)
# 评论输入框大致在屏幕下部
INPUT_RATIO = (0.45, 0.90)
# 发送按钮大致在右下
SEND_RATIO = (0.93, 0.90)

_last_comment = {"text": "", "history": []}


def _log(app, msg: str, level: str = "info") -> None:
    app.msg_q.put(("log", msg, level))


def pick_comment() -> str:
    """随机挑一条评论，避免和上次重复，尽量不在最近 8 条里重复。"""
    recent = _last_comment["history"][-8:]
    pool = [c for c in COMMENT_POOL if c != _last_comment["text"] and c not in recent]
    if not pool:
        pool = [c for c in COMMENT_POOL if c != _last_comment["text"]] or COMMENT_POOL
    text = random.choice(pool)
    _last_comment["text"] = text
    _last_comment["history"].append(text)
    if len(_last_comment["history"]) > 40:
        _last_comment["history"] = _last_comment["history"][-40:]
    return text


def _find_ocr(boxes, words) -> Optional[object]:
    for b in boxes or []:
        t = (b.text or "").strip()
        if any(w in t for w in words):
            return b
    return None


def post_comment(app, G, live, dev_w: int, dev_h: int) -> bool:
    """
    在当前作品下发一条随机评论。
    返回是否成功发出（尽力而为，失败就跳过，绝不卡住主流程）。
    """
    text = pick_comment()
    ok = False
    try:
        # 1) 点右侧评论气泡，打开评论区
        cx = int(dev_w * COMMENT_BTN_RATIO[0])
        cy = int(dev_h * COMMENT_BTN_RATIO[1])
        _log(app, f"[评论] 打开评论区，准备发「{text}」", "info")
        G.tap(cx, cy)
        time.sleep(1.2)

        frame = live.latest()
        boxes = []
        if frame is not None:
            try:
                boxes = G.run_ocr(frame)
            except Exception:
                pass

        # 2) 找输入框：识别"说点什么/评论"等提示，找不到就用比例坐标兜底
        box_in = _find_ocr(boxes, ("说点什么", "写评论", "评论一句", "留下你的", "抢沙发"))
        if box_in is not None:
            sh, sw = frame.shape[:2]
            ix, iy = G.map_to_device(int(box_in.cx), int(box_in.cy), sw, sh, dev_w, dev_h)
        else:
            ix = int(dev_w * INPUT_RATIO[0])
            iy = int(dev_h * INPUT_RATIO[1])
        G.tap(ix, iy)
        time.sleep(0.5)

        # 3) 输入评论文字（通过 adb 输入，兼容中文需要用剪贴板）
        _input_text(G, text)
        time.sleep(0.6)

        # 4) 找"发送"按钮，找不到就按回车
        frame2 = live.latest()
        boxes2 = []
        if frame2 is not None:
            try:
                boxes2 = G.run_ocr(frame2)
            except Exception:
                pass
        send = _find_ocr(boxes2, ("发送", "发布"))
        if send is not None:
            sh, sw = frame2.shape[:2]
            sx, sy = G.map_to_device(int(send.cx), int(send.cy), sw, sh, dev_w, dev_h)
            G.tap(sx, sy)
            ok = True
        else:
            G.run_adb(["shell", "input", "keyevent", "66"])  # ENTER
            ok = True
        time.sleep(0.8)
        _log(app, f"[评论] 已发送：{text}", "click")

    except Exception as e:
        _log(app, f"[评论] 失败: {type(e).__name__}: {e}，跳过", "warn")
    finally:
        # 5) 关闭评论区回到视频页
        for _ in range(3):
            f = G.current_focus() or ""
            act = f.lower()
            if "homeactivity" in act or "photodetailactivity" in act:
                break
            G.press_back()
            time.sleep(0.5)
    return ok


def _input_text(G, text: str) -> None:
    """通过 ADB 输入中文：先切换 Unicode IME（ADBKeyboard），失败则用 am broadcast 剪贴板。"""
    # 方案1：ADBKeyboard（安装后可用 adb input text 直接输入 Unicode）
    try:
        imes = G.run_adb_text(["shell", "ime", "list", "-s"])
        if "adbkeyboard" in imes.lower() or "unicode" in imes.lower():
            ime = next((l.strip() for l in imes.splitlines()
                        if "adbkeyboard" in l.lower() or "unicode" in l.lower()), "")
            G.run_adb(["shell", "ime", "set", ime])
            time.sleep(0.3)
            G.run_adb(["shell", "input", "text", text])
            return
    except Exception:
        pass

    # 方案2：clipper 剪贴板广播（装了 clipper app 时可用）
    try:
        safe = text.replace('"', '\\"')
        G.run_adb(["shell", "am", "broadcast", "-a", "clipper.set",
                   "-e", "text", safe])
        time.sleep(0.3)
        G.run_adb(["shell", "input", "keyevent", "279"])  # PASTE
        return
    except Exception:
        pass

    # 方案3：兜底——纯 ASCII 内容（英文/数字），input text 原生支持
    fallback = "".join(ch for ch in text if ord(ch) < 128)
    if not fallback:
        fallback = "nice"
    try:
        G.run_adb(["shell", "input", "text", fallback.replace(" ", "%s")])
    except Exception:
        pass
