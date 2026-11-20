# -*- coding: utf-8 -*-
"""
汽水音乐去广告 GUI 版
实时手机截图预览 + OCR 识别信息 + 自动点击 + 运行日志
"""

from __future__ import annotations

import os
import queue
import random
import re
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from shutil import which
from typing import Optional

import cv2
import numpy as np
from PIL import Image, ImageTk

from live_screen import LiveScreen, app_dir, find_scrcpy

# ---------------- 控制台/编码 ----------------
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 延迟导入 OCR（启动更快，也方便在未装包时提示）
RapidOCR = None

# ========================= 配置 =========================
TARGETS = ["开心收下", "领取奖励", "领取", "广告膨胀", "开宝箱得金币", "听歌已赚",
           "继续观看", "领取成功", "我知道了"]
# 金币页里的"入口按钮"，点它才会开出下一条广告
AD_ENTRY_TARGETS = {"开宝箱得金币"}
QISHUI_PKG = "com.luna.music"
OCR_SCORE = 0.55
OCR_MAX_WIDTH = 720
POLL_INTERVAL = 1.2
CLICK_COOLDOWN = 1.8
# 点击坐标随机抖动（像素），及等待时间浮动，降低脚本特征
CLICK_JITTER = 6
SCREEN_W = 0  # 运行时写入
SCREEN_H = 0
MISS_BACK_THRESHOLD = 6
STUCK_SECONDS = 20         # 连续多久没有任何进展就判定卡住（直播间/弹窗/白屏）
NO_COOLDOWN_TARGETS = {"领取成功", "领取奖励", "领取", "我知道了"}
# 金币页入口按钮长冷却，避免反复开宝箱刷出弹窗
ENTRY_COOLDOWN = 6.0
CENTER_TARGETS = {"领取奖励", "领取"}
# 「继续观看」以底部大按钮为准（顶部可能有同名关闭/跳过）
BOTTOM_TARGETS = {"继续观看"}

# ROI 放宽：按钮常在底部，旧值会把 y>0.92 的「领取奖励」滤掉导致只识别不点击
# 这些文案属于广告落地页的"引流按钮"，点了会跳出汽水音乐（跳浏览器/应用市场），必须跳过
CLICK_BLACKLIST = ("下载", "安装", "打开应用", "查看详情", "了解更多", "立即购买", "领券",
                   "去逛逛", "去看看", "去看看", "获取", "预约", "分享", "领取优惠")

TARGET_ROI = {
    "领取奖励": {"x": (0.10, 0.90), "y": (0.10, 0.98)},
    "领取": {"x": (0.10, 0.90), "y": (0.10, 0.98)},
    "继续观看": {"x": (0.05, 0.98), "y": (0.02, 0.98)},
    "领取成功": {"x": (0.05, 0.95), "y": (0.15, 0.95)},
    "我知道了": {"x": (0.10, 0.90), "y": (0.15, 0.98)},
    "开宝箱得金币": {"x": (0.40, 1.00), "y": (0.45, 1.00)},
    "广告膨胀": {"x": (0.10, 0.90), "y": (0.40, 1.00)},

    "开心收下": {"x": (0.10, 0.90), "y": (0.30, 0.95)},
    "听歌已赚": {"x": (0.25, 1.00), "y": (0.02, 0.13)},
}

OCR_FIX = {
    "领収": "领取", "领职": "领取", "领敢": "领取", "领联": "领取",
    "奨": "奖", "奬": "奖", "勵": "励",
    "继渎": "继续", "继读": "继续", "観看": "观看", "观着": "观看",
}
COUNTDOWN_PAT = re.compile(
    r"(秒后|后再|再看|再闯|后可|后领|后得|后获得|试看|完整观看|观看完整)"
)

SCRIPT_DIR = Path(__file__).resolve().parent
DEBUG_DIR = SCRIPT_DIR / "debug_clicks"
LOG_DIR = SCRIPT_DIR / "logs"
# =========================================================


# --------------------------- ADB ---------------------------

def find_adb() -> str:
    hit = which("adb") or which("adb.exe")
    if hit:
        return hit
    for p in (
        Path(os.environ.get("LOCALAPPDATA", "")) / "Android" / "platform-tools" / "adb.exe",
        Path(os.environ.get("ANDROID_HOME", "")) / "platform-tools" / "adb.exe",
        Path(r"C:\platform-tools\adb.exe"),
        SCRIPT_DIR / "platform-tools" / "adb.exe",
        SCRIPT_DIR / "adb.exe",
    ):
        try:
            if p.is_file():
                return str(p)
        except Exception:
            pass
    return "adb"


ADB = find_adb()
DEVICE_SERIAL: Optional[str] = None

