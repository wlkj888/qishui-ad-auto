# -*- coding: utf-8 -*-
"""
快手金币领取模块

流程：
  1. 从视频页点左上角红包 → 进入任务中心
  2. 循环处理：
     a. 关闭遮挡弹窗（"立即参与/我知道了/领取成功/×" 等）
     b. 点右下角大红包"点可领XXXX金币"领取
     c. 逐个点日常任务里处于"去领取"状态的按钮（只领现成的，不做"去观看/去浏览"任务）
     d. 列表滚到底没有可领的 → 结束
  3. 按返回回到视频页，交还给刷视频模块

只领取"现成可领"的金币，不刷任务、不看广告、不点外链，尽量降低风控。
本模块为纯新增文件，由 kuaishou_bot.py 的调度器在"领金币阶段"调用。
"""

from __future__ import annotations

import random
import time
from typing import Optional

import cv2

TASK_ACTIVITY_HINT = ("kds.krn", "task", "reward", "krn")   # 任务中心页
KS_PKG = "com.smile.gifmaker"

# 可"一键领取"的按钮文字（现成金币）；"去观看/去浏览/去逛"等做任务类一律不点
CLAIM_WORDS = ("去领取", "领取", "立即领取", "一键领取", "开红包", "拆红包")
# 领取成功 / 弹窗关闭类文字（短且明确，避免误点视频文案"知道了他们的壮举"这类长句）
DISMISS_WORDS = ("我知道了", "我知道了", "领取成功", "开心收下", "继续刷视频")
DISMISS_EXACT = {"知道了", "关闭", "跳过", "稍后", "下次再说", "确定", "收下", "返回", "我知道了"}
# 明确是"做任务"的按钮，遇到就跳过
TASK_ONLY_WORDS = ("去观看", "去浏览", "去完成", "去参与", "去搜索", "去下单", "去逛", "去查看")

# 右下角大红包在 1080x2340 上的大致中心（按比例换算）
BIG_RED_PACKET_RATIO = (0.88, 0.925)


def _log(app, msg: str, level: str = "info") -> None:
    app.msg_q.put(("log", msg, level))


def locate_packet(frame, G, dev_w: int, dev_h: int):
    """
    在视频页左上角用颜色检测定位红色红包图标，返回设备坐标 (x, y)。
    红包是左上角明显的红色块 + 黄色进度环；检测失败时回退到经验坐标。
    经验值来自真机实测：1080x2340 上红包中心约 (108, 482)。
    """
    try:
        import numpy as np
        sh, sw = frame.shape[:2]
        b, g, r = cv2.split(frame)
        # 红色主体：R 高、G/B 低
        mask = ((r > 165) & (g < 100) & (b < 100)).astype(np.uint8) * 255
        # 只在左上区域找：x<30%, y 在 8%~32%（避开顶部菜单和视频主体）
        x1, x2 = 0, int(sw * 0.30)
        y1, y2 = int(sh * 0.08), int(sh * 0.32)
        zone = mask[y1:y2, x1:x2]
        n, lab, stats, cent = cv2.connectedComponentsWithStats(zone)
        # 红包常被中间金色横条分成上下两块红色，取所有合格红色块的面积加权重心，
        # 落在两块之间才是红包中心（单取某一块会偏上或偏下点到边缘）。
        tot_a = 0.0
        sx = sy = 0.0
        for i in range(1, n):
            xx, yy, ww, hh, area = int(stats[i][0]), int(stats[i][1]), int(stats[i][2]), int(stats[i][3]), int(stats[i][4])
            if area < 40 or ww > sw * 0.22 or hh > sh * 0.16:
                continue
            cx, cy = cent[i]
            tot_a += area
            sx += (cx + x1) * area
            sy += (cy + y1) * area
        if tot_a > 0:
            mx, my = G.map_to_device(sx / tot_a, sy / tot_a, sw, sh, dev_w, dev_h)
            return int(mx), int(my), "color"
    except Exception:
        pass
    # 回退经验坐标（真机实测 1080x2340 上 (121,522) 可稳定进任务中心）
    return int(dev_w * 0.112), int(dev_h * 0.223), "fallback"


TASK_CENTER_OCR = ("任务中心", "我的金币", "日常任务", "金币商城", "我的现金")


