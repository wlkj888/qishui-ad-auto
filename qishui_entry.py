# -*- coding: utf-8 -*-
"""
汽水音乐「免费听30天」自动引导 + 达成检测（纯新增模块，不改原刷广告核心逻辑）

用户流程（真机实测 2026-11-20，1080x2340）：
  1. 启动汽水 → 播放页转发按钮旁有「免费听30天」标签（实测中心约 828,1896）
  2. 点该标签 → 弹出畅听弹窗「恭喜获得 第N日畅听 / 权益截止X月X日」，
     含「提醒我每天来」「看2个视频 享今日无损音质」，背景视频有「点击看更多视频」
  3. 点「看2个视频」或「点击看更多视频」→ 进入 ExcitingVideoActivity 广告流
     （顶部「N秒后可领取奖励」），由原脚本的核心循环处理看广告/领奖/关弹窗
  4. 广告结束返回到弹窗，若弹窗显示「第30日畅听」→ 视为领满30天 → 自动停止

本模块只提供两个函数：
  guide_into_ad_stream(app, G, live, dev_w, dev_h)  —— 启动后引导进入广告流
  check_30day_reached(boxes) -> Optional[str]        —— 检测"第N日畅听"且N>=30

所有操作都带 OCR 校验和次数上限，绝不点「立即下载/去下单」等落地页按钮。
"""
from __future__ import annotations

import re
import time
from typing import Optional

# 入口标签（播放页转发旁）
ENTRY_WORDS = ("免费听30天", "免费听", "畅听")
# 畅听弹窗特征
POPUP_WORDS = ("第", "日畅听", "权益截止", "恭喜获得", "提醒我每天来")
# 进入广告流的按钮（只点这些，绝不点下载/下单）
START_WORDS = ("看2个视频", "看视频", "点击看更多视频", "享今日无损音质", "继续看视频")
# 明确禁止点击的落地页词
FORBID_WORDS = ("立即下载", "下载", "去下单", "购买", "安装", "打开应用", "去应用市场")

# "第30日畅听" 解析
DAY_PAT = re.compile(r"第\s*(\d+)\s*日\s*畅听")


def _log(app, msg: str, level: str = "info") -> None:
    app.msg_q.put(("log", msg, level))


def _ocr(live, G):
    try:
        f = live.latest()
        if f is None:
            return [], None
        return G.run_ocr(f) or [], f
    except Exception:
        return [], None


def _find(boxes, words, exclude=()):
    for b in boxes or []:
        t = (b.text or "").strip()
        if not t or any(w in t for w in exclude):
            continue
        if any(w in t for w in words):
            return b
    return None


def _tap_box(app, G, b, frame, dev_w, dev_h, tag: str) -> None:
    sh, sw = frame.shape[:2]
    x, y = G.map_to_device(int(b.cx), int(b.cy), sw, sh, dev_w, dev_h)
    _log(app, f"[汽水引导] 点「{tag}」 {b.text!r} @({x},{y})", "click")
    G.tap(x, y)


