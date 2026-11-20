# -*- coding: utf-8 -*-
"""
快手自动刷作品模块（双模式附加组件）

规则：
  1. 每条作品"播完再刷"：检测画面停止（红包进度停）/循环回到开头才上滑，没播完就一直等；
     绝不按时间提前刷走，只有异常卡死 5 分钟才兜底
  2. 刷到直播：按返回 / 上滑快速跳过，不观看、不互动
  3. 只做上滑手势，不点赞、不评论、不关注、不点登录
  4. 与汽水音乐去广告模式共用 ADB / OCR / LiveScreen 基础设施

本模块是纯新增文件，不修改汽水模式的任何原有逻辑；
由 qishui_ad_gui.py 的"模式"下拉框分流调用。

调用方式：
    kuaishou_bot.run(app, G, watch_seconds)
    app: qishui_ad_gui.App 实例（提供 msg_q / stop_event / frame_q / rounds / clicks）
    G  : sys.modules[__name__]（运行中的 GUI 模块，保证 ADB/OCR 全局状态为同一份）
"""

from __future__ import annotations

import random
import threading
import time
from typing import Optional

import cv2

# ---------- 直播识别 ----------
# 直播间 Activity 名关键词（取 focus 中 "/" 之后的 Activity 部分匹配）
KS_LIVE_ACTIVITY = ("live", "liveroom", "liveplay", "liveshow")
# 直播间画面强特征文字（弱词如"主播"容易误判成普通作品文案，故不收）
KS_LIVE_TEXT = ("正在直播", "直播中", "进入直播间", "来看直播", "开播中", "直播间", "LIVE")

# ---------- 默认参数 ----------
DEFAULT_WATCH = 25.0      # 兼容参数：GUI 里"每条停留"输入框的值（现在仅作参考，不再按时间刷走）
MIN_WATCH = 2.0           # 播完判定生效前最少观看秒数（防止刚切换转场时误判）
LOOP_DIFF_THRESH = 13.0   # 首尾帧平均灰度差小于此值 → 认为循环回到开头（看完了）
STATIC_DIFF_THRESH = 2.2  # 相邻帧平均灰度差小于此值 → 画面静止（作品播完停在末帧）
STATIC_SAMPLES = 10       # 连续静止采样次数（约 0.25s/次 → 约 2.5 秒静止算播完）
RP_STABLE_N = 6           # 红包进度环连续 N 次不增长（约 6 次轮询）判定进度停=看完
HARD_TIMEOUT = 300        # 异常卡死兜底：单条 5 分钟还没检测到播完才强制下一条（正常不会触发）
KS_PKG = "com.smile.gifmaker"


def _log(app, msg: str, level: str = "info") -> None:
    app.msg_q.put(("log", msg, level))


def _is_live_focus(focus: str) -> bool:
    """根据前台 Activity 名判断是否直播间。"""
    if not focus:
        return False
    act = focus.lower().split("/")[-1] if "/" in focus else focus.lower()
    return any(k in act for k in KS_LIVE_ACTIVITY)


def _is_live_text(boxes) -> Optional[str]:
    """根据 OCR 文字判断是否直播间，命中则返回关键词。"""
    for b in boxes or []:
        text = (b.text or "").strip()
        if not text:
            continue
        for kw in KS_LIVE_TEXT:
            if kw.lower() in text.lower():
                return kw
    return None


def _thumb(frame):
    """取画面中部区域的 24x24 灰度缩略图，避开静止的顶部状态栏与底部文案栏。"""
    h, w = frame.shape[:2]
    y1, y2 = int(h * 0.16), int(h * 0.80)
    x1, x2 = int(w * 0.08), int(w * 0.92)
    roi = frame[y1:y2, x1:x2]
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    return cv2.resize(gray, (24, 24), interpolation=cv2.INTER_AREA).astype("float32")


def _diff(a, b) -> float:
    return float(abs(a - b).mean())


