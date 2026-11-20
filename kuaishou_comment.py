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

import base64
import random
import time
from typing import Optional

# ADBKeyboard（开源 Unicode 输入法）组件名；只在发评论瞬间临时启用，用完还原
ADB_IME = "com.android.adbkeyboard/.AdbIME"
ADB_IME_PKG = "com.android.adbkeyboard"

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

# 以下比例均在 1080x2340 真机上实测校准
COMMENT_BTN_RATIO = (0.915, 0.622)   # 右侧评论气泡 ≈ (988, 1455)
# 评论输入框大致在屏幕下部
INPUT_RATIO = (0.30, 0.966)          # ≈ (324, 2260)
# 发送按钮大致在输入框右下
SEND_RATIO = (0.88, 0.966)           # ≈ (950, 2260)

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


def _find_ocr(boxes, words, prefer_bottom: bool = False) -> Optional[object]:
    hits = [b for b in (boxes or [])
            if any(w in (b.text or "") for w in words)]
    if not hits:
        return None
    # 视频画面里常出现"发送/评论"等字样，评论区控件在最下方，取最靠下的命中
    return max(hits, key=lambda b: b.cy) if prefer_bottom else hits[0]


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
        #    若误进"回复给 XXX"状态，先按返回取消回复，确保发的是独立评论
        reply_hint = _find_ocr(boxes, ("回复给", "回复 @", "回复评论"))
        if reply_hint is not None:
            _log(app, "[评论] 检测到回复态，先取消", "warn")
            G.press_back()
            time.sleep(0.6)
            frame = live.latest()
            if frame is not None:
                try:
                    boxes = G.run_ocr(frame)
                except Exception:
                    boxes = []
        box_in = _find_ocr(boxes, ("说点什么", "写评论", "评论一句", "留下你的",
                                   "抢沙发", "暖评", "我先来", "評論"),
                           prefer_bottom=True)
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
        send = _find_ocr(boxes2, ("发送", "发布"), prefer_bottom=True)
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
        # 注意：快手评论区是盖在 HomeActivity 上的弹层，Activity 名仍是 homeactivity，
        # 不能用 Activity 名判断（会误以为已经在视频页而一次返回都不按 → 卡在评论区连发）。
        # 改用画面 OCR：看到评论区特征文字就按返回，直到特征消失。
        PANEL_WORDS = ("条评论", "说点什么", "写评论", "留下你的", "抢沙发",
                       "发表评论", "全部评论", "评论一句", "暖评")
        for _ in range(6):
            time.sleep(0.4)
            f = live.latest()
            in_panel = False
            if f is not None:
                try:
                    bxs = G.run_ocr(f) or []
                    in_panel = any(any(w in (b.text or "") for w in PANEL_WORDS)
                                   for b in bxs)
                except Exception:
                    in_panel = False
            focus = (G.current_focus() or "").lower()
            # 离开快手就别再按返回（否则会退回桌面），交给主循环拉起
            if "gifmaker" not in focus:
                _log(app, "[评论] 已离开快手，停止返回", "warn")
                break
            if not in_panel:
                break
            G.press_back()
        _log(app, "[评论] 已关闭评论区回到视频流", "info")
    return ok


def _input_text(G, text: str) -> None:
    """
    输入中文评论：临时切到 ADBKeyboard，用 base64 广播送入 Unicode 文本，
    随后立即还原用户原来的输入法；没有该输入法时才退回英文。
    """
    original_ime = ""
    switched = False
    try:
        packages = G.run_adb_text(["shell", "pm", "list", "packages", ADB_IME_PKG])
        if ADB_IME_PKG in packages:
            enabled = G.run_adb_text(["shell", "ime", "list", "-s"])
            if ADB_IME not in enabled:
                G.run_adb(["shell", "ime", "enable", ADB_IME])
                time.sleep(0.3)
                enabled = G.run_adb_text(["shell", "ime", "list", "-s"])
            if ADB_IME in enabled or "adbkeyboard" in enabled.lower():
                original_ime = G.run_adb_text(
                    ["shell", "settings", "get", "secure", "default_input_method"]
                )
                G.run_adb(["shell", "ime", "set", ADB_IME])
                switched = True
                time.sleep(0.3)
                # base64 通道：避免中文在 am 广播参数里被转义破坏
                b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
                G.run_adb(["shell", "am", "broadcast", "-a", "ADB_INPUT_B64",
                           "--es", "msg", b64])
                time.sleep(0.5)
                return
    except Exception:
        pass
    finally:
        if switched and original_ime and original_ime != ADB_IME:
            try:
                G.run_adb(["shell", "ime", "set", original_ime])
            except Exception:
                pass

    # 兜底：没有 Unicode 输入法时只能发 ASCII，避免 adb input text 直接报错
    fallback = "".join(ch for ch in text if ord(ch) < 128)
    if not fallback:
        fallback = "nice"
    try:
        G.run_adb(["shell", "input", "text", fallback.replace(" ", "%s")])
    except Exception:
        pass