def guide_into_ad_stream(app, G, live, dev_w: int, dev_h: int,
                         max_steps: int = 14, stop_at_day: int = 0) -> str:
    """
    启动后把汽水从播放页引导进广告视频流。
    返回：'already_30'=弹窗已显示领满 N 天(应停止) / 'entered'=已进广告流 / 'failed'=未进入(核心脚本继续)。
    stop_at_day>0 时，若在弹窗看到「第N日畅听」且 N>=stop_at_day，返回 'already_30'。
    只用 OCR 识别 + 白名单按钮，绝不点下载/下单落地页。
    """
    # 1) 确保汽水在前台
    try:
        focus = G.current_focus() or ""
    except Exception:
        focus = ""
    if "com.luna.music" not in focus:
        _log(app, "[汽水引导] 汽水不在前台，先拉起", "warn")
        G.run_adb(["shell", "monkey", "-p", "com.luna.music",
                   "-c", "android.intent.category.LAUNCHER", "1"], timeout=15)
        time.sleep(3.0)

    entered = False
    for step in range(max_steps):
        if getattr(app, "stop_event", None) is not None and app.stop_event.is_set():
            break
        boxes, frame = _ocr(live, G)
        if frame is None:
            time.sleep(0.6)
            continue
        texts = [(b.text or "").strip() for b in boxes]

        # 已经在广告流/奖励页，视为成功
        if any("可领取奖励" in t or "领取奖励" in t for t in texts) \
                or "ExcitingVideoActivity" in (G.current_focus() or ""):
            _log(app, "[汽水引导] 已进入广告流，交还核心脚本", "info")
            entered = True
            break

        # 弹窗已出现：若已领满 N 天则直接返回停止信号；否则点看视频入口
        if any("日畅听" in t for t in texts):
            hit = check_30day_reached(boxes, stop_at_day) if stop_at_day > 0 else None
            if hit:
                _log(app, f"[汽水引导] 弹窗显示「{hit}」——已领满 {stop_at_day} 天，直接停止", "click")
                return "already_30"
            start = _find(boxes, START_WORDS, exclude=FORBID_WORDS)
            if start is not None:
                _tap_box(app, G, start, frame, dev_w, dev_h, "看视频入口")
                time.sleep(2.5)
                continue
            time.sleep(0.8)
            continue

        # 播放页：点「免费听30天」标签打开弹窗
        entry = _find(boxes, ENTRY_WORDS, exclude=FORBID_WORDS)
        if entry is not None:
            _tap_box(app, G, entry, frame, dev_w, dev_h, "免费听30天标签")
            time.sleep(2.0)
            continue

        time.sleep(0.7)

    if not entered:
        _log(app, "[汽水引导] 未能自动进入广告流，核心脚本将继续在检测到入口时接管", "warn")
    return "entered" if entered else "failed"


def check_30day_reached(boxes, target_days: int = 30) -> Optional[str]:
    """
    检测 OCR 结果里是否出现「第N日畅听」且 N>=target_days。
    命中返回命中的原文，否则 None。仅匹配强特征"日畅听"，避免误判常驻标签。
    """
    for b in boxes or []:
        t = (b.text or "").strip()
        if "日畅听" not in t:
            continue
        m = DAY_PAT.search(t)
        if m:
            day = int(m.group(1))
            if day >= target_days:
                return t
        # 文案被 OCR 拆开时，出现"第30日"+"畅听"也算
        if str(target_days) in t:
            return t
    return None


def read_listen_days(boxes) -> Optional[int]:
    """从 OCR 结果读出「第N日畅听」的 N。读不到返回 None。"""
    for b in boxes or []:
        t = (b.text or "").strip()
        if "日畅听" not in t:
            continue
        m = DAY_PAT.search(t)
        if m:
            return int(m.group(1))
    return None