def red_packet_progress(frame) -> Optional[float]:
    """
    检测左上角红包上方的黄色进度环，返回进度 0.0~1.0；检测不到返回 None。

    实测规律（1080x2340）：红包约在 (88,516)-(156,563)，进度环是红包上方的一段
    金黄色弧。观看时黄像素从约 90 逐步增长到约 459，播满后稳定不再增长。
    用"当前黄像素 / 满环像素(≈460)"估算进度。传入帧分辨率任意，按比例换算。
    """
    if frame is None:
        return None
    h, w = frame.shape[:2]
    b, g, r = cv2.split(frame)
    # 红色红包本体：在左上 40%宽 x 45%高内找红色连通块
    m = ((r > 170) & (g < 120) & (b < 120)).astype("uint8") * 255
    sub = m[0:int(h * 0.45), 0:int(w * 0.35)]
    n, lab, stats, cent = cv2.connectedComponentsWithStats(sub)
    best = None
    for j in range(1, n):
        x, y, ww, hh, area = stats[j]
        if area > 600 and ww > 25 and 20 < hh < 90:
            best = (x, y, ww, hh)
            break
    if best is None:
        return None
    x, y, ww, hh = best
    # 红包上方区域的金黄色像素（进度环）
    yellow = ((r > 190) & (g > 140) & (g < 235) & (b < 130)).astype("uint8")
    y0 = max(0, y - 40)
    y1 = min(h, y + 10)
    x0 = max(0, x - 30)
    x1 = min(w, x + ww + 30)
    cnt = int(yellow[y0:y1, x0:x1].sum())
    if cnt <= 0:
        return None
    # 满环约 459 像素（1080 宽基准）；按宽度缩放
    full = 459.0 * (w / 1080.0)
    return min(1.0, cnt / full)


def _swipe_next(G, dev_w: int, dev_h: int) -> tuple:
    """上滑切换到下一条作品，带随机起点与时长，模拟真人手势。"""
    x = int(dev_w * 0.5) + random.randint(-24, 24)
    y1 = int(dev_h * 0.78) + random.randint(-30, 30)
    y2 = int(dev_h * 0.22) + random.randint(-30, 30)
    dur = random.randint(220, 380)
    G.run_adb(["shell", "input", "swipe", str(x), str(y1), str(x), str(y2), str(dur)])
    return x, y1, x, y2, dur


def _launch(G) -> None:
    try:
        G.run_adb(["shell", "monkey", "-p", KS_PKG,
                   "-c", "android.intent.category.LAUNCHER", "1"], timeout=15)
    except Exception:
        pass


