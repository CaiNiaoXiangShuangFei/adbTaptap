"""本地 ADB 实时预览窗口。

优先由管理服务在未找到 scrcpy 时启动。画面和触控都直接走本机 ADB，
不经过 HTTP 和浏览器图片解码链路。
"""

from __future__ import annotations

import argparse
import io
import os
import queue
import re
import subprocess
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run_hidden(command: list[str], *, timeout: float = 8, binary: bool = False):
    return subprocess.run(
        command,
        capture_output=True,
        text=not binary,
        timeout=timeout,
        errors=None if binary else "replace",
        creationflags=CREATE_NO_WINDOW,
    )


class NativePreview:
    def __init__(self, root: tk.Tk, adb_path: str, serial: str, interval: float):
        self.root = root
        self.adb_path = os.path.abspath(adb_path)
        self.serial = serial
        self.interval = max(0.03, interval)
        self.stop_event = threading.Event()
        self.frames: queue.Queue[tuple[bytes, float] | tuple[None, str]] = queue.Queue(maxsize=1)
        self.latest_png = b""
        self.photo = None
        self.image_size = (0, 0)
        self.display_rect = (0, 0, 0, 0)
        self.drag_start = None
        self.frame_times: list[float] = []

        root.title(f"TapTap 本地实时预览 · {serial}")
        root.geometry("520x900")
        root.minsize(320, 520)
        root.configure(bg="#09111f")
        root.protocol("WM_DELETE_WINDOW", self.close)

        self._build_ui()
        self.worker = threading.Thread(target=self._capture_loop, daemon=True, name="adb-preview")
        self.worker.start()
        self.root.after(16, self._poll_frames)

    def _build_ui(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass

        top = tk.Frame(self.root, bg="#111c2f", padx=10, pady=8)
        top.pack(fill=tk.X)
        tk.Label(
            top, text=self.serial, bg="#111c2f", fg="#e7eefc",
            font=("Segoe UI", 10, "bold"), anchor="w",
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.status = tk.StringVar(value="正在连接设备…")
        tk.Label(
            top, textvariable=self.status, bg="#111c2f", fg="#91a4c5",
            font=("Segoe UI", 9), anchor="e",
        ).pack(side=tk.RIGHT)

        self.canvas = tk.Canvas(
            self.root, bg="#030712", highlightthickness=0, cursor="crosshair",
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.bind("<ButtonPress-1>", self._pointer_down)
        self.canvas.bind("<ButtonRelease-1>", self._pointer_up)
        self.canvas.bind("<Configure>", lambda _event: self._render_latest())

        controls = tk.Frame(self.root, bg="#111c2f", padx=8, pady=8)
        controls.pack(fill=tk.X)
        for label, keycode in (("返回", 4), ("主页", 3), ("最近任务", 187), ("电源", 26)):
            ttk.Button(
                controls, text=label,
                command=lambda value=keycode: self._send_key(value),
            ).pack(side=tk.LEFT, padx=3)
        ttk.Button(controls, text="保存截图", command=self._save_snapshot).pack(side=tk.RIGHT, padx=3)

        hint = tk.Label(
            self.root,
            text="点击画面=手机点击 · 拖动画面=手机滑动 · Ctrl+W=关闭窗口",
            bg="#09111f", fg="#7283a2", font=("Segoe UI", 9), pady=5,
        )
        hint.pack(fill=tk.X)
        self.root.bind("<Control-w>", lambda _event: self.close())

    def _adb_command(self, *args: str, timeout: float = 8, binary: bool = False):
        return run_hidden(
            [self.adb_path, "-s", self.serial, *map(str, args)],
            timeout=timeout,
            binary=binary,
        )

    def _capture_once(self) -> tuple[bytes | None, str]:
        try:
            result = self._adb_command("exec-out", "screencap", "-p", timeout=8, binary=True)
        except subprocess.TimeoutExpired:
            return None, "截图超时"
        except OSError as exc:
            return None, f"ADB 启动失败：{exc}"
        data = result.stdout or b""
        if result.returncode == 0 and data.startswith(PNG_SIGNATURE):
            return data, ""
        error = (result.stderr or b"").decode("utf-8", errors="replace").strip()
        return None, error or f"ADB 退出码 {result.returncode}"

    def _replace_queued_frame(self, item):
        try:
            self.frames.put_nowait(item)
            return
        except queue.Full:
            pass
        try:
            self.frames.get_nowait()
        except queue.Empty:
            pass
        try:
            self.frames.put_nowait(item)
        except queue.Full:
            pass

    def _capture_loop(self):
        failures = 0
        while not self.stop_event.is_set():
            started = time.perf_counter()
            data, error = self._capture_once()
            elapsed = time.perf_counter() - started
            if data:
                failures = 0
                self._replace_queued_frame((data, elapsed))
            else:
                failures += 1
                self._replace_queued_frame((None, error or "设备无响应"))
                if failures == 1:
                    try:
                        if ":" in self.serial:
                            run_hidden([self.adb_path, "connect", self.serial], timeout=5)
                    except (OSError, subprocess.SubprocessError):
                        pass
            delay = 1.0 if failures else max(0.0, self.interval - elapsed)
            self.stop_event.wait(delay)

    def _poll_frames(self):
        if self.stop_event.is_set():
            return
        newest = None
        while True:
            try:
                newest = self.frames.get_nowait()
            except queue.Empty:
                break
        if newest is not None:
            data, meta = newest
            if data is None:
                self.status.set(f"预览失败：{meta}")
            else:
                self.latest_png = data
                now = time.perf_counter()
                self.frame_times.append(now)
                self.frame_times = self.frame_times[-12:]
                fps = 0.0
                if len(self.frame_times) > 1:
                    span = self.frame_times[-1] - self.frame_times[0]
                    fps = (len(self.frame_times) - 1) / span if span > 0 else 0.0
                self.status.set(f"延迟 {meta * 1000:.0f} ms · {fps:.1f} FPS")
                self._render_latest()
        self.root.after(16, self._poll_frames)

    def _render_latest(self):
        if not self.latest_png:
            return
        try:
            original = Image.open(io.BytesIO(self.latest_png)).convert("RGB")
            self.image_size = original.size
            canvas_width = max(1, self.canvas.winfo_width())
            canvas_height = max(1, self.canvas.winfo_height())
            scale = min(canvas_width / original.width, canvas_height / original.height)
            draw_width = max(1, int(original.width * scale))
            draw_height = max(1, int(original.height * scale))
            resampling = getattr(Image, "Resampling", Image).BILINEAR
            display = original.resize((draw_width, draw_height), resampling)
            self.photo = ImageTk.PhotoImage(display)
            left = (canvas_width - draw_width) // 2
            top = (canvas_height - draw_height) // 2
            self.display_rect = (left, top, draw_width, draw_height)
            self.canvas.delete("frame")
            self.canvas.create_image(left, top, image=self.photo, anchor=tk.NW, tags="frame")
        except Exception as exc:
            self.status.set(f"图片解码失败：{exc}")

    def _device_point(self, x: int, y: int) -> tuple[int, int] | None:
        left, top, width, height = self.display_rect
        source_width, source_height = self.image_size
        if not width or not height or not source_width or not source_height:
            return None
        if not (left <= x <= left + width and top <= y <= top + height):
            return None
        device_x = round((x - left) * source_width / width)
        device_y = round((y - top) * source_height / height)
        return (
            min(max(device_x, 0), source_width - 1),
            min(max(device_y, 0), source_height - 1),
        )

    def _pointer_down(self, event):
        point = self._device_point(event.x, event.y)
        if point is not None:
            self.drag_start = (event.x, event.y, *point, time.perf_counter())

    def _pointer_up(self, event):
        if self.drag_start is None:
            return
        start_x, start_y, device_x1, device_y1, started = self.drag_start
        self.drag_start = None
        end_point = self._device_point(event.x, event.y)
        if end_point is None:
            return
        device_x2, device_y2 = end_point
        distance = abs(event.x - start_x) + abs(event.y - start_y)
        if distance < 12:
            self._send_input("tap", device_x2, device_y2)
        else:
            duration = min(1500, max(100, int((time.perf_counter() - started) * 1000)))
            self._send_input("swipe", device_x1, device_y1, device_x2, device_y2, duration)

    def _send_input(self, *args):
        def worker():
            try:
                self._adb_command("shell", "input", *map(str, args), timeout=5)
            except (OSError, subprocess.SubprocessError):
                pass
        threading.Thread(target=worker, daemon=True).start()

    def _send_key(self, keycode: int):
        self._send_input("keyevent", keycode)

    def _save_snapshot(self):
        if not self.latest_png:
            messagebox.showinfo("本地实时预览", "当前还没有可保存的画面。", parent=self.root)
            return
        safe_serial = re.sub(r"[^0-9A-Za-z._-]+", "_", self.serial).strip("._") or "device"
        default_name = f"screen_{safe_serial}_{datetime.now():%Y%m%d_%H%M%S}.png"
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title="保存设备截图",
            defaultextension=".png",
            initialfile=default_name,
            filetypes=(("PNG 图片", "*.png"),),
        )
        if not path:
            return
        try:
            with open(path, "wb") as file:
                file.write(self.latest_png)
            self.status.set(f"截图已保存：{os.path.basename(path)}")
        except OSError as exc:
            messagebox.showerror("保存失败", str(exc), parent=self.root)

    def close(self):
        self.stop_event.set()
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description="TapTap 本地 ADB 实时预览")
    parser.add_argument("--device", required=True, help="ADB 设备序列号")
    parser.add_argument("--adb", required=True, help="ADB 可执行文件")
    parser.add_argument("--interval", type=float, default=0.08, help="截图循环最小间隔（秒）")
    args = parser.parse_args()

    if not os.path.isfile(args.adb):
        raise SystemExit(f"找不到 ADB：{args.adb}")
    root = tk.Tk()
    NativePreview(root, args.adb, args.device, args.interval)
    root.mainloop()


if __name__ == "__main__":
    main()