def return_and_check_days(app, G, live, dev_w: int, dev_h: int,
                          target_days: int = 30, max_steps: int = 12) -> tuple:
    """
    刷满一定条数广告后调用：从广告流返回播放页 → 点「免费听30天」→ 读弹窗天数。
    返回 (state, days)：
      state='reached'  已到/超过目标天数，应停止
      state='continue' 未到目标，已重新进入广告流，可继续刷
      state='unknown'  读不到天数，交还核心脚本继续，不误停
    days 为读到的天数（或 None）。
    绝不在广告里点下载/安装——本函数只按返回键和点白名单标签。
    """
    # 1) 退出广告流：连按返回，直到回到播放页（有"免费听"标签或 MainActivity）
    for _ in range(max_steps):
        if getattr(app, "stop_event", None) is not None and app.stop_event.is_set():
            return ("unknown", None)
        boxes, frame = _ocr(live, G)
        texts = [(b.text or "").strip() for b in boxes]
        focus = ""
        try:
            focus = G.current_focus() or ""
        except Exception:
            pass
        # 已在播放页（出现免费听标签，且不在广告 Activity）
        if "ExcitingVideoActivity" not in focus and any(
                any(w in t for w in ENTRY_WORDS) for t in texts):
            break
        # 广告页有"坚持退出 / 退出广告"就点它（跳过应用市场落地）
        exit_box = _find(boxes, ("坚持退出", "退出广告", "关闭广告"))
        if exit_box is not None and frame is not None:
            _tap_box(app, G, exit_box, frame, dev_w, dev_h, "退出广告")
        else:
            G.press_back()
        time.sleep(0.9)
    else:
        _log(app, "[汽水回查] 未能退回播放页，本轮回查跳过", "warn")
        return ("unknown", None)

    # 2) 点「免费听30天」标签打开畅听弹窗
    opened = False
    for _ in range(6):
        boxes, frame = _ocr(live, G)
        texts = [(b.text or "").strip() for b in boxes]
        if any("日畅听" in t for t in texts):
            opened = True
            break
        entry = _find(boxes, ENTRY_WORDS, exclude=FORBID_WORDS)
        if entry is not None and frame is not None:
            _tap_box(app, G, entry, frame, dev_w, dev_h, "免费听30天标签")
            time.sleep(1.8)
        else:
            time.sleep(0.7)
    if not opened:
        _log(app, "[汽水回查] 未读到畅听弹窗，本轮回查跳过", "warn")
        return ("unknown", None)

    # 3) 读天数判断
    boxes, frame = _ocr(live, G)
    days = read_listen_days(boxes)
    if days is None:
        # 文案可能被拆行：从所有文字里找"第NN日"
        joined = "".join((b.text or "").replace(" ", "") for b in (boxes or []))
        m = re.search(r"第(\d+)日", joined)
        if m:
            days = int(m.group(1))
    _log(app, f"[汽水回查] 弹窗显示第 {days} 日畅听（目标 {target_days} 日）", "info")
    if days is not None and days >= target_days:
        return ("reached", days)

    # 4) 未到目标：点「看视频」重新进广告流继续刷
    start = _find(boxes, START_WORDS, exclude=FORBID_WORDS)
    if start is not None and frame is not None:
        _tap_box(app, G, start, frame, dev_w, dev_h, "继续看视频")
        time.sleep(2.0)
        return ("continue", days)
    # 弹窗找不到入口就关掉它，交给核心脚本自己找入口
    G.press_back()
    time.sleep(0.8)
    return ("continue", days)


def detect_full_days_popup(boxes, target_days: int = 30) -> Optional[str]:
    """
    主循环每轮轻量检测：是否出现「第N日畅听」满天数弹窗（账号已满/领满）。
    命中返回命中的原文（如"第30日畅听"），否则 None。
    只认强特征"日畅听"+天数，不会误判播放页常驻的"免费听30天"标签。
    另外兼容"恭喜获得"+"权益截止"+"看N个视频享今日无损音质"这种领满弹窗。
    """
    # 强特征：第N日畅听 且 N>=目标
    hit = check_30day_reached(boxes, target_days)
    if hit:
        return hit
    texts = [(b.text or "").strip() for b in (boxes or [])]
    joined = "".join(t.replace(" ", "") for t in texts)
    # 领满弹窗特征组合：恭喜获得 + 权益截止 + 无损音质
    if "恭喜获得" in joined and "权益截止" in joined and "无损音质" in joined:
        # 再从 joined 里取天数
        import re as _re
        m = _re.search(r"第(\d+)日", joined)
        if m and int(m.group(1)) >= target_days:
            return f"第{m.group(1)}日畅听"
    return None


def detect_free_listen_entry(boxes) -> Optional[str]:
    """
    主循环轻量检测：播放页出现「免费听30天」入口标签 → 命中返回原文，否则 None。
    用户要求：识别到它就自动暂停（账号已领满，不再进广告、不再反复弹窗/重启App）。
    只认"免费听30天"完整短语，避免"免费听"泛词误判；不依赖 Activity 名。
    """
    for b in boxes or []:
        t = (b.text or "").replace(" ", "")
        if "免费听30天" in t:
            return (b.text or "").strip()
    return None