def _in_task_center(focus: str, live=None, G=None) -> bool:
    """
    判断是否在任务中心。注意：真机任务中心是 HomeActivity 上的页面，
    Activity 名不含 task/krn 关键词，必须结合画面 OCR 判断，否则会误判"未进入"而退出。
    """
    # 画面 OCR 判断（最可靠）
    if live is not None and G is not None:
        f = live.latest()
        if f is not None:
            try:
                boxes = G.run_ocr(f) or []
                if any(any(kw in (b.text or "") for kw in TASK_CENTER_OCR) for b in boxes):
                    return True
            except Exception:
                pass
    # Activity 名判断（部分机型任务中心是独立 Activity）
    if KS_PKG not in (focus or ""):
        return False
    low = focus.lower()
    return any(h in low for h in TASK_ACTIVITY_HINT)


def _find_text(boxes, words, exclude=()) -> Optional[object]:
    """在 OCR 结果里找第一个命中 words、且不命中 exclude 的框。"""
    for b in boxes or []:
        t = (b.text or "").strip()
        if not t:
            continue
        if any(w in t for w in exclude):
            continue
        for w in words:
            if w in t:
                return b
    return None


def dismiss_popups(app, G, live, dev_w: int, dev_h: int, max_round: int = 6) -> int:
    """关闭任务中心里不断冒出的活动弹窗，返回关闭次数。"""
    closed = 0
    for _ in range(max_round):
        frame = live.latest()
        if frame is None:
            time.sleep(0.4)
            continue
        try:
            boxes = G.run_ocr(frame)
        except Exception:
            boxes = []
        sh, sw = frame.shape[:2]

        hit = _find_text(boxes, DISMISS_WORDS)
        # 短词必须整条完全相等才算（如"知道了"），长文案里的"知道了他们的壮举"不算
        if hit is not None and any(len(hit.text.strip()) > 8 and w in hit.text
                                   for w in DISMISS_WORDS):
            hit = None  # 命中来自长视频文案，忽略
        if hit is not None and (hit.text.strip() in DISMISS_EXACT or
                                any(w in hit.text for w in ("我知道了", "领取成功", "开心收下", "继续刷视频"))):
            x, y = G.map_to_device(int(hit.cx), int(hit.cy), sw, sh, dev_w, dev_h)
            _log(app, f"[金币] 关闭弹窗「{hit.text}」@({x},{y})", "info")
            G.tap(x, y)
            closed += 1
            time.sleep(0.8)
            continue
        break
    return closed


