# -*- coding: utf-8 -*-
"""
实时屏幕影像源

优先：scrcpy + Windows 命名管道 + PyAV 解码 H.264（约 30 FPS）
回退：RAW screencap 连续抓帧（约 1.5 FPS，无 scrcpy/失败时用）
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import cv2
import numpy as np

# 注意：PyAV(av) 和 OpenCV 各自带一份 libav*，同时加载会重复符号并偶发 SIGSEGV。
# 所以这里只探测是否可用，真正使用时再在函数内部 import；macOS 走 ffmpeg 子进程解码，不碰 PyAV。
try:
    import importlib.util

    HAVE_AV = importlib.util.find_spec("av") is not None
except Exception:  # pragma: no cover
    HAVE_AV = False


def _load_av():
    import av  # type: ignore

    return av

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
PIPE_NAME = "\\\\.\\pipe\\qishui_scrspy_live"
# scrcpy 服务端参数（macOS/Linux 走 socket 转发，不需要 Windows 命名管道）
SCRCPY_PORT = 27183
SCRCPY_JAR_REMOTE = "/data/local/tmp/scrcpy-server.jar"


def _popen_flags() -> dict:
    return {"creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {}


def find_scrcpy_server(start_dir: Optional[Path] = None) -> Optional[Path]:
    """定位 scrcpy 服务端 jar（macOS/Linux 手动启动 scrcpy-server 时需要）。"""
    bases = []
    if start_dir:
        bases.append(Path(start_dir))
    bases.append(app_dir())
    # 常见安装位置：~/.local/opt/scrcpy、homebrew、PATH 中 scrcpy 同目录
    cands = [
        Path.home() / ".local" / "opt" / "scrcpy" / "scrcpy-server",
        Path.home() / ".local" / "share" / "scrcpy" / "scrcpy-server",
        Path("/opt/homebrew/share/scrcpy/scrcpy-server"),
        Path("/usr/local/share/scrcpy/scrcpy-server"),
        Path("/usr/share/scrcpy/scrcpy-server"),
    ]
    for base in bases:
        cands += [base / "scrcpy-server", base / "scrcpy" / "scrcpy-server"]
    try:
        import shutil

        which = shutil.which("scrcpy")
        if which:
            p = Path(which).resolve()
            cands += [p.parent / "scrcpy-server", p.parent / "scrcpy-server.jar"]
    except Exception:
        pass
    for p in cands:
        try:
            if p.is_file() and p.stat().st_size > 100_000:
                return p
        except Exception:
            continue
    return None


def find_ffmpeg() -> Optional[str]:
    """找 ffmpeg 可执行文件（独立进程解码，避免库冲突）。"""
    import shutil

    cands = []
    try:
        w = shutil.which("ffmpeg")
        if w:
            cands.append(w)
    except Exception:
        pass
    cands += [
        str(Path.home() / ".local" / "bin" / "ffmpeg"),
        "/opt/homebrew/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        "/usr/bin/ffmpeg",
    ]
    for c in cands:
        try:
            if Path(c).is_file() and os.access(c, os.X_OK):
                return c
        except Exception:
            continue
    return None


def scrcpy_version(scrcpy_path: Optional[Path]) -> str:
    """读取本机 scrcpy 版本号，用于与服务端 jar 匹配。"""
    if scrcpy_path:
        try:
            r = subprocess.run(
                [str(scrcpy_path), "--version"], capture_output=True, text=True, timeout=10
            )
            import re

            m = re.search(r"scrcpy\s+(\d+\.\d+(?:\.\d+)?)", (r.stdout or "") + (r.stderr or ""))
            if m:
                return m.group(1)
        except Exception:
            pass
    return "5.0.1"


class _SockReader:
    """给 PyAV 当 file-like，从 socket 持续读 H.264 字节。"""

    def __init__(self, sock, initial: bytes = b""):
        self.sock = sock
        self.initial = initial
        self.closed = False

    def read(self, n: int = -1) -> bytes:
        if self.closed:
            return b""
        if self.initial:
            data, self.initial = self.initial, b""
            return data
        try:
            return self.sock.recv(65536)
        except Exception:
            return b""

    def seek(self, *a, **k):
        return 0

    def tell(self):
        return 0

    def close(self):
        self.closed = True
        try:
            self.sock.close()
        except Exception:
            pass


def app_dir() -> Path:
    """运行目录：exe 所在目录（打包后），或源码目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def find_scrcpy(start_dir: Optional[Path] = None) -> Optional[Path]:
    bases = []
    if start_dir:
        bases.append(Path(start_dir))
    bases.append(app_dir())
    # 源码调试时可能在上一级
    bases.append(app_dir().parent)
    bases.append(Path.cwd())
    tried = []
    for base in bases:
        for sub in (
            Path("scrcpy") / "scrcpy.exe",
            Path("dist") / "scrcpy" / "scrcpy.exe",
            Path("scrcpy.exe"),
        ):
            p = base / sub
            tried.append(str(p))
            try:
                if p.is_file():
                    return p
            except Exception:
                pass
    # 记录搜索路径便于排查
    find_scrcpy.tried = tried  # type: ignore
    return None