# Windows: 调用 adb 时不弹出黑色控制台窗口
CREATE_NO_WINDOW = 0x08000000
_NO_WINDOW = {"creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {}


def adb_args(args: list[str]) -> list[str]:
    if DEVICE_SERIAL:
        return [ADB, "-s", DEVICE_SERIAL, *args]
    return [ADB, *args]


def run_adb(args: list[str], timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(adb_args(args), capture_output=True, timeout=timeout, **_NO_WINDOW)


def run_adb_host(args: list[str], timeout: int = 15) -> str:
    """host 级 adb 命令（connect/disconnect/kill-server 等，不带 -s）。"""
    r = subprocess.run([ADB, *args], capture_output=True, timeout=timeout, **_NO_WINDOW)
    out = (r.stdout or b"").decode("utf-8", errors="replace").strip()
    err = (r.stderr or b"").decode("utf-8", errors="replace").strip()
    return out or err


def run_adb_text(args: list[str], timeout: int = 15) -> str:
    r = run_adb(args, timeout=timeout)
    return (r.stdout or b"").decode("utf-8", errors="replace").strip()


def _settings_file(name: str) -> Path:
    return Path(__file__).resolve().parent / name


def load_wifi_ip() -> str:
    """读上次成功连接的 WiFi ADB 地址（含 :5555）。"""
    try:
        v = _settings_file(".last_wifi_ip").read_text(encoding="utf-8").strip()
        if v:
            return v
    except Exception:
        pass
    # 兜底：上次选的目标设备本身是 IP:端口
    try:
        v = _settings_file(".last_device").read_text(encoding="utf-8").strip()
        if ":" in v:
            return v
    except Exception:
        pass
    return ""


def save_wifi_ip(host: str) -> None:
    """保存 WiFi ADB 地址，下次启动自动带出并尝试连接。"""
    host = (host or "").strip()
    if not host:
        return
    try:
        _settings_file(".last_wifi_ip").write_text(host, encoding="utf-8")
    except Exception:
        pass


def snapshot_of_wifi_ip() -> str:
    return load_wifi_ip()


def screenshot() -> Optional[np.ndarray]:
    r = run_adb(["exec-out", "screencap", "-p"], timeout=20)
    if r.returncode != 0 or not r.stdout:
        return None
    return cv2.imdecode(np.frombuffer(r.stdout, np.uint8), cv2.IMREAD_COLOR)


def collect_device_info() -> dict:
    """采集手机基础信息：机型/系统/分辨率/电量/充电/网络。"""
    info: dict = {
        "model": "?",
        "brand": "?",
        "android": "?",
        "resolution": "?",
        "battery": "?",
        "charging": "?",
        "network": "?",
        "ip": "?",
        "wifi": "?",
    }
    try:
        info["model"] = run_adb_text(["shell", "getprop", "ro.product.model"]) or "?"
        info["brand"] = run_adb_text(["shell", "getprop", "ro.product.brand"]) or "?"
        info["android"] = run_adb_text(["shell", "getprop", "ro.build.version.release"]) or "?"
    except Exception:
        pass
    try:
        w, h = get_device_size()
        info["resolution"] = f"{w}x{h}"
    except Exception:
        pass
    # 电量 / 充电
    try:
        bat = run_adb_text(["shell", "dumpsys", "battery"])
        m = re.search(r"level:\s*(\d+)", bat)
        if m:
            info["battery"] = f"{m.group(1)}%"
        st = re.search(r"status:\s*(\d+)", bat)
        # 2=charging 5=full
        if st:
            code = st.group(1)
            info["charging"] = {"2": "充电中", "5": "已充满", "3": "放电", "4": "未充电"}.get(code, f"状态{code}")
        if "AC powered: true" in bat or "USB powered: true" in bat or "Wireless powered: true" in bat:
            if info["charging"] in ("?", "放电", "未充电"):
                info["charging"] = "充电中"
    except Exception:
        pass
    # 网络：WiFi 名 / IP
    try:
        wifi = run_adb_text(["shell", "dumpsys", "wifi"])
        m = re.search(r"mWifiInfo SSID:\s*(.+?),", wifi)
        if not m:
            m = re.search(r'SSID:\s*"?([^",\n]+)"?', wifi)
        if m:
            info["wifi"] = m.group(1).strip().strip('"')
        ip = run_adb_text(["shell", "ip", "-f", "inet", "addr", "show", "wlan0"])
        m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", ip)
        if m:
            info["ip"] = m.group(1)
            info["network"] = f"WiFi {info['wifi']}" if info["wifi"] not in ("?", "") else "WiFi"
        else:
            # 蜂窝
            cell = run_adb_text(["shell", "ip", "-f", "inet", "addr", "show", "rmnet_data0"])
            m2 = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", cell)
            if m2:
                info["ip"] = m2.group(1)
                info["network"] = "移动数据"
            else:
                info["network"] = "未联网"
    except Exception:
        pass
    return info


def get_device_size() -> tuple[int, int]:
    """读取真机分辨率，优先 Physical size。"""
    txt = run_adb_text(["shell", "wm", "size"])
    m = re.search(r"(\d+)\s*x\s*(\d+)", txt or "")
    if m:
        return int(m.group(1)), int(m.group(2))
    return 1080, 2340


def map_to_device(x: float, y: float, fw: int, fh: int, dw: int, dh: int) -> tuple[int, int]:
    """把影像帧坐标换算成真机点击坐标。"""
    if fw <= 0 or fh <= 0:
        return int(x), int(y)
    return int(round(x * dw / fw)), int(round(y * dh / fh))


def tap(x: int, y: int) -> None:
    """点击并加小幅随机抖动，降低固定坐标点击特征。"""
    time.sleep(random.uniform(0.03, 0.15))
    jx = random.randint(-CLICK_JITTER, CLICK_JITTER)
    jy = random.randint(-CLICK_JITTER, CLICK_JITTER)
    tx, ty = int(x) + jx, int(y) + jy
    if SCREEN_W:
        tx = max(2, min(tx, SCREEN_W - 2))
    if SCREEN_H:
        ty = max(2, min(ty, SCREEN_H - 2))
    run_adb(["shell", "input", "tap", str(tx), str(ty)])


def human_sleep(base: float, spread: float = 0.35) -> None:
    """在 base 附近随机浮动的等待。"""
    time.sleep(max(0.15, base + random.uniform(-spread, spread)))


def press_back() -> None:
    run_adb(["shell", "input", "keyevent", "4"])


def current_focus() -> str:
    """当前前台窗口（含包名），取不到返回空串。"""
    serial = DEVICE_SERIAL
    if not serial:
        try:
            for ln in run_adb_text(["devices"]).splitlines()[1:]:
                parts = ln.split()
                if len(parts) >= 2 and parts[1] == "device":
                    serial = parts[0]
                    break
        except Exception:
            serial = None
    cmd = [ADB] + (["-s", serial] if serial else []) + ["shell", "dumpsys", "window"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=8, **_NO_WINDOW)
        out = (r.stdout or b"").decode("utf-8", errors="replace")
    except Exception:
        return ""
    for line in out.splitlines():
        if "mCurrentFocus" in line:
            return line.strip()
    for line in out.splitlines():
        if "mFocusedApp" in line:
            return line.strip()
    return ""


def qishui_in_front():
    """True=在前台，False=不在，None=取不到状态（离线/权限受限）。"""
    focus = current_focus()
    if not focus:
        return None
    return QISHUI_PKG in focus


def relaunch_qishui() -> None:
    """把汽水音乐拉到前台。"""
    try:
        run_adb(["shell", "monkey", "-p", QISHUI_PKG, "-c", "android.intent.category.LAUNCHER", "1"], timeout=15)
    except Exception:
        pass


# --------------------------- OCR ---------------------------

_ocr = None


def get_ocr():
    global _ocr, RapidOCR
    if _ocr is None:
        if RapidOCR is None:
            from rapidocr_onnxruntime import RapidOCR as _R
            RapidOCR = _R
        _ocr = RapidOCR()
    return _ocr


def normalize(text: str) -> str:
    s = (text or "").strip().replace(" ", "").replace("　", "")
    s = re.sub(r"[×xX✕✖·•|丨\[\]【】()（）]+$", "", s)
    s = re.sub(r"^[×xX✕✖·•|丨]+", "", s)
    for a, b in OCR_FIX.items():
        s = s.replace(a, b)
    return s


def is_countdown_noise(recognized: str) -> bool:
    s = normalize(recognized)
    if not s:
        return True
    if re.search(r"\d+\s*秒", s) or COUNTDOWN_PAT.search(s):
        return True
    return False


def text_hit(recognized: str, target: str) -> bool:
    a, b = normalize(recognized), normalize(target)
    if not a or not b:
        return False
    if is_countdown_noise(a):
        return False
    if len(b) <= 2:
        return a == b or a == b + "了"
    if a == b:
        return True
    if b in a and len(a) <= len(b) + 4:
        return True
    if len(b) >= 4 and any(b[:i] + b[i + 1 :] in a for i in range(len(b))):
        return True
    return False


@dataclass
class OcrBox:
    text: str
    score: float
    cx: float
    cy: float
    matched: str = ""


def run_ocr(screen: np.ndarray) -> list[OcrBox]:
    h, w = screen.shape[:2]
    scale = 1.0
    img = screen
    if w > OCR_MAX_WIDTH:
        scale = OCR_MAX_WIDTH / w
        img = cv2.resize(screen, (OCR_MAX_WIDTH, int(h * scale)), interpolation=cv2.INTER_AREA)
    result, _ = get_ocr()(img)
    boxes: list[OcrBox] = []
    if not result:
        return boxes
    for item in result:
        pts, text, score = item[0], item[1], float(item[2])
        if score < OCR_SCORE:
            continue
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        cx = (min(xs) + max(xs)) / 2 / scale
        cy = (min(ys) + max(ys)) / 2 / scale
        boxes.append(OcrBox(str(text), score, cx, cy))
    return boxes


def in_roi(target: str, cx: float, cy: float, sw: int, sh: int) -> bool:
    roi = TARGET_ROI.get(target)
    if not roi:
        return True
    x0, x1 = roi["x"]
    y0, y1 = roi["y"]
    return x0 * sw <= cx <= x1 * sw and y0 * sh <= cy <= y1 * sh


# 直播间强特征：只保留真正的直播间 UI 元素（避免把"直播带货广告"误判成直播间）
LIVE_STRONG = (
    "正在直播", "直播中", "小时榜", "人气榜", "送礼物", "说点什么",
    "在线人数", "观众", "粉丝团", "点亮", "关注主播",
)
LIVE_WEAK = ("直播", "关注", "人气", "礼物")


def in_ad_activity() -> bool:
    """当前是不是广告页（汽水音乐用 ss.android.excitingvideo 播放激励广告）。"""
    f = current_focus().lower()
    return ("excitingvideo" in f) or ("adactivity" in f)


def is_live_room(boxes: list[OcrBox], sh: int) -> bool:
    """识别真正的直播间页面；广告页一律返回 False。"""
    if in_ad_activity():
        return False
    top = sh * 0.18
    # 广告页/带广告浮层的页面绝不当直播间，否则会误按返回把 App 顶到后台
    ad_veto = ("领取成功", "领取奖励", "坚持退出", "跳过广告", "关闭广告", "反馈", "广告",
               "查看详情", "立即下载", "了解更多", "进入直播间")
    for b in boxes:
        t = normalize(b.text)
        if any(k in t for k in ad_veto):
            return False
    has_follow = False
    has_live = False
    for b in boxes:
        t = normalize(b.text)
        if any(k in t for k in ("正在直播", "直播中", "小时榜", "人气榜", "送礼物", "说点什么", "在线人数")):
            return True
        if b.cy <= top:
            if t == "关注" or t.startswith("关注"):
                has_follow = True
            # 只认明确的"正在直播/直播中"，泛"直播"字样（如广告里的直播带货文案）不算
            if ("正在直播" in t) or ("直播中" in t):
                has_live = True
    return has_follow and has_live


def find_ad_close(boxes: list[OcrBox], sw: int, sh: int) -> Optional[OcrBox]:
    """广告页右上角的关闭按钮（原始文本里带 ×，normalize 会把 × 去掉，必须看原文）。"""
    best = None
    for b in boxes:
        raw = (b.text or "").strip()
        t = normalize(raw)
        if b.cx <= sw * 0.55 or b.cy >= sh * 0.16:
            continue
        has_x = ("×" in raw) or ("✕" in raw) or ("✖" in raw) or raw.upper().endswith("X")
        if not has_x:
            continue
        if ("秒后" in t) or ("可领取" in t) or re.search(r"\d+\s*秒", t):
            continue
        if ("领取成功" in t) or ("广告" in t) or t == "":
            best = b if best is None else (b if b.cy < best.cy else best)
    return best


def is_in_ad(boxes: list[OcrBox]) -> bool:
    for b in boxes:
        t = normalize(b.text)
        if t == "广告" or "秒后可领" in t or re.search(r"\d+\s*秒后", t):
            return True
    return False


def parse_countdown_seconds(boxes: list[OcrBox]) -> Optional[int]:
    """从「03秒后可领奖励 / 再看15秒」等文案解析剩余秒数。"""
    best: Optional[int] = None
    for b in boxes:
        t = normalize(b.text)
        # 03秒后可领奖励 / 5秒后
        m = re.search(r"(\d{1,3})\s*秒后", t)
        if m:
            n = int(m.group(1))
            if 0 <= n <= 180:
                best = n if best is None else min(best, n)
            continue
        # 再看15秒继续解锁
        m = re.search(r"再看\s*(\d{1,3})\s*秒", t)
        if m:
            n = int(m.group(1))
            if 0 <= n <= 180:
                best = n if best is None else min(best, n)
    return best


# 只关注这几个目标（以及广告/直播间状态词）
FOCUS_WORDS = ("领取", "继续", "观看", "成功", "知道", "广告", "关注", "直播", "反馈")


def is_focus_text(text: str) -> bool:
    t = normalize(text)
    if not t:
        return False
    if any(w in t for w in FOCUS_WORDS):
        return True
    # 倒计时也算状态信息
    if re.search(r"\d+\s*秒", t):
        return True
    return False


def filter_focus(boxes: list[OcrBox], sw: int, sh: int) -> list[OcrBox]:
    """只保留与按钮/状态相关的识别结果，并标记命中目标。"""
    for target in TARGETS:
        for b in boxes:
            if text_hit(b.text, target) and in_roi(target, b.cx, b.cy, sw, sh):
                if not b.matched:
                    b.matched = target
    return [b for b in boxes if b.matched or is_focus_text(b.text)]


def pick_click(boxes: list[OcrBox], sw: int, sh: int) -> Optional[tuple[str, OcrBox, float]]:
    """返回 (target, box, score)。优先级即 TARGETS 顺序。"""
    rejects: list[str] = []
    for target in TARGETS:
        cands = []
        for b in boxes:
            t = normalize(b.text)
            # 长文本多半是 OCR 合并的一整行（顶部状态条等），点它容易误触关闭按钮
            if len(t) > 10 and target not in ("继续观看", "广告膨胀", "听歌已赚"):
                continue
            if any(k in t for k in CLICK_BLACKLIST):
                continue
            if not text_hit(b.text, target):
                continue
            if not in_roi(target, b.cx, b.cy, sw, sh):
                rejects.append(f"{target}:{b.text!r}@({int(b.cx)},{int(b.cy)}) 出界")
                continue
            cands.append(b)
        if not cands:
            continue
        if target in CENTER_TARGETS:
            mx, my = sw * 0.5, sh * 0.5
            cands.sort(key=lambda b: (-b.score, (b.cx - mx) ** 2 + (b.cy - my) ** 2))
        elif target in BOTTOM_TARGETS:
            # 优先底部按钮，避免点到顶部同名「继续观看」
            cands.sort(key=lambda b: (-b.score, -b.cy, abs(b.cx - sw * 0.5)))
        else:
            cands.sort(key=lambda b: (-b.score, b.cy, -b.cx))
        best = cands[0]
        best.matched = target
        pick_click.last_rejects = []
        return target, best, best.score
    # 便于排查：识别到了但 ROI/文案没过
    if rejects:
        pick_click.last_rejects = rejects[:6]
    else:
        pick_click.last_rejects = []
    return None


pick_click.last_rejects = []  # type: ignore


# --------------------------- GUI ---------------------------

try:
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog
except Exception as e:  # pragma: no cover
    print("无法加载 tkinter:", e)
    sys.exit(1)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("汽水音乐去广告")
        self.geometry("1180x760")
        self.minsize(980, 640)
        self.configure(bg="#1e1f26")
        self._set_window_icon()

        self.msg_q: queue.Queue = queue.Queue()
        # 预览专用队列：只保留最新一帧（LifoQueue(maxsize=1) 天然丢旧帧）
        self.frame_q: queue.LifoQueue = queue.LifoQueue(maxsize=1)
        self._last_boxes: list[OcrBox] = []
        self._draw_n = 0
        self._draw_t0 = time.time()
        self._draw_fps = 0.0
        self.stop_event = threading.Event()
        self.worker: Optional[threading.Thread] = None
        self.running = False
        self.clicks = 0
        self.rounds = 0
        self.started_at = 0.0
        self._photo = None
        self._last_frame: Optional[np.ndarray] = None
        self.log_buf = deque(maxlen=800)

        # 双模式：界面显示值含"快手"即为快手模式
        self.mode_var = tk.StringVar(value="汽水音乐·去广告")
        self.ks_watch_var = tk.StringVar(value="25")       # 每条作品最长停留秒
        self.ks_watch_phase_var = tk.StringVar(value="60")  # 刷视频阶段分钟，到点领金币
        self.ks_rest_phase_var = tk.StringVar(value="10")   # 领金币后休息分钟
        self.ks_claim_var = tk.BooleanVar(value=True)       # 是否启用定时领金币

        self._build_style()
        self._build_ui()
        self.after(120, self._poll_queue)

        # 启动时探测设备 + 周期刷新手机信息
        self.after(200, self._refresh_device)
        self.after(400, self._refresh_device_info)
        self.after(8000, self._schedule_device_info)
        self.after(600, self._autoconnect_once)
        self.after(2500, self._autostart_once)

    # ---------- 图标 ----------
    def _set_window_icon(self):
        """把 assets 里的汽水音乐风格图标设为窗口/Dock 图标。"""
        try:
            icon_png = Path(__file__).resolve().parent / "assets" / "icon_256.png"
            if icon_png.is_file():
                self._icon_img = tk.PhotoImage(file=str(icon_png))
                self.iconphoto(True, self._icon_img)
        except Exception:
            pass

    # ---------- UI ----------
    def _build_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TFrame", background="#1e1f26")
        style.configure("TLabelframe", background="#1e1f26", foreground="#e8e8ef")
        style.configure("TLabelframe.Label", background="#1e1f26", foreground="#a8b1ff")
        style.configure("TLabel", background="#1e1f26", foreground="#e8e8ef")
        style.configure("Header.TLabel", background="#1e1f26", foreground="#ffffff", font=("PingFang SC", 15, "bold"))
        style.configure("Status.TLabel", background="#1e1f26", foreground="#9aa3b2", font=("Menlo", 10))
        style.configure("Accent.TButton", font=("PingFang SC", 11, "bold"))
        style.configure("Treeview", background="#2a2c36", fieldbackground="#2a2c36",
                        foreground="#e8e8ef", rowheight=24)
        style.configure("Treeview.Heading", background="#3a3d4a", foreground="#ffffff")

    def _build_ui(self):
        # 顶栏
        top = ttk.Frame(self, padding=(12, 10))
        top.pack(fill="x")
        ttk.Label(top, text="自动化工具", style="Header.TLabel").pack(side="left")
        # 模式选择
        mode_box = ttk.Frame(top)
        mode_box.pack(side="left", padx=(16, 0))
        ttk.Label(mode_box, text="模式:").pack(side="left")
        self.mode_combo = ttk.Combobox(
            mode_box, state="readonly", width=20,
            values=["汽水音乐·去广告", "快手·自动刷作品"],
            textvariable=self.mode_var,
        )
        self.mode_combo.current(0)
        self.mode_combo.pack(side="left", padx=4)
        self.mode_combo.bind("<<ComboboxSelected>>", self._on_mode_change)
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(top, textvariable=self.status_var, style="Status.TLabel").pack(side="left", padx=16)

        btns = ttk.Frame(top)
        btns.pack(side="right")
        self.btn_start = ttk.Button(btns, text="▶ 启动", style="Accent.TButton", command=self.start_bot)
        self.btn_start.pack(side="left", padx=4)
        self.btn_stop = ttk.Button(btns, text="⏹ 停止", command=self.stop_bot, state="disabled")
        self.btn_stop.pack(side="left", padx=4)
        self.screen_off_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(btns, text="熄屏运行", variable=self.screen_off_var).pack(side="left", padx=6)
        ttk.Button(btns, text="刷新设备", command=self._refresh_device).pack(side="left", padx=4)
        ttk.Button(btns, text="保存截图", command=self._save_shot).pack(side="left", padx=4)
        ttk.Button(btns, text="清空日志", command=self._clear_log).pack(side="left", padx=4)

        # 主体
        main = ttk.Frame(self, padding=(12, 0, 12, 8))
        main.pack(fill="both", expand=True)

        # 左：截图预览
        left = ttk.LabelFrame(main, text="手机实时影像（H264 流）", padding=8)
        left.pack(side="left", fill="both", expand=True, padx=(0, 8))
        self.preview = tk.Label(left, bg="#12131a", text="尚未截图\n启动后自动刷新",
                                fg="#6b7280", font=("PingFang SC", 12), anchor="center")
        self.preview.pack(fill="both", expand=True)
        self.preview_info = tk.StringVar(value="分辨率: -   缩放: -")
        ttk.Label(left, textvariable=self.preview_info, style="Status.TLabel").pack(fill="x", pady=(6, 0))

        # 右：识别信息
        right = ttk.Frame(main)
        right.pack(side="right", fill="both", expand=False)
        right.configure(width=430)

        # WiFi ADB
        wf = ttk.LabelFrame(right, text="WiFi ADB", padding=8)
        wf.pack(fill="x", pady=(0, 8))
        row = ttk.Frame(wf)
        row.pack(fill="x")
        ttk.Label(row, text="IP:").pack(side="left")
        self.wifi_ip_var = tk.StringVar(value=load_wifi_ip() or "192.168.")
        self.wifi_ip_entry = ttk.Entry(row, textvariable=self.wifi_ip_var, width=18)
        self.wifi_ip_entry.pack(side="left", padx=4)
        ttk.Button(row, text="连接", command=self._wifi_connect).pack(side="left", padx=2)
        ttk.Button(row, text="开启无线", command=self._wifi_enable).pack(side="left", padx=2)
        ttk.Button(row, text="断开", command=self._wifi_disconnect).pack(side="left", padx=2)
        self.wifi_status_var = tk.StringVar(value="可先 USB 连接后点「开启无线」，再填手机 IP 连接")
        ttk.Label(wf, textvariable=self.wifi_status_var, wraplength=400, foreground="#9aa3b2").pack(fill="x", pady=(4, 0))

        # 快手模式设置（选中快手时显示）
        self.ks_frame = ttk.LabelFrame(right, text="快手设置", padding=8)
        ks_row1 = ttk.Frame(self.ks_frame)
        ks_row1.pack(fill="x", pady=(0, 4))
        ttk.Label(ks_row1, text="每条停留(秒):").pack(side="left")
        ttk.Entry(ks_row1, textvariable=self.ks_watch_var, width=6).pack(side="left", padx=4)
        ttk.Label(ks_row1, text="播完自动上滑，到点兜底", foreground="#9aa3b2").pack(side="left")
        ks_row2 = ttk.Frame(self.ks_frame)
        ks_row2.pack(fill="x", pady=(0, 4))
        ttk.Checkbutton(ks_row2, text="定时领金币循环", variable=self.ks_claim_var,
                        command=self._on_claim_toggle).pack(side="left")
        ks_row3 = ttk.Frame(self.ks_frame)
        ks_row3.pack(fill="x")
        ttk.Label(ks_row3, text="刷视频(分):").pack(side="left")
        self.ks_watch_phase_entry = ttk.Entry(ks_row3, textvariable=self.ks_watch_phase_var, width=5)
        self.ks_watch_phase_entry.pack(side="left", padx=(4, 8))
        ttk.Label(ks_row3, text="休息(分):").pack(side="left")
        self.ks_rest_phase_entry = ttk.Entry(ks_row3, textvariable=self.ks_rest_phase_var, width=5)
        self.ks_rest_phase_entry.pack(side="left", padx=4)
        ttk.Label(self.ks_frame, text="到点自动点红包领金币 → 休息 → 继续刷；直播自动跳过",
                  foreground="#9aa3b2", wraplength=400).pack(fill="x", pady=(4, 0))
        if "快手" not in self.mode_var.get():
            self.ks_frame.pack_forget()

        # 手机信息
        devf = ttk.LabelFrame(right, text="手机信息", padding=8)
        devf.pack(fill="x", pady=(0, 8))
        self._devf = devf  # 供模式切换时定位快手设置的位置
        pick = ttk.Frame(devf)
        pick.pack(fill="x", pady=(0, 6))
        ttk.Label(pick, text="目标设备:").pack(side="left")
        self.device_map: dict[str, tuple[str, str]] = {}
        self.device_combo = ttk.Combobox(pick, state="readonly", width=24, values=[])
        self.device_combo.pack(side="left", padx=4)
        self.device_combo.bind("<<ComboboxSelected>>", lambda e: self._apply_selected_device())
        ttk.Button(pick, text="刷新", command=self._refresh_device).pack(side="left")
        self.device_var = tk.StringVar(value="设备信息获取中…")
        ttk.Label(devf, textvariable=self.device_var, wraplength=400, justify="left").pack(fill="x")

        # 统计
        statf = ttk.LabelFrame(right, text="运行统计", padding=8)
        statf.pack(fill="x", pady=(0, 8))
        self.stat_var = tk.StringVar(value="点击 0 次 · 轮次 0 · 用时 0s · ADB: 未检测")
        ttk.Label(statf, textvariable=self.stat_var, wraplength=400).pack(fill="x")

        # OCR 表格
        ocrf = ttk.LabelFrame(right, text="重点识别（领取/继续观看/成功等）", padding=8)
        ocrf.pack(fill="both", expand=True, pady=(0, 8))
        cols = ("text", "score", "pos", "hit")
        self.tree = ttk.Treeview(ocrf, columns=cols, show="headings", height=12)
        self.tree.heading("text", text="识别文本")
        self.tree.heading("score", text="置信度")
        self.tree.heading("pos", text="坐标")
        self.tree.heading("hit", text="命中")
        self.tree.column("text", width=170, anchor="w")
        self.tree.column("score", width=70, anchor="center")
        self.tree.column("pos", width=110, anchor="center")
        self.tree.column("hit", width=70, anchor="center")
        self.tree.pack(fill="both", expand=True)

        # 日志
        logf = ttk.LabelFrame(right, text="运行日志", padding=8)
        logf.pack(fill="both", expand=False)
        self.log_text = tk.Text(logf, height=10, bg="#15161c", fg="#c9d1d9",
                                insertbackground="#c9d1d9", font=("Menlo", 9),
                                relief="flat", wrap="word")
        self.log_text.pack(fill="both", expand=True)
        self.log_text.configure(state="disabled")

    # ---------- helpers ----------
    def _is_kuaishou(self) -> bool:
        return "快手" in self.mode_var.get()

    def _on_mode_change(self, _=None):
        """切换模式时显示/隐藏快手设置，并给出提示。"""
        if self._is_kuaishou():
            self.ks_frame.pack(fill="x", pady=(0, 8), before=self._devf)
            self.log("已切到【快手·自动刷作品】：请先打开快手并登录，点▶启动后自动刷推荐", "info")
        else:
            self.ks_frame.pack_forget()
            self.log("已切到【汽水音乐·去广告】", "info")

    def _on_claim_toggle(self):
        state = "normal" if self.ks_claim_var.get() else "disabled"
        self.ks_watch_phase_entry.configure(state=state)
        self.ks_rest_phase_entry.configure(state=state)

    def log(self, msg: str, level: str = "info"):
        # Tk 控件只能在主线程操作，后台线程一律入队，避免 macOS 上段错误闪退
        if threading.current_thread() is not threading.main_thread():
            self.msg_q.put(("log", msg, level))
            return
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}\n"
        # 同时落盘，方便卡住后回看发生了什么
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            log_file = LOG_DIR / "run.log"
            if log_file.exists() and log_file.stat().st_size > 3_000_000:
                log_file.rename(LOG_DIR / "run.1.log")
            with open(log_file, "a", encoding="utf-8") as fh:
                fh.write(line)
        except Exception:
            pass
        self.log_buf.append(line)
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
        if level == "click":
            self.log_text.tag_add("click", "end-2l", "end-1l")
            self.log_text.tag_configure("click", foreground="#4ade80")
        elif level == "warn":
            self.log_text.tag_add("warn", "end-2l", "end-1l")
            self.log_text.tag_configure("warn", foreground="#fbbf24")
        elif level == "err":
            self.log_text.tag_add("err", "end-2l", "end-1l")
            self.log_text.tag_configure("err", foreground="#f87171")

    def _clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self.log_buf.clear()

    def _schedule_device_info(self):
        self._refresh_device_info()
        self.after(8000, self._schedule_device_info)

    def _refresh_device_info(self):
        def work():
            try:
                info = collect_device_info()
                self.msg_q.put(("device_info", info))
            except Exception as e:
                self.msg_q.put(("device_info", {"error": str(e)}))

        threading.Thread(target=work, daemon=True).start()

    def _wifi_enable(self):
        """USB 已连接时，开启手机 TCP/IP 调试（默认 5555）。"""
        def work():
            self.wifi_status_var.set("正在开启无线调试…")
            try:
                # 用当前已连 USB 设备执行 tcpip
                r = run_adb_text(["tcpip", "5555"], timeout=10)
                # 读取手机 WiFi IP
                info = collect_device_info()
                ip = info.get("ip", "")
                if ip and ip != "?":
                    self.wifi_ip_var.set(f"{ip}:5555")
                    save_wifi_ip(f"{ip}:5555")
                    self.wifi_status_var.set(f"已开启无线调试，请拔线后点「连接」→ {ip}:5555")
                else:
                    self.wifi_status_var.set(f"已发送 tcpip 5555，请手动填手机 IP（{r[:60]}）")
            except Exception as e:
                self.wifi_status_var.set(f"开启失败: {e}")

        threading.Thread(target=work, daemon=True).start()

    def _wifi_connect(self):
        host = (self.wifi_ip_var.get() or "").strip()
        if not host:
            self.wifi_status_var.set("请填写 IP，如 192.168.1.8:5555")
            return
        if ":" not in host:
            host = host + ":5555"
            self.wifi_ip_var.set(host)

        def work():
            self._ui(self.wifi_status_var.set, f"正在连接 {host} …")
            try:
                # 先 adb connect（用 host 级命令，避免带上 -s）
                r1 = run_adb_host(["connect", host], timeout=12)
                if "connected to" not in (r1 or "").lower():
                    # 失败常见于 adb 服务里残留 offline 条目，清掉并重启服务后重试一次
                    self.msg_q.put(("log", "首次连接失败，清理离线条目并重启 adb 服务后重试", "warn"))
                    for ln in run_adb_text(["devices"]).splitlines()[1:]:
                        parts = ln.split()
                        if len(parts) >= 2 and parts[1] == "offline":
                            run_adb_host(["disconnect", parts[0]], timeout=8)
                    run_adb_host(["kill-server"], timeout=8)
                    run_adb_host(["start-server"], timeout=12)
                    time.sleep(2)
                    r1 = run_adb_host(["connect", host], timeout=12) or r1
                out = run_adb_text(["devices"])
                ok = any(host.split(":")[0] in ln and "device" in ln for ln in out.splitlines())
                if "connected" in (r1 or "").lower() or ok:
                    self._ui(self.wifi_status_var.set, f"已连接 {host}")
                    self._ui(self.wifi_ip_var.set, host)
                    save_wifi_ip(host)
                    self._refresh_device()
                    self._refresh_device_info()
                    self.log(f"WiFi ADB 已连接: {host}", "info")
                else:
                    self._ui(self.wifi_status_var.set, f"连接结果: {r1 or out[:80]}")
            except Exception as e:
                self._ui(self.wifi_status_var.set, f"连接失败: {e}")

        threading.Thread(target=work, daemon=True).start()

    def _wifi_disconnect(self):
        host = (self.wifi_ip_var.get() or "").strip()

        def work():
            try:
                if host:
                    run_adb_host(["disconnect", host], timeout=8)
                self._ui(self.wifi_status_var.set, "已断开 WiFi 连接（USB 不受影响）")
                self._refresh_device()
            except Exception as e:
                self._ui(self.wifi_status_var.set, f"断开失败: {e}")

        threading.Thread(target=work, daemon=True).start()

    def _refresh_device(self):
        def work():
            try:
                out = run_adb_text(["devices", "-l"])
                entries: list[tuple[str, str]] = []
                for ln in out.splitlines()[1:]:
                    parts = ln.strip().split()
                    if len(parts) < 2 or parts[1] != "device":
                        continue
                    serial = parts[0]
                    model = "?"
                    for p in parts:
                        if p.startswith("model:"):
                            model = p.split(":", 1)[1].replace("_", " ")
                    entries.append((serial, model))
                # 模拟器/本机端口排在后面，真机优先
                entries.sort(key=lambda e: (e[0].startswith("emulator") or e[0].startswith("127.0.0.1")))
                self.msg_q.put(("devices", entries))
            except Exception as e:
                self.msg_q.put(("devices", []))
                self.msg_q.put(("log", f"设备检测失败: {e}", "err"))

        threading.Thread(target=work, daemon=True).start()
        self._ui(self.status_var.set, "正在检测设备…")

    def _apply_selected_device(self):
        """把下拉框选中的设备设为全局目标，并记住选择。"""
        label = self.device_combo.get()
        if label not in self.device_map:
            return
        serial, model = self.device_map[label]
        global DEVICE_SERIAL
        DEVICE_SERIAL = serial
        self.status_var.set(f"设备: {model} ({serial})")
        self.log(f"目标设备: {model} ({serial})", "info")
        try:
            (Path(__file__).resolve().parent / ".last_device").write_text(serial, encoding="utf-8")
        except Exception:
            pass
        self._refresh_device_info()

    def _ui(self, fn, *args):
        """线程安全的 UI 调用：主线程直接执行，后台线程投递给主线程。"""
        if threading.current_thread() is threading.main_thread():
            fn(*args)
        else:
            self.msg_q.put(("ui", fn, args))

    def _save_shot(self):
        if self._last_frame is None:
            messagebox.showinfo("提示", "还没有截图，请先启动脚本")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".png",
            filetypes=[("PNG", "*.png"), ("JPEG", "*.jpg")],
            initialfile=f"screen_{datetime.now().strftime('%H%M%S')}.png",
        )
        if not path:
            return
        cv2.imwrite(path, self._last_frame)
        self.log(f"截图已保存: {path}")

    def _update_preview(self, frame: np.ndarray, boxes: list[OcrBox]):
        # 低延迟渲染：OCR 框约 1 秒才变一次，只重算框坐标，每帧直接画在最新帧上
        self._last_frame = frame
        h, w = frame.shape[:2]
        sig = tuple((b.text, int(b.cx), int(b.cy), bool(b.matched)) for b in boxes)
        if sig != getattr(self, "_ov_sig", None):
            rects = []
            for b in boxes:
                color = (0, 255, 0) if b.matched else (255, 200, 0)
                thickness = 3 if b.matched else 1
                x, y = int(b.cx), int(b.cy)
                rects.append((max(0, x - 40), max(0, y - 18),
                              min(w - 1, x + 120), min(h - 1, y + 18), color, thickness))
            self._ov_sig = sig
            self._ov_rects = rects
        for x0, y0, x1, y1, color, thickness in getattr(self, "_ov_rects", []):
            cv2.rectangle(frame, (x0, y0), (x1, y1), color, thickness)

        # 缩放到预览区（只在缩小时做一次插值）
        max_h = 560
        scale = min(1.0, max_h / h)
        disp_w, disp_h = int(w * scale), int(h * scale)
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB, dst=None)
        if scale != 1.0:
            rgb = cv2.resize(rgb, (disp_w, disp_h), interpolation=cv2.INTER_LINEAR)
        self._photo = ImageTk.PhotoImage(Image.fromarray(rgb))
        self.preview.configure(image=self._photo, text="")
        self.preview_info.set(f"分辨率: {w}x{h}   显示: {disp_w}x{disp_h}   OCR框: {len(boxes)}")

    def _update_ocr_table(self, boxes: list[OcrBox]):
        self.tree.delete(*self.tree.get_children())
        for b in boxes[:30]:
            hit = b.matched or "-"
            self.tree.insert(
                "", "end",
                values=(b.text[:28], f"{b.score:.2f}", f"({int(b.cx)},{int(b.cy)})", hit),
            )

    def _poll_queue(self):
        # 预览帧单独走一个"只留最新一帧"的队列，保证画面永远是当前时刻
        latest = None
        try:
            while True:
                latest = self.frame_q.get_nowait()
        except queue.Empty:
            pass
        if latest is not None:
            self._draw_n += 1
            now = time.time()
            if now - self._draw_t0 >= 1.0:
                self._draw_fps = self._draw_n / (now - self._draw_t0)
                self._draw_n = 0
                self._draw_t0 = now
            self._update_preview(latest, self._last_boxes)
            self.preview_info.set(
                self.preview_info.get() + f"   预览 {self._draw_fps:.0f}fps"
            )
        try:
            while True:
                kind = self.msg_q.get_nowait()
                if kind[0] == "log":
                    _, msg, level = kind
                    self.log(msg, level)
                elif kind[0] == "ocr":
                    _, boxes = kind
                    self._last_boxes = boxes
                    self._update_ocr_table(boxes)
                elif kind[0] == "stat":
                    _, clicks, rounds, elapsed, extra = kind
                    self.stat_var.set(
                        f"点击 {clicks} 次 · 轮次 {rounds} · 用时 {elapsed:.0f}s · {extra}"
                    )
                elif kind[0] == "ui":
                    _, fn, args = kind
                    try:
                        fn(*args)
                    except Exception:
                        pass
                elif kind[0] == "devices":
                    _, entries = kind
                    if not entries:
                        self.device_combo["values"] = []
                        self.device_map = {}
                        self.status_var.set("未连接设备")
                        self.device_var.set("设备信息: 未检测到已授权设备（检查 USB 调试 / adb connect）")
                        self.log("未检测到已授权设备", "err")
                    else:
                        values = [f"{model} · {serial}" for serial, model in entries]
                        self.device_map = {f"{model} · {serial}": (serial, model) for serial, model in entries}
                        self.device_combo["values"] = values
                        cur = self.device_combo.get()
                        if cur not in values:
                            # 优先沿用上次选择的设备
                            saved = ""
                            try:
                                saved = (Path(__file__).resolve().parent / ".last_device").read_text(encoding="utf-8").strip()
                            except Exception:
                                pass
                            target = next((v for v in values if v.endswith(saved)), values[0]) if saved else values[0]
                            self.device_combo.set(target)
                        self._apply_selected_device()
                elif kind[0] == "device_info":
                    info = kind[1]
                    if "error" in info:
                        self.device_var.set(f"设备信息: {info['error']}")
                    else:
                        self.device_var.set(
                            f"机型: {info.get('brand','?')} {info.get('model','?')}  ·  Android {info.get('android','?')}\n"
                            f"分辨率: {info.get('resolution','?')}  ·  电量: {info.get('battery','?')} ({info.get('charging','?')})\n"
                            f"网络: {info.get('network','?')}  ·  IP: {info.get('ip','?')}\n"
                            f"WiFi: {info.get('wifi','?')}"
                        )
                elif kind[0] == "stopped":
                    self.running = False
                    self.btn_start.configure(state="normal")
                    self.btn_stop.configure(state="disabled")
                    self.status_var.set("已停止")
        except queue.Empty:
            pass
        self.after(15, self._poll_queue)

    # ---------- 控制 ----------
    def _autoconnect_once(self):
        """启动时用记住的 WiFi IP 自动连一次，免去每次重新输入。"""
        host = load_wifi_ip()
        if not host:
            return

        def work():
            try:
                out = run_adb_text(["devices"])
                if any(host.split(":")[0] in ln and "device" in ln for ln in out.splitlines()):
                    self._ui(self.wifi_status_var.set, f"已连接 {host}")
                    self._refresh_device()
                    return
                self._ui(self.wifi_status_var.set, f"正在自动连接 {host} …")
                r = run_adb_host(["connect", host], timeout=12)
                time.sleep(1.2)
                out = run_adb_text(["devices"])
                if not any(host.split(":")[0] in ln and "device" in ln for ln in out.splitlines()):
                    # 华为掉线后常见的 No route to host，需要重启 adb server
                    run_adb_host(["disconnect", host], timeout=8)
                    run_adb_host(["kill-server"], timeout=10)
                    run_adb_host(["start-server"], timeout=12)
                    time.sleep(1.5)
                    r = run_adb_host(["connect", host], timeout=12) or r
                    time.sleep(1.2)
                    out = run_adb_text(["devices"])
                if any(host.split(":")[0] in ln and "device" in ln for ln in out.splitlines()):
                    self._ui(self.wifi_status_var.set, f"已自动连接 {host}")
                    self.log(f"启动时自动连接 WiFi ADB: {host}", "info")
                    self._refresh_device()
                else:
                    self._ui(self.wifi_status_var.set, f"自动连接未成功：{(r or '')[:60]}，可点「连接」重试")
            except Exception as e:
                self._ui(self.wifi_status_var.set, f"自动连接失败: {e}")

        threading.Thread(target=work, daemon=True).start()

    def _autostart_once(self):
        """窗口打开后自动开始挂机（按钮仍可随时停止/再启动）。"""
        try:
            if not self.running:
                self.start_bot()
        except Exception:
            pass

    def start_bot(self):
        if self.running:
            return
        self.stop_event.clear()
        self.running = True
        self.clicks = 0
        self.rounds = 0
        self.started_at = time.time()
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.status_var.set("运行中…")
        if self._is_kuaishou():
            self.log("启动【快手·自动刷作品】（实时影像）", "info")
            self.worker = threading.Thread(target=self._ks_worker_loop, daemon=True)
        else:
            self.log("启动【汽水音乐·去广告】（实时影像）", "info")
            self.worker = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker.start()

    def stop_bot(self):
        self.stop_event.set()
        self.status_var.set("正在停止…")
        self.log("收到停止指令", "warn")

    def _ks_worker_loop(self):
        """快手模式的常驻守护：单轮异常自动重连重试。"""
        import sys as _sys
        import kuaishou_bot
        G = _sys.modules[__name__]
        while not self.stop_event.is_set():
            try:
                watch = float(self.ks_watch_var.get() or 25)
            except Exception:
                watch = 25.0
            try:
                wphase = float(self.ks_watch_phase_var.get() or 60)
                rphase = float(self.ks_rest_phase_var.get() or 10)
            except Exception:
                wphase, rphase = 60.0, 10.0
            try:
                kuaishou_bot.run(self, G, watch_seconds=watch,
                                 watch_phase_min=wphase,
                                 claim_coins=bool(self.ks_claim_var.get()),
                                 rest_phase_min=rphase)
            except Exception as e:
                self.msg_q.put(("log", f"快手运行器异常: {type(e).__name__}: {e}，5 秒后自动重试", "err"))
            if self.stop_event.is_set():
                break
            self.msg_q.put(("log", "快手模式：5 秒后自动重连继续", "warn"))
            for _ in range(50):
                if self.stop_event.is_set():
                    break
                time.sleep(0.1)

    def _worker_loop(self):
        """常驻守护：单轮失败（断线/异常/无设备）也自动重连重启，除非用户点了停止。"""
        while not self.stop_event.is_set():
            try:
                self._worker_once()
            except Exception as e:
                self.msg_q.put(("log", f"运行器异常: {type(e).__name__}: {e}，5 秒后自动重试", "err"))
            if self.stop_event.is_set():
                break
            self.msg_q.put(("log", "自动恢复：5 秒后重新连接设备并继续，无需手动重启", "warn"))
            for _ in range(50):
                if self.stop_event.is_set():
                    break
                time.sleep(0.1)

    def _worker_once(self):
        global DEVICE_SERIAL
        live: Optional[LiveScreen] = None
        try:
            self.msg_q.put(("log", f"ADB: {ADB}", "info"))
            out = run_adb_text(["devices"])
            lines = [ln.strip() for ln in out.splitlines()[1:] if ln.strip()]
            ready = [ln.split()[0] for ln in lines if ln.endswith("device")]
            if not ready:
                host = load_wifi_ip()
                if host:
                    self.msg_q.put(("log", f"未发现设备，尝试连接记忆中的 {host}", "warn"))
                    run_adb_host(["connect", host], timeout=12)
                    time.sleep(1.5)
                    out = run_adb_text(["devices"])
                    lines = [ln.strip() for ln in out.splitlines()[1:] if ln.strip()]
                    ready = [ln.split()[0] for ln in lines if ln.endswith("device")]
            if not ready:
                self.msg_q.put(("log", "未检测到已授权设备，请开启 USB 调试", "err"))
                self.msg_q.put(("stopped",))
                return
            DEVICE_SERIAL = DEVICE_SERIAL if DEVICE_SERIAL in ready else ready[0]
            if len(ready) > 1:
                self.msg_q.put(("log", f"多设备（{len(ready)} 台），使用 {DEVICE_SERIAL}", "warn"))
            self.msg_q.put(("log", f"目标设备 {DEVICE_SERIAL}", "info"))

            dev_w, dev_h = get_device_size()
            global SCREEN_W, SCREEN_H
            SCREEN_W, SCREEN_H = dev_w, dev_h
            # 流分辨率保持真机宽高比，避免坐标系/ROI 被拉变形
            scale = 0.5 if dev_w >= 1000 else 1.0
            stream_w, stream_h = int(dev_w * scale) // 2 * 2, int(dev_h * scale) // 2 * 2
            self.msg_q.put(("log", f"真机分辨率 {dev_w}x{dev_h} · 影像流 {stream_w}x{stream_h}", "info"))

            self.msg_q.put(("log", "加载 OCR 模型…", "info"))
            get_ocr()
            self.msg_q.put(("log", "OCR 就绪，启动实时影像流", "info"))
            # 尽量保持设备不休眠（USB 供电时）
            try:
                run_adb_text(["shell", "svc", "power", "stayon", "true"])
            except Exception:
                pass

            live = LiveScreen(
                ADB,
                DEVICE_SERIAL,
                size=f"{stream_w}x{stream_h}",
                on_log=lambda s: self.msg_q.put(("log", s, "info")),
                screen_off=bool(self.screen_off_var.get()),
            )
            live.start()

            # 预览单独开一条线程，30fps 推最新帧，不受 OCR（每次约 0.5 秒）阻塞
            def _preview_loop():
                last_seq = -1
                while not self.stop_event.is_set():
                    try:
                        seq = live.latest_seq()
                        if seq != last_seq:
                            frame = live.latest()
                            if frame is not None:
                                if self.frame_q.full():
                                    try:
                                        self.frame_q.get_nowait()
                                    except queue.Empty:
                                        pass
                                try:
                                    self.frame_q.put_nowait(frame)
                                except queue.Full:
                                    pass
                                last_seq = seq
                        time.sleep(0.012)
                    except Exception:
                        time.sleep(0.05)

            threading.Thread(target=_preview_loop, daemon=True).start()

            miss = 0
            last_seq = -1
            last_click: dict[tuple, float] = {}
            ocr_gap = 0.0
            progress_t = time.time()   # 最后一次"有进展"的时间（点击/广告/识别到目标）
            close_since: Optional[float] = None   # 「领取成功 ×」出现的时间，停留几秒再点，避免误点
            stuck_n = 0                            # 连续卡住次数，防止在广告页无限等待
            last_cd: Optional[int] = None           # 上一轮倒计时数字，只有变小才算进展
            while not self.stop_event.is_set():
                frame = live.latest()
                if frame is None:
                    if int(time.time()) % 3 == 0:
                        self.msg_q.put(("log", f"等待实时影像 mode={live.mode}", "info"))
                    time.sleep(0.15)
                    continue

                now = time.time()
                # OCR 节流：约每 1 秒识别一次
                if now - ocr_gap < 0.9:
                    time.sleep(0.05)
                    continue
                ocr_gap = now

                # 前台守卫：一旦被广告落地页/应用市场带出去，立刻返回并拉回汽水音乐
                front = qishui_in_front()
                if front is False:
                    self.msg_q.put(("log", "已离开汽水音乐（误触广告落地页/应用市场），按返回并拉回", "warn"))
                    press_back()
                    time.sleep(0.9)
                    press_back()
                    time.sleep(0.6)
                    relaunch_qishui()
                    time.sleep(2.0)
                    progress_t = time.time()
                    miss = 0
                    continue

                boxes = run_ocr(frame)
                sh, sw = frame.shape[:2]
                focus = filter_focus(boxes, sw, sh)
                self.msg_q.put(("ocr", focus))
                self.rounds += 1

                # 广告结束页：右上角出现「领取成功 ×」，说明奖励已到账，点叉继续下一个
                close_box = find_ad_close(boxes, sw, sh)
                # 弹窗里有「领取奖励/领取」时先领奖，右上角 × 被弹窗挡着，点了也没反应
                has_claim_btn = any(
                    (normalize(b.text).startswith("领取")
                     and len(normalize(b.text)) <= 6
                     and "×" not in b.text and "✕" not in b.text)
                    for b in boxes
                )
                if has_claim_btn:
                    close_box = None
                if close_box is not None:
                    now2 = time.time()
                    if close_since is None:
                        close_since = now2
                    elif now2 - close_since >= 3.0:
                        cx, cy = map_to_device(int(close_box.cx), int(close_box.cy), sw, sh, dev_w, dev_h)
                        self.msg_q.put(("log", f"广告已结束，点击右上角关闭 ×（{close_box.text}）@({cx},{cy})", "click"))
                        tap(cx, cy)
                        close_since = None
                        progress_t = time.time()
                        last_click[("关闭×", cx, cy)] = time.time()
                        time.sleep(1.2)
                        continue
                else:
                    close_since = None

                if is_live_room(boxes, sh):
                    self.msg_q.put(("log", "识别到直播间，按返回退出", "warn"))
                    press_back()
                    miss = 0
                    time.sleep(1.2)
                    state = qishui_in_front()
                    if state is False:
                        self.msg_q.put(("log", "返回后已离开汽水音乐前台，重新拉回", "warn"))
                        relaunch_qishui()
                        time.sleep(1.8)
                        progress_t = time.time()
                        continue
                    # 还在直播间就再退一次，仍不行则强制回主页
                    f2 = live.latest()
                    if f2 is not None:
                        b2 = run_ocr(f2)
                        if is_live_room(b2, f2.shape[0]):
                            self.msg_q.put(("log", "仍在直播间，再返回并回主页", "warn"))
                            press_back()
                            time.sleep(1.2)
                            relaunch_qishui()
                            time.sleep(1.8)
                    progress_t = time.time()
                    continue

                in_ad = is_in_ad(boxes)
                cd = parse_countdown_seconds(boxes)

                # 屏幕上有模态弹窗（再看一个视频/继续看30秒/坚持退出）时不能只等倒计时，
                # 否则倒计时永远不往前走，必须交给下面的点击逻辑去点「领取奖励 / 继续观看」
                has_modal = any(
                    ("再看一个视频" in normalize(b.text))
                    or ("坚持退出" in normalize(b.text))
                    or ("继续看" in normalize(b.text) and "秒" in normalize(b.text))
                    for b in boxes
                )

                if in_ad and cd is not None and cd >= 1 and not has_modal:
                    progress_t = time.time()
                    # 按倒计时休眠，结束后立刻探查「领取奖励」
                    wait = max(0.3, min(cd - 0.2, 25.0))
                    self.msg_q.put(("log", f"广告倒计时 {cd}s，等待 {wait:.1f}s 后探查领取奖励", "info"))
                    time.sleep(wait)
                    continue

                picked = pick_click(boxes, sw, sh)
                if picked:
                    target, box, score = picked
                    fx, fy = int(box.cx), int(box.cy)
                    # 影像帧坐标 → 真机坐标（帧分辨率可能被缩放）
                    x, y = map_to_device(fx, fy, sw, sh, dev_w, dev_h)
                    # 同点防抖：2 秒内不重复点同一按钮
                    now = time.time()
                    key = (target, x, y)
                    if key in last_click and now - last_click[key] < (5.0 if target in AD_ENTRY_TARGETS else 2.0):
                        self.msg_q.put(("log", f"跳过重复点击「{target}」@({x},{y})", "warn"))
                        time.sleep(0.4)
                        continue
                    last_click[key] = now
                    self.clicks += 1
                    self.msg_q.put(
                        ("log",
                         f"点击「{target}」 score={score:.2f} text={box.text!r} "
                         f"帧({fx},{fy})→机({x},{y})",
                         "click")
                    )
                    try:
                        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
                        vis = frame.copy()
                        cv2.circle(vis, (fx, fy), 28, (0, 0, 255), 3)
                        cv2.imwrite(str(DEBUG_DIR / f"{datetime.now().strftime('%H%M%S')}_{target}.jpg"), vis)
                    except Exception:
                        pass
                    tap(x, y)
                    miss = 0
                    stuck_n = 0
                    progress_t = time.time()
                    if target in NO_COOLDOWN_TARGETS:
                        self.msg_q.put(("stat", self.clicks, self.rounds,
                                        time.time() - self.started_at, f"{target} 已点"))
                        continue
                    human_sleep(ENTRY_COOLDOWN if target in AD_ENTRY_TARGETS else CLICK_COOLDOWN, 0.5)
                else:
                    tag = "广告中" if in_ad else "待机"
                    preview = " | ".join(normalize(b.text) for b in focus[:8]) or "（无重点文字）"
                    extra = ""
                    if getattr(pick_click, "last_rejects", None):
                        extra = " · 拒绝: " + "; ".join(pick_click.last_rejects[:3])
                    # 兜底：页面上有个孤零零的圆形 ×（弹窗的取消/关闭），直接点它继续
                    cancel_box = None
                    for b in boxes:
                        raw = (b.text or "").strip()
                        t = normalize(raw)
                        if len(raw) > 4:
                            continue
                        if ("×" in raw or "✕" in raw or "✖" in raw or raw.upper() == "X") \
                                and sh * 0.30 < b.cy < sh * 0.90:
                            cancel_box = b
                            break
                    if cancel_box is not None:
                        cx2, cy2 = map_to_device(int(cancel_box.cx), int(cancel_box.cy), sw, sh, dev_w, dev_h)
                        key2 = ("取消×", cx2, cy2)
                        if time.time() - last_click.get(key2, 0) > 4.0:
                            last_click[key2] = time.time()
                            self.msg_q.put(("log", f"未识别到按钮，点弹窗取消 × @({cx2},{cy2})", "warn"))
                            tap(cx2, cy2)
                            progress_t = time.time()
                            miss = 0
                            time.sleep(1.2)
                            continue

                    self.msg_q.put(("log", f"未命中[{tag}] {preview[:80]}{extra}", "info"))
                    human_sleep(POLL_INTERVAL, 0.4)
                    if in_ad:
                        miss = 0
                        # 只有倒计时真的在走才算"有进展"，否则弹窗卡死时会被无限重置
                        if cd is not None and cd < (last_cd if last_cd is not None else 999):
                            last_cd = cd
                            progress_t = time.time()
                    else:
                        miss += 1
                        # 卡住兜底：长时间没有任何进展（直播间/弹窗/白页），主动退一层再回主页
                        if now - progress_t > 60:
                            # ===== 全局自愈：一路降级重试，绝不干等 =====
                            self.msg_q.put(("log", "60s 无进展，触发自愈：先试着领奖/关弹窗，再返回重开", "warn"))
                            acted = False
                            for b in boxes:
                                t = normalize(b.text)
                                raw = b.text or ""
                                if any(k in t for k in CLICK_BLACKLIST) or ("×" in raw):
                                    continue
                                if len(t) <= 12 and (t.startswith("领取") or t.startswith("开心") or t.startswith("继续观看")
                                                     or t.startswith("广告膨胀") or "开宝箱" in t):
                                    ax, ay = map_to_device(int(b.cx), int(b.cy), sw, sh, dev_w, dev_h)
                                    self.msg_q.put(("log", f"自愈点击「{b.text}」@({ax},{ay})", "warn"))
                                    tap(ax, ay)
                                    self.clicks += 1
                                    acted = True
                                    break
                            if not acted:
                                press_back()
                                time.sleep(1.0)
                                press_back()
                                time.sleep(0.8)
                                relaunch_qishui()
                                time.sleep(2.2)
                            progress_t = time.time()
                            last_cd = None
                            stuck_n = 0
                            miss = 0
                            continue

                        if now - progress_t > STUCK_SECONDS:
                            self.msg_q.put(("log",
                                            f"{STUCK_SECONDS}s 无进展（疑似直播间/广告弹窗卡住），尝试退出",
                                            "warn"))
                            # 广告页里常有「坚持退出 / 退出广告」按钮，优先点它（比按返回温和）
                            exit_box = None
                            for b in boxes:
                                t = normalize(b.text)
                                if any(k in t for k in ("坚持退出", "退出广告", "关闭广告", "跳过广告", "跳过")):
                                    exit_box = b
                                    break
                            if exit_box is not None:
                                ex, ey = map_to_device(int(exit_box.cx), int(exit_box.cy), sw, sh, dev_w, dev_h)
                                self.msg_q.put(("log", f"点击退出按钮「{exit_box.text}」@({ex},{ey})", "warn"))
                                tap(ex, ey)
                                time.sleep(1.2)
                            elif in_ad_activity() and stuck_n < 2:
                                # 广告页先等两轮（可能是长视频/加载中），再不动就按返回
                                stuck_n += 1
                                self.msg_q.put(("log", f"仍在广告页且无退出按钮，继续等待（{stuck_n}/2）", "warn"))
                                time.sleep(1.5)
                                progress_t = time.time()
                                continue
                            else:
                                entry_box = None
                                for b in boxes:
                                    t = normalize(b.text)
                                    if len(t) > 10:
                                        continue
                                    if any(k in t for k in AD_ENTRY_TARGETS) or ("广告膨胀" in t):
                                        entry_box = b
                                        break
                                if entry_box is not None:
                                    ex, ey = map_to_device(int(entry_box.cx), int(entry_box.cy), sw, sh, dev_w, dev_h)
                                    self.msg_q.put(("log", f"卡住不动，点击广告入口「{entry_box.text}」@({ex},{ey})", "click"))
                                    tap(ex, ey)
                                    self.clicks += 1
                                    time.sleep(1.5)
                                    progress_t = time.time()
                                    miss = 0
                                    continue
                                press_back()
                                time.sleep(1.0)
                                state = qishui_in_front()
                                if state is False:
                                    relaunch_qishui()
                                    time.sleep(1.8)
                                else:
                                    f3 = live.latest()
                                    b3 = run_ocr(f3 if f3 is not None else frame)
                                    if is_live_room(b3, sh):
                                        self.msg_q.put(("log", "仍停留在直播间，强制回主页", "warn"))
                                        relaunch_qishui()
                                        time.sleep(1.8)
                            progress_t = time.time()
                            miss = 0
                            continue
                        if miss >= MISS_BACK_THRESHOLD:
                            # 不盲按返回（华为上按一次就把 App 退到后台），
                            # 只检查是否还在汽水音乐前台，不在才拉回
                            state = qishui_in_front()
                            if state is True:
                                self.msg_q.put(("log", f"连续 {miss} 次未命中，仍在汽水音乐前台，继续等待", "info"))
                            elif state is False:
                                self.msg_q.put(("log", f"连续 {miss} 次未命中且已离开前台，重新拉回汽水音乐", "warn"))
                                relaunch_qishui()
                                time.sleep(1.5)
                            else:
                                self.msg_q.put(("log", f"连续 {miss} 次未命中，前台状态未知（设备离线？），不做返回", "warn"))
                            miss = 0

                self.msg_q.put((
                    "stat", self.clicks, self.rounds, time.time() - self.started_at,
                    f"{live.mode}/{live.fps:.1f}fps · " + ("广告等待中" if in_ad else "识别中"),
                ))

        except Exception as e:
            self.msg_q.put(("log", f"工作线程异常: {type(e).__name__}: {e}", "err"))
        finally:
            if live is not None:
                try:
                    live.stop()
                except Exception:
                    pass
            if self.stop_event.is_set():
                self.msg_q.put(("stopped",))


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