def run(app, G, watch_seconds: float = DEFAULT_WATCH,
        watch_phase_min: float = 0.0, claim_coins: bool = False,
        rest_phase_min: float = 0.0, enable_comment: bool = False) -> None:
    """
    快手模式主循环（在 GUI 的 worker 线程中运行）。

    app: App 实例；G: 运行中的 GUI 模块（sys.modules[__name__]）
    watch_seconds   : 兼容旧参数（现在按红包进度判定，不再按时间刷走）
    watch_phase_min : 刷视频阶段时长（分钟），到点去领金币；0=不定时
    claim_coins     : 是否启用"领金币阶段"
    rest_phase_min  : 领完金币后休息时长（分钟），到点回来继续刷
    enable_comment  : 是否启用随机自动评论
    """
    import kuaishou_coin
    live = None
    stop = app.stop_event
    try:
        watch = float(watch_seconds or DEFAULT_WATCH)
        watch = max(6.0, min(watch, 180.0))
        schedule_on = claim_coins and watch_phase_min > 0
        sched = {
            "phase": "watch",                 # watch / claim / rest
            "phase_end": time.time() + watch_phase_min * 60 if schedule_on else 0,
            "coin_rounds": 0,
        }
        _log(app, "快手模式启动：每条作品播完（画面停止/红包进度停）才上滑下一条，没播完就一直等；直播自动跳过", "info")
        if schedule_on:
            _log(app, f"定时循环已开启：刷 {watch_phase_min:.0f} 分钟 → 领金币 → "
                      f"休息 {rest_phase_min:.0f} 分钟 → 继续刷", "info")

        # ---- 设备 ----
        out = G.run_adb_text(["devices"])
        lines = [ln.strip() for ln in out.splitlines()[1:] if ln.strip()]
        ready = [ln.split()[0] for ln in lines if ln.endswith("device")]
        if not ready:
            host = G.load_wifi_ip()
            if host:
                _log(app, f"未发现设备，尝试连接记忆中的 {host}", "warn")
                G.run_adb_host(["connect", host], timeout=12)
                time.sleep(1.5)
                out = G.run_adb_text(["devices"])
                ready = [ln.split()[0] for ln in out.splitlines()[1:] if ln.strip() and ln.endswith("device")]
        if not ready:
            _log(app, "未检测到已授权设备，请开启 USB 调试 / 连上 WiFi ADB", "err")
            app.msg_q.put(("stopped",))
            return

        serial = getattr(G, "DEVICE_SERIAL", None)
        # 自动挑选装了快手的设备：GUI 选中的优先，否则逐台探测 com.smile.gifmaker
        def has_kuaishou(sn: str) -> bool:
            try:
                import subprocess
                r = subprocess.run([G.ADB, "-s", sn, "shell", "pm", "list", "packages", KS_PKG],
                                   capture_output=True, timeout=8)
                out = (r.stdout or b"").decode("utf-8", errors="replace")
                return KS_PKG in out
            except Exception:
                return False

        chosen = serial if (serial in ready and has_kuaishou(serial)) else None
        if chosen is None:
            for sn in ready:
                if has_kuaishou(sn):
                    chosen = sn
                    break
        if chosen is not None and chosen != serial:
            _log(app, f"自动切换到装有快手的设备 {chosen}", "warn")
        serial = chosen or (serial if serial in ready else ready[0])
        G.DEVICE_SERIAL = serial
        _log(app, f"目标设备 {serial}", "info")

        dev_w, dev_h = G.get_device_size()
        G.SCREEN_W, G.SCREEN_H = dev_w, dev_h
        scale = 0.5 if dev_w >= 1000 else 1.0
        stream_w = int(dev_w * scale) // 2 * 2
        stream_h = int(dev_h * scale) // 2 * 2
        _log(app, f"真机分辨率 {dev_w}x{dev_h} · 影像流 {stream_w}x{stream_h}", "info")

        # ---- OCR / 影像 ----
        _log(app, "加载 OCR 模型…", "info")
        G.get_ocr()
        try:
            G.run_adb_text(["shell", "svc", "power", "stayon", "true"])
        except Exception:
            pass

        live = G.LiveScreen(
            G.ADB, serial, size=f"{stream_w}x{stream_h}",
            on_log=lambda s: app.msg_q.put(("log", s, "info")),
            screen_off=False,
        )
        live.start()

        def _preview_loop():
            last_seq = -1
            while not stop.is_set():
                try:
                    seq = live.latest_seq()
                    if seq != last_seq:
                        frame = live.latest()
                        if frame is not None:
                            if app.frame_q.full():
                                try:
                                    app.frame_q.get_nowait()
                                except Exception:
                                    pass
                            try:
                                app.frame_q.put_nowait(frame)
                            except Exception:
                                pass
                            last_seq = seq
                except Exception:
                    pass
                time.sleep(0.012)

        threading.Thread(target=_preview_loop, daemon=True).start()

        # ---- 确保快手在前台 ----
        focus = G.current_focus()
        if KS_PKG not in (focus or ""):
            _log(app, "快手不在前台，正在拉起…", "info")
            _launch(G)
            time.sleep(2.5)
        focus = G.current_focus()
        if KS_PKG not in (focus or ""):
            _log(app, "无法把快手切到前台，请在手机上手动打开快手后重新启动", "err")
            app.msg_q.put(("stopped",))
            return

        # ---- 主循环 ----
        watched = 0
        skipped_live = 0
        started_at = time.time()
        current_start = time.time()
        first_thumb = None
        prev_thumb = None
        loop_ref = None       # 循环检测基准帧（观看约2秒锁定，回到相似画面=播完）
        static_count = 0
        rp_prev = None        # 上一次红包进度值
        rp_stable = 0         # 红包进度环"不再增长"的连续次数（=看完了）
        comment_after = random.randint(3, 7)   # 再刷几条后触发一次随机评论
        last_ocr = 0.0
        last_report = 0.0

        while not stop.is_set():
            now = time.time()

            # ===== 定时调度：刷视频 → 领金币 → 休息 → 继续刷 =====
            if schedule_on and now >= sched["phase_end"]:
                if sched["phase"] == "watch":
                    _log(app, f"刷视频阶段满 {watch_phase_min:.0f} 分钟，进入领金币阶段", "info")
                    kuaishou_coin.claim_once._clicked = set()  # 重置本轮任务点击记录
                    r = kuaishou_coin.claim_once(app, G, live, dev_w, dev_h)
                    sched["coin_rounds"] += 1
                    _log(app, f"第 {sched['coin_rounds']} 次领金币完成（红包{r['big_packet']}/"
                              f"任务{r['task_claims']}/弹窗{r['popups']}）", "info")
                    sched["phase"] = "rest"
                    sched["phase_end"] = time.time() + rest_phase_min * 60
                    if rest_phase_min > 0:
                        _log(app, f"休息 {rest_phase_min:.0f} 分钟后继续刷视频", "info")
                    else:
                        sched["phase_end"] = time.time()
                elif sched["phase"] == "rest":
                    _log(app, "休息结束，回到推荐页继续刷视频", "info")
                    _launch(G)
                    time.sleep(2.5)
                    G.press_back()  # 若在详情页，回一次到推荐流
                    time.sleep(1.0)
                    sched["phase"] = "watch"
                    sched["phase_end"] = time.time() + watch_phase_min * 60
                    current_start = time.time()
                    first_thumb = None
                    prev_thumb = None
                    loop_ref = None
                    static_count = 0
                    rp_prev = None
                    rp_stable = 0
                continue

            # 休息阶段：不做任何操作，只等倒计时
            if schedule_on and sched["phase"] == "rest":
                time.sleep(1.0)
                continue

            frame = live.latest()
            if frame is None:
                time.sleep(0.15)
                continue

            # 前台守卫：被系统弹窗 / 其他 App 带离快手就拉回
            focus = G.current_focus()
            if KS_PKG not in (focus or ""):
                _log(app, "已离开快手（弹窗/其他 App），按返回并重新拉起", "warn")
                G.press_back()
                time.sleep(0.5)
                G.press_back()
                time.sleep(0.4)
                _launch(G)
                time.sleep(1.8)
                current_start = time.time()
                first_thumb = None
                prev_thumb = None
                loop_ref = None
                static_count = 0
                rp_prev = None
                rp_stable = 0
                continue

            # 直播检测一：Activity 名（最可靠）
            if _is_live_focus(focus):
                skipped_live += 1
                _log(app, f"识别到直播间（Activity），快速上滑跳过 · 累计跳过 {skipped_live}", "warn")
                _swipe_next(G, dev_w, dev_h)
                app.rounds += 1
                current_start = time.time()
                first_thumb = None
                prev_thumb = None
                loop_ref = None
                static_count = 0
                rp_prev = None
                rp_stable = 0
                time.sleep(1.0)
                continue

            # OCR 节流：约每 1 秒一次，用于直播文字识别 + 预览表格
            boxes = []
            if now - last_ocr >= 1.0:
                last_ocr = now
                try:
                    boxes = G.run_ocr(frame)
                    app.msg_q.put(("ocr", boxes[:30]))
                except Exception:
                    boxes = []

            # 直播检测二：画面文字
            live_kw = _is_live_text(boxes)
            if live_kw:
                skipped_live += 1
                _log(app, f"识别到直播间（画面文字「{live_kw}」），快速上滑跳过 · 累计跳过 {skipped_live}", "warn")
                G.press_back()
                time.sleep(0.45)
                _swipe_next(G, dev_w, dev_h)
                app.rounds += 1
                current_start = time.time()
                first_thumb = None
                prev_thumb = None
                loop_ref = None
                static_count = 0
                rp_prev = None
                rp_stable = 0
                time.sleep(1.0)
                continue

            elapsed = now - current_start
            thumb = _thumb(frame)

            # 记录本条作品开场帧（切换后约 1 秒取，避开转场动画）
            if first_thumb is None and elapsed >= 1.0:
                first_thumb = thumb

            # ===== 红包进度环检测（主判据，最贴合用户观察）=====
            # 左上角红包的黄色进度环随观看增长，播满后停住不再增长。
            # 连续 N 次"进度不再增长"（且进度已达高位）即判定这条看完。
            rp = red_packet_progress(frame)
            rp_done = False
            if rp is not None and elapsed >= MIN_WATCH:
                if rp_prev is not None and rp >= rp_prev - 0.01 and rp >= 0.85:
                    rp_stable += 1
                else:
                    rp_stable = 0
                rp_prev = rp
                if rp_stable >= RP_STABLE_N:
                    rp_done = True
            else:
                rp_stable = 0
                rp_prev = rp

            # 画面静止检测（辅助）：作品播完停在末帧
            frame_delta = _diff(thumb, prev_thumb) if prev_thumb is not None else 99.0
            prev_thumb = thumb
            if elapsed >= MIN_WATCH and frame_delta < STATIC_DIFF_THRESH:
                static_count += 1
            else:
                static_count = 0

            # 循环检测（辅助）：观看约2秒锁定基准帧，之后回到相似画面=播完重播
            loop_hit = False
            if loop_ref is None and elapsed >= 2.0:
                loop_ref = thumb
            elif loop_ref is not None and elapsed >= 5.0:
                if _diff(thumb, loop_ref) < LOOP_DIFF_THRESH:
                    loop_hit = True

            # 判定"这条播完了"：
            #   A) 红包进度环满且停住（主判据）
            #   B) 画面连续静止 → 作品播完停在末帧
            #   C) 循环回到开头 → 作品播完自动重播
            # 没播完就一直等，绝不按时间提前刷走。仅异常卡死才兜底。
            done = False
            reason = ""
            if rp_done:
                done, reason = True, "红包进度环已满且停止（作品看完）"
            elif static_count >= STATIC_SAMPLES:
                done, reason = True, "画面静止（作品已播完）"
            elif loop_hit:
                done, reason = True, "画面循环回到开头（作品已播完）"
            elif elapsed >= HARD_TIMEOUT:
                done, reason = True, f"异常卡死兜底（{elapsed:.0f}s 未检测到播完，强制下一条）"

            if done:
                watched += 1
                app.clicks += 1
                app.rounds += 1
                _log(app, f"第 {watched} 条看完（{reason}），上滑下一条", "click")
                _swipe_next(G, dev_w, dev_h)
                current_start = time.time()
                first_thumb = None
                prev_thumb = None
                loop_ref = None
                static_count = 0
                rp_prev = None
                rp_stable = 0
                time.sleep(random.uniform(0.7, 1.3))
                continue

            # 随机评论：刷了几条后不定时发一条（仅刷视频阶段、非休息/领金币时）
            phase_ok = (not schedule_on) or sched["phase"] == "watch"
            if enable_comment and phase_ok and watched >= comment_after:
                try:
                    import kuaishou_comment
                    kuaishou_comment.post_comment(app, G, live, dev_w, dev_h)
                except Exception as e:
                    _log(app, f"[评论] 跳过: {e}", "warn")
                comment_after = watched + random.randint(3, 7)
                first_thumb = None
                prev_thumb = None
                loop_ref = None
                static_count = 0
                rp_prev = None
                rp_stable = 0
                current_start = time.time()
                continue

            # 每 3 秒汇报一次进度
            if now - last_report >= 3.0:
                last_report = now
                phase_txt = "刷视频"
                if schedule_on:
                    remain = max(0, sched["phase_end"] - now)
                    if sched["phase"] == "rest":
                        phase_txt = f"休息 {remain/60:.1f}分"
                    else:
                        phase_txt = f"刷视频(距领金币 {remain/60:.1f}分)"
                play_state = "静止中(将上滑)" if static_count >= STATIC_SAMPLES // 2 else "播放中(等播完)"
                _log(app, f"[{phase_txt}] 本条已看 {elapsed:.0f}s · {play_state} · 已刷 {watched} · 直播跳过 {skipped_live}", "info")
                app.msg_q.put((
                    "stat", app.clicks, app.rounds, time.time() - started_at,
                    f"快手 · {phase_txt} · 已刷 {watched} · 直播跳过 {skipped_live} · 金币轮次 {sched['coin_rounds']}",
                ))

            time.sleep(0.2)

    except Exception as e:
        _log(app, f"快手模式异常: {type(e).__name__}: {e}", "err")
    finally:
        if live is not None:
            try:
                live.stop()
            except Exception:
                pass
        # 停止时若停在任务中心/落地页，按返回归位到快手首页，方便下次启动直接刷
        try:
            for _ in range(6):
                f = G.current_focus() or ""
                if KS_PKG not in f:
                    break
                act = f.lower()
                if "homeactivity" in act or "photodetailactivity" in act:
                    break
                G.press_back()
                time.sleep(0.5)
        except Exception:
            pass
        if stop.is_set():
            app.msg_q.put(("stopped",))