def claim_once(app, G, live, dev_w: int, dev_h: int) -> dict:
    """
    执行一次完整的金币领取流程。返回统计 dict。
    要求：调用时快手在视频页（HomeActivity）。
    """
    stat = {"popups": 0, "big_packet": 0, "task_claims": 0, "coins_hint": ""}
    try:
        # ---- 1. 点左上角红包进任务中心 ----
        focus = G.current_focus()
        if KS_PKG not in (focus or ""):
            _log(app, "[金币] 快手不在前台，先拉起", "warn")
            try:
                G.run_adb(["shell", "monkey", "-p", KS_PKG,
                           "-c", "android.intent.category.LAUNCHER", "1"], timeout=15)
            except Exception:
                pass
            time.sleep(2.0)

        frame = live.latest()
        if frame is None:
            _log(app, "[金币] 没有实时画面，跳过本轮领取", "warn")
            return stat
        # 颜色检测定位红包；失败则用真机实测经验坐标 (121,522)
        packet_x, packet_y, how = locate_packet(frame, G, dev_w, dev_h)
        candidates = [(packet_x, packet_y)]
        # 备选坐标：真机实测可进的 (121,522) 与红包上沿 (108,482)
        for rx, ry in ((int(dev_w * 0.112), int(dev_h * 0.223)),
                       (int(dev_w * 0.10), int(dev_h * 0.206))):
            if all(abs(rx - cx) > 6 or abs(ry - cy) > 6 for cx, cy in candidates):
                candidates.append((rx, ry))
        focus = ""
        for ci, (tx, ty) in enumerate(candidates):
            _log(app, f"[金币] 点击左上角红包 @({tx},{ty})[{how if ci == 0 else 'retry'}] 进入任务中心", "click")
            G.tap(tx, ty)
            time.sleep(2.0)
            focus = G.current_focus()
            if _in_task_center(focus, live, G):
                break
        if not _in_task_center(focus, live, G):
            _log(app, f"[金币] 未进入任务中心（前台 {focus[-60:]}），本轮跳过", "warn")
            G.press_back()
            time.sleep(0.6)
            return stat
        _log(app, "[金币] 已进入任务中心", "info")

        # ---- 2. 关弹窗 ----
        stat["popups"] = dismiss_popups(app, G, live, dev_w, dev_h)

        # ---- 3. 循环领取（右下角大红包 + 去领取按钮）----
        no_progress = 0
        last_hint = ""       # 上一次领的金币文案，避免同一红包反复点
        big_taps = 0         # 大红包点击次数上限
        fallback_done = False  # 兜底点击只做一次
        for round_i in range(12):
            frame = live.latest()
            if frame is None:
                time.sleep(0.4)
                continue
            sh, sh_w = frame.shape[:2]
            sw = sh_w
            try:
                boxes = G.run_ocr(frame)
            except Exception:
                boxes = []

            # 每轮先关弹窗
            if dismiss_popups(app, G, live, dev_w, dev_h, max_round=3):
                stat["popups"] += 1
                time.sleep(0.5)
                continue

            acted = False
            # 3a. 右下角大红包"点可领XXXX金币"（文案变了才点，最多 3 次）
            big_hint = _find_text(boxes, ("可领", "点可领", "开红包", "拆红包"))
            hint_text = big_hint.text.strip() if big_hint is not None else ""
            if big_hint is not None and hint_text != last_hint and big_taps < 3:
                bx, by = G.map_to_device(int(big_hint.cx), int(big_hint.cy), sw, sh, dev_w, dev_h)
                _log(app, f"[金币] 点大红包「{big_hint.text}」@({bx},{by})", "click")
                G.tap(bx, by)
                stat["big_packet"] += 1
                stat["coins_hint"] = big_hint.text
                last_hint = hint_text
                big_taps += 1
                acted = True
                time.sleep(1.2)
                dismiss_popups(app, G, live, dev_w, dev_h, max_round=3)

            # 3b. 日常任务里的"去领取"（排除做任务类）
            claim_box = _find_text(boxes, CLAIM_WORDS, exclude=TASK_ONLY_WORDS)
            if claim_box is not None:
                cx, cy = G.map_to_device(int(claim_box.cx), int(claim_box.cy), sw, sh, dev_w, dev_h)
                key = (int(claim_box.cx), int(claim_box.cy))
                if not hasattr(claim_once, "_clicked"):
                    claim_once._clicked = set()
                if key not in claim_once._clicked:
                    _log(app, f"[金币] 点任务「{claim_box.text}」@({cx},{cy})", "click")
                    G.tap(cx, cy)
                    claim_once._clicked.add(key)
                    stat["task_claims"] += 1
                    acted = True
                    time.sleep(1.0)
                    dismiss_popups(app, G, live, dev_w, dev_h, max_round=3)

            if acted:
                no_progress = 0
            else:
                no_progress += 1
            if no_progress >= 2:
                break

        _log(app, f"[金币] 本轮领取完成：弹窗{stat['popups']} 红包{stat['big_packet']} "
                  f"任务{stat['task_claims']}", "info")

    except Exception as e:
        _log(app, f"[金币] 领取异常: {type(e).__name__}: {e}", "err")
    finally:
        # ---- 4. 返回视频页（任务可能跳到搜索/商城/落地页，一路退回快手首页）----
        def at_video_page(f: str) -> bool:
            if KS_PKG not in (f or ""):
                return False
            act = f.lower()
            # 视频流首页或作品详情页算到位
            return ("homeactivity" in act or "photodetailactivity" in act
                    or "mainactivity" in act)

        for _ in range(8):
            focus = G.current_focus()
            if at_video_page(focus):
                break
            G.press_back()
            time.sleep(0.6)
        focus = G.current_focus()
        if KS_PKG not in (focus or ""):
            # 退出了快手就重新拉起
            try:
                G.run_adb(["shell", "monkey", "-p", KS_PKG,
                           "-c", "android.intent.category.LAUNCHER", "1"], timeout=15)
            except Exception:
                pass
            time.sleep(2.0)
        _log(app, f"[金币] 已返回快手，前台: {(G.current_focus() or '')[-50:]}", "info")
    return stat