class _PipeReader:
    """给 PyAV 当 file-like，从命名管道持续读字节。"""

    def __init__(self, handle):
        self.handle = handle
        self.closed = False

    def read(self, n: int = -1) -> bytes:
        if self.closed:
            return b""
        if n is None or n < 0:
            n = 65536
        try:
            import win32file

            _hr, data = win32file.ReadFile(self.handle, max(int(n), 1))
            return data or b""
        except Exception:
            return b""

    def seek(self, *a, **k):
        return 0

    def tell(self):
        return 0

    def close(self):
        self.closed = True


class LiveScreen:
    def __init__(
        self,
        adb: str,
        serial: Optional[str] = None,
        size: str = "",
        on_log: Optional[Callable[[str], None]] = None,
        scrcpy_path: Optional[str] = None,
        screen_off: bool = False,
    ):
        self.adb = adb
        self.serial = serial
        self.size = size
        self.on_log = on_log or (lambda s: None)
        self.scrcpy_path = Path(scrcpy_path) if scrcpy_path else find_scrcpy()
        self.screen_off = screen_off
        self._lock = threading.Lock()
        self._frame: Optional[np.ndarray] = None
        self._seq = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.mode = "idle"
        self.fps = 0.0
        self._fps_n = 0
        self._fps_t0 = time.time()

    def _adb_args(self, args: list[str]) -> list[str]:
        if self.serial:
            return [self.adb, "-s", self.serial, *args]
        return [self.adb, *args]

    def _log(self, msg: str) -> None:
        try:
            self.on_log(msg)
        except Exception:
            pass

    def latest(self) -> Optional[np.ndarray]:
        with self._lock:
            return None if self._frame is None else self._frame.copy()

    def latest_seq(self) -> int:
        return self._seq

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=4)
            self._thread = None
        self.mode = "idle"

    def _put(self, frame: np.ndarray) -> None:
        with self._lock:
            self._frame = frame
            self._seq += 1
        self._fps_n += 1
        now = time.time()
        dt = now - self._fps_t0
        if dt >= 1.0:
            self.fps = self._fps_n / dt
            self._fps_t0 = now
            self._fps_n = 0

    def _run(self) -> None:
        fps_t0 = time.time()
        fps_n = 0
        fail_n = 0
        while not self._stop.is_set():
            n = 0
            try:
                if self.scrcpy_path and os.name == "nt":
                    n = self._run_scrcpy()
                    fps_n += n
                    if n == 0:
                        time.sleep(1.0)  # scrcpy 秒退时避免忙等
                elif os.name != "nt" and find_scrcpy_server() and find_ffmpeg():
                    n = self._run_scrcpy_server()
                    fps_n += n
                    if n == 0:
                        fail_n += 1
                        wait = min(8.0, 1.5 * fail_n)
                        self._log(f"影像流中断（第 {fail_n} 次），{wait:.0f}s 后重连")
                        time.sleep(wait)
                    else:
                        fail_n = 0
                elif HAVE_AV and sys.platform.startswith("linux"):
                    n = self._run_h264()
                    fps_n += n
                    if n == 0:
                        time.sleep(1.0)
                else:
                    self._log("使用截图模式（未找到 scrcpy-server 或 PyAV）")
                    fps_n += self._run_screencap_loop()
            except Exception as e:
                self._log(f"影像源异常: {type(e).__name__}: {e}")
                time.sleep(1.0)
            if self._stop.is_set():
                break
            if n > 0:
                # 本轮流正常结束（如 5 分钟重连），直接进入下一轮，不插截图以免画面卡顿
                now = time.time()
                if now - fps_t0 >= 2.0 and fps_n:
                    self.fps = fps_n / (now - fps_t0)
                    fps_t0, fps_n = now, 0
                continue
            # 流启动失败时短暂回退截图，避免完全黑屏
            try:
                self.mode = "screencap"
                for _ in range(3):
                    if self._stop.is_set():
                        break
                    if self._capture_once():
                        fps_n += 1
                    time.sleep(0.2)
            except Exception as e:
                self._log(f"截图失败: {e}")
                time.sleep(1.0)
            now = time.time()
            if now - fps_t0 >= 2.0 and fps_n:
                self.fps = fps_n / (now - fps_t0)
                fps_t0, fps_n = now, 0

    def _ensure_device(self) -> bool:
        """Wi-Fi ADB 掉线时自动重连（挂机稳定性的关键）。

        华为机 Wi-Fi 掉线后常见 ``offline`` / ``No route to host``，
        单纯 adb connect 恢复不了，必须重启 adb server 才行。
        """
        if not self.serial:
            return True

        def state() -> str:
            try:
                st = subprocess.run(
                    [self.adb, "-s", self.serial, "get-state"],
                    capture_output=True, text=True, timeout=10,
                )
                return (st.stdout or st.stderr or "").strip()
            except Exception:
                return "timeout"

        if state() == "device":
            return True
        if ":" not in self.serial:
            return False

        # 第 1 步：断开旧条目再连
        try:
            subprocess.run([self.adb, "disconnect", self.serial], capture_output=True, timeout=10)
            time.sleep(0.8)
            r = subprocess.run([self.adb, "connect", self.serial], capture_output=True, text=True, timeout=20)
            self._log(f"设备掉线，重连：{(r.stdout or r.stderr or '').strip()[:70]}")
            time.sleep(1.2)
            if state() == "device":
                return True
        except Exception:
            pass

        # 第 2 步：重启 adb server 再连（Wi-Fi 掉线后基本靠这步恢复）
        try:
            self._log("重连失败，重启 adb 服务后再次连接")
            subprocess.run([self.adb, "kill-server"], capture_output=True, timeout=15)
            subprocess.run([self.adb, "start-server"], capture_output=True, timeout=20)
            time.sleep(1.5)
            subprocess.run([self.adb, "connect", self.serial], capture_output=True, text=True, timeout=20)
            time.sleep(1.5)
            if state() == "device":
                self._log("设备已重新连接")
                return True
        except Exception:
            pass
        self._log("设备暂不可达（检查手机是否锁屏/断网，已保持自动重试）")
        return False

    # ---------------- scrcpy server（macOS/Linux：socket + 裸 H.264） ----------------
    def _scrcpy_max_size(self) -> int:
        side = 720
        try:
            if self.size and "x" in self.size.lower():
                a, b = self.size.lower().split("x", 1)
                side = max(int(a), int(b))
        except Exception:
            pass
        return max(320, min(1280, side))

    def _run_scrcpy_server(self) -> int:
        """adb forward + app_process 启动 scrcpy-server，直接从 socket 解裸 H.264。

        macOS/Linux 没有 Windows 命名管道，scrcpy 官方提交只能走 --record 文件；
        这里改用手动启动服务端 + 端口转发，延迟约 1 帧（~30fps）。
        """
        import socket as _socket

        if not self._ensure_device():
            return 0
        jar = find_scrcpy_server()
        if not jar:
            self._log("未找到 scrcpy-server，改用截图模式")
            return 0
        ver = scrcpy_version(self.scrcpy_path)
        max_size = self._scrcpy_max_size()

        def adb(*args, timeout=25):
            return subprocess.run(
                self._adb_args(list(args)), capture_output=True, text=True, timeout=timeout
            )

        proc = None
        ff = None
        sock = None
        frames = 0
        try:
            # 1) 清理设备上残留的服务端进程
            try:
                ps = adb("shell", "ps -A -o PID,CMD").stdout or ""
                for line in ps.splitlines():
                    if "scrcpy" in line:
                        pid = line.strip().split(" ", 1)[0]
                        if pid.isdigit():
                            adb("shell", "kill -9 " + pid)
            except Exception:
                pass
            # 2) 推服务端 jar（华为会自动清 /data/local/tmp，每次启动都补一次）
            adb("push", str(jar), SCRCPY_JAR_REMOTE)
            # 3) 端口转发
            adb("forward", "--remove-all")
            fwd = adb("forward", f"tcp:{SCRCPY_PORT}", "localabstract:scrcpy")
            if fwd.returncode != 0:
                self._log(f"forward 失败: {(fwd.stderr or fwd.stdout).strip()[:120]}")
                return 0
            # 4) 启动服务端（tunnel_forward + 无帧头，出裸 Annex-B H.264）
            cmd_str = (
                "CLASSPATH=%s app_process / com.genymobile.scrcpy.Server %s "
                "log_level=info tunnel_forward=true audio=false control=false "
                "send_frame_meta=false max_size=%d max_fps=60"
            ) % (SCRCPY_JAR_REMOTE, ver, max_size)
            self.mode = "scrcpy"
            self._log(f"启动 scrcpy 服务端 {ver}（{max_size}p）")
            proc = subprocess.Popen(
                self._adb_args(["shell", cmd_str]),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **_popen_flags(),
            )
            # 5) 连本地转发端口
            for _ in range(20):
                if self._stop.is_set():
                    return 0
                try:
                    sock = _socket.create_connection(("127.0.0.1", SCRCPY_PORT), timeout=2)
                    break
                except OSError:
                    sock = None
                    time.sleep(0.4)
            if sock is None:
                self._log("连接 scrcpy 服务端失败（镜像未建立）")
                return 0
            # 6) 连端口并读到真实数据：adb forward 会先接下本地连接，
            #    若设备端服务端还没就绪，连接会立刻被关闭（收到 0 字节），需要重连等它起来
            buf = b""
            for attempt in range(8):
                if self._stop.is_set():
                    return 0
                try:
                    sock = _socket.create_connection(("127.0.0.1", SCRCPY_PORT), timeout=2)
                except OSError:
                    sock = None
                    time.sleep(0.5)
                    continue
                sock.settimeout(2.0)
                try:
                    chunk = sock.recv(512)
                except Exception:
                    chunk = b""
                if chunk:
                    buf = chunk
                    break
                try:
                    sock.close()
                except Exception:
                    pass
                sock = None
                time.sleep(0.5)
            if sock is None or not buf:
                self._log("连接 scrcpy 服务端失败（设备端未出数据）")
                return 0
            # 7) 跳过头部：1 字节哨兵 + 64 字节设备名 + codec 头，找到首个 Annex-B 起始码
            sock.settimeout(8)
            while b"\x00\x00\x00\x01" not in buf and len(buf) < 4096:
                if self._stop.is_set():
                    return 0
                chunk = sock.recv(512)
                if not chunk:
                    break
                buf += chunk
            idx = buf.find(b"\x00\x00\x00\x01")
            if idx < 0:
                self._log(f"scrcpy 流未找到 H.264 起始码（收到 {len(buf)} 字节: {buf[:16].hex()}）")
                return 0
            sock.settimeout(None)
            # 7) 解码：用独立 ffmpeg 子进程（避免 PyAV 与 OpenCV 的 libav 冲突崩溃）
            try:
                sw = int.from_bytes(buf[idx - 8: idx - 4], "big")
                sh = int.from_bytes(buf[idx - 4: idx], "big")
            except Exception:
                sw = sh = 0
            if not (0 < sw < 4000 and 0 < sh < 4000):
                sw = sh = 0
            ffmpeg = find_ffmpeg()
            if not ffmpeg:
                self._log("未找到 ffmpeg，无法解码 H.264 流")
                return 0
            self._log(f"scrcpy 流头 {idx} 字节 · {sw}x{sh} · ffmpeg 解码")
            ff = subprocess.Popen(
                [ffmpeg, "-loglevel", "error",
                 "-probesize", "32", "-analyzeduration", "0", "-flags", "low_delay",
                 "-f", "h264", "-i", "pipe:0", "-f", "rawvideo", "-pix_fmt", "bgr24",
                 "pipe:1"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                bufsize=0, **_popen_flags(),
            )

            def _feed():
                """把 socket 上的 H.264 字节喂给 ffmpeg（先补上已读到的首包）。"""
                if start_bytes:
                    try:
                        ff.stdin.write(start_bytes)
                    except Exception:
                        return
                while not self._stop.is_set():
                    try:
                        data = sock.recv(65536)
                    except Exception:
                        break
                    if not data:
                        break
                    try:
                        ff.stdin.write(data)
                    except Exception:
                        break
                try:
                    ff.stdin.close()
                except Exception:
                    pass

            start_bytes = buf[idx:]
            feeder = threading.Thread(target=_feed, daemon=True)
            feeder.start()
            started = time.time()
            out = ff.stdout
            frame_bytes = sw * sh * 3

            def _watchdog():
                """画面长时间不更新（服务端僵死/网络黑洞）时主动断开重连。"""
                last_seq, last_t = self._seq, time.time()
                while not self._stop.is_set() and not self._stop.wait(2.0):
                    if self._seq != last_seq:
                        last_seq, last_t = self._seq, time.time()
                    elif time.time() - last_t > 12:
                        self._log("画面 12 秒无更新，重启影像流")
                        try:
                            sock.shutdown(2)
                        except Exception:
                            pass
                        try:
                            sock.close()
                        except Exception:
                            pass
                        return

            threading.Thread(target=_watchdog, daemon=True).start()
            while not self._stop.is_set():
                chunks, got = [], 0
                while got < frame_bytes:
                    b = out.read(frame_bytes - got)
                    if not b:
                        break
                    chunks.append(b)
                    got += len(b)
                if got < frame_bytes:
                    break
                img = np.frombuffer(b"".join(chunks), np.uint8).reshape((sh, sw, 3))
                self._put(img)
                frames += 1
                if frames == 1:
                    self._log(f"scrcpy 出帧 {sw}x{sh}")
                if time.time() - started > 1800:  # 半小时兜底重建一次
                    break
        except Exception as e:
            if not self._stop.is_set():
                self._log(f"scrcpy 服务端异常: {type(e).__name__}: {e}")
        finally:
            try:
                if sock is not None:
                    sock.close()
            except Exception:
                pass
            try:
                if ff is not None:
                    ff.kill()
            except Exception:
                pass
            try:
                if proc is not None:
                    proc.kill()
            except Exception:
                pass
            try:
                adb("forward", "--remove-all", timeout=10)
            except Exception:
                pass
        return frames

    # ---------------- scrcpy H.264 ----------------
    def _run_scrcpy(self) -> int:
        try:
            import win32file
            import win32pipe
        except ImportError:
            self._log("缺少 pywin32，无法使用 scrcpy 管道")
            return self._run_screencap_loop()

        self.mode = "scrcpy"
        self._log(f"启动 scrcpy 实时流 ({self.scrcpy_path.name})")
        pipe = win32pipe.CreateNamedPipe(
            PIPE_NAME,
            win32pipe.PIPE_ACCESS_INBOUND,
            win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
            1,
            1 << 22,
            1 << 22,
            0,
            None,
        )
        cmd = [
            str(self.scrcpy_path),
            "--no-window",
            f"--record={PIPE_NAME}",
            "--record-format=mkv",
            "--max-fps=30",
            "--video-bit-rate=8M",
            "--stay-awake",
        ]
        # 限制分辨率（格式 WxH，保持宽高比）降低带宽
        if self.size and "x" in self.size.lower():
            try:
                w_s, h_s = self.size.lower().split("x", 1)
                max_side = max(int(w_s), int(h_s))
                if max_side > 0:
                    cmd += [f"--max-size={max_side}"]
            except Exception:
                pass
        # 熄屏运行：手机屏幕关掉，但仍推流给本程序（scrcpy 官方能力）
        if self.screen_off:
            cmd.append("--turn-screen-off")
            cmd.append("--no-power-on")
        if self.serial:
            cmd[1:1] = ["-s", self.serial]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **_popen_flags())
        frames = 0
        started = time.time()
        box = None
        try:
            win32pipe.ConnectNamedPipe(pipe, None)
            self._log("scrcpy 管道已连接")
            av = _load_av()
            box = av.open(_PipeReader(pipe), format="matroska", mode="r")
            for frame in box.decode(box.streams.video[0]):
                if self._stop.is_set():
                    break
                img = frame.to_ndarray(format="bgr24")
                self._put(img)
                frames += 1
                if frames == 1:
                    self._log(f"scrcpy 出帧 {img.shape[1]}x{img.shape[0]}")
                # 一次会话最长 4 分钟后重启（防止句柄泄漏）
                if time.time() - started > 240:
                    break
            self._log(f"scrcpy 会话结束，共 {frames} 帧")
            return frames
        finally:
            try:
                if box is not None:
                    box.close()
            except Exception:
                pass
            try:
                proc.kill()
            except Exception:
                pass
            try:
                win32file.CloseHandle(pipe)
            except Exception:
                pass

    # ---------------- H.264 流（跨平台） ----------------
    def _run_h264(self) -> int:
        """用 screenrecord 输出裸 H.264，本地用 PyAV 解码（macOS/Linux 可用）。"""
        n = 0
        while not self._stop.is_set():
            cmd = ["exec-out", "screenrecord", "--output-format=h264",
                   "--bit-rate", "6M", "--time-limit", "180"]
            if self.size:
                cmd += ["--size", self.size]
            cmd.append("-")
            try:
                proc = subprocess.Popen(
                    self._adb_args(cmd),
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
                    **_popen_flags(),
                )
            except Exception as e:
                self._log(f"screenrecord 启动失败: {e}")
                return n
            self.mode = "h264"
            self._log(f"H.264 流启动 size={self.size or 'native'}")
            try:
                av = _load_av()
                container = av.open(proc.stdout, format="h264", mode="r",
                                    options={"fflags": "nobuffer", "flags": "low_delay"})
                for packet in container.demux(video=0):
                    if self._stop.is_set():
                        break
                    for frame in packet.decode():
                        if self._stop.is_set():
                            break
                        self._put(frame.to_ndarray(format="bgr24"))
                        n += 1
                container.close()
            except Exception as e:
                if not self._stop.is_set():
                    self._log(f"H.264 解码中断: {type(e).__name__}: {e}")
            finally:
                try:
                    proc.terminate()
                except Exception:
                    pass
            if not self._stop.is_set():
                time.sleep(0.3)  # 180s 时限到了自动重开
        return n

    # ---------------- 截图回退 ----------------
    def _run_screencap_loop(self) -> int:
        self.mode = "screencap"
        self._log("实时影像：RAW screencap")
        n = 0
        while not self._stop.is_set():
            t0 = time.time()
            if self._capture_once():
                n += 1
            dt = time.time() - t0
            if dt < 0.05:
                time.sleep(0.05 - dt)
        return n

    def _capture_once(self) -> bool:
        r = subprocess.run(
            self._adb_args(["exec-out", "screencap"]),
            capture_output=True, timeout=15, **_popen_flags(),
        )
        data = r.stdout or b""
        if len(data) > 16:
            frame = self._parse_raw(data)
            if frame is not None:
                self._put(frame)
                return True
        r = subprocess.run(
            self._adb_args(["exec-out", "screencap", "-p"]),
            capture_output=True, timeout=15, **_popen_flags(),
        )
        if not r.stdout:
            return False
        frame = cv2.imdecode(np.frombuffer(r.stdout, np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            return False
        self._put(frame)
        return True

    @staticmethod
    def _parse_raw(data: bytes) -> Optional[np.ndarray]:
        try:
            w, h, fmt = struct.unpack_from("<III", data, 0)
            if not (0 < w < 8000 and 0 < h < 8000):
                return None
            for offset in (12, 16):
                need = offset + w * h * 4
                if len(data) >= need:
                    pix = np.frombuffer(data, np.uint8, count=w * h * 4, offset=offset)
                    rgba = np.reshape(pix, (h, w, 4))
                    return cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGR)
        except Exception:
            return None
        return None
