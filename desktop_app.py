"""adbTaptap Windows desktop shell powered by Edge WebView2."""

from __future__ import annotations

import ctypes
import json
import logging
import os
import sys
import threading
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = PROJECT_DIR / "runtime"
DESKTOP_DIR = RUNTIME_DIR / "desktop"
PACKAGES_DIR = RUNTIME_DIR / "desktop-packages"
WEBVIEW_DATA_DIR = DESKTOP_DIR / "webview-data"
STATE_PATH = DESKTOP_DIR / "window_state.json"
LOG_PATH = DESKTOP_DIR / "desktop_app.log"
MUTEX_NAME = "Local\\adbTaptapDesktopApplication"
WINDOW_TITLE = "ADB 设备管理器"

for folder in (DESKTOP_DIR, PACKAGES_DIR, WEBVIEW_DATA_DIR):
    folder.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(PACKAGES_DIR))

logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    encoding="utf-8",
)
LOGGER = logging.getLogger("adbTaptap.desktop")


def native_message(title: str, message: str, error: bool = False) -> None:
    if os.name == "nt":
        flags = 0x10 if error else 0x40
        ctypes.windll.user32.MessageBoxW(None, message, title, flags)
    else:
        print(f"{title}: {message}", file=sys.stderr if error else sys.stdout)


def activate_existing_window() -> bool:
    if os.name != "nt":
        return False
    user32 = ctypes.windll.user32
    handle = user32.FindWindowW(None, WINDOW_TITLE)
    if not handle:
        return False
    user32.ShowWindow(handle, 9)  # SW_RESTORE
    user32.SetForegroundWindow(handle)
    return True


class SingleInstance:
    def __init__(self) -> None:
        self.handle = None

    def acquire(self) -> bool:
        if os.name != "nt":
            return True
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        self.handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        return bool(self.handle) and kernel32.GetLastError() != 183

    def close(self) -> None:
        if self.handle and os.name == "nt":
            ctypes.windll.kernel32.CloseHandle(self.handle)
            self.handle = None


class WindowState:
    DEFAULT = {
        "width": 1440,
        "height": 900,
        "x": None,
        "y": None,
        "maximized": False,
        "on_top": False,
    }

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.value = dict(self.DEFAULT)
        try:
            loaded = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self.value.update({key: loaded[key] for key in self.DEFAULT if key in loaded})
        except (OSError, ValueError):
            pass
        self.value["width"] = max(1000, int(self.value.get("width") or 1440))
        self.value["height"] = max(680, int(self.value.get("height") or 900))
        for axis in ("x", "y"):
            coordinate = self.value.get(axis)
            if not isinstance(coordinate, int) or abs(coordinate) > 20000:
                self.value[axis] = None

    def update(self, **changes) -> None:
        with self.lock:
            self.value.update(changes)
            temp = STATE_PATH.with_suffix(".tmp")
            temp.write_text(json.dumps(self.value, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp, STATE_PATH)

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.value)


class DesktopApi:
    def __init__(self, state: WindowState) -> None:
        self._state = state
        self._window = None

    def bind(self, window) -> None:
        self._window = window

    def get_window_state(self) -> dict:
        result = self._state.snapshot()
        result["desktop"] = True
        return result

    def set_always_on_top(self, enabled) -> dict:
        value = bool(enabled)
        if self._window is not None:
            self._window.on_top = value
        self._state.update(on_top=value)
        return {"ok": True, "on_top": value}

    def reload(self) -> dict:
        if self._window is not None:
            self._window.load_url(self._window.get_current_url())
        return {"ok": True}


def import_webview():
    try:
        import webview  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "桌面组件尚未安装。请双击“启动桌面版.bat”，程序会把依赖安装到项目 runtime 目录。"
        ) from exc
    return webview


def run() -> int:
    instance = SingleInstance()
    if not instance.acquire():
        if not activate_existing_window():
            native_message(WINDOW_TITLE, "桌面程序已经在运行，请切换到现有窗口。")
        return 0

    http_server = None
    server_thread = None
    try:
        webview = import_webview()
        from adb_manager import server as manager_server

        # Keep the existing phone QR/LAN access feature while the desktop shell
        # itself always connects through loopback.
        http_server = manager_server.create_server("0.0.0.0", 0)
        port = int(http_server.server_address[1])
        server_thread = threading.Thread(
            target=http_server.serve_forever,
            kwargs={"poll_interval": 0.25},
            daemon=True,
            name="desktop-http-server",
        )
        server_thread.start()

        state = WindowState()
        saved = state.snapshot()
        api = DesktopApi(state)
        url = f"http://127.0.0.1:{port}/?desktop=1"
        window = webview.create_window(
            WINDOW_TITLE,
            url=url,
            js_api=api,
            width=saved["width"],
            height=saved["height"],
            x=saved.get("x"),
            y=saved.get("y"),
            min_size=(1000, 680),
            resizable=True,
            maximized=bool(saved.get("maximized")),
            on_top=bool(saved.get("on_top")),
            background_color="#0f172a",
            text_select=True,
        )
        api.bind(window)

        def save_resize(width, height):
            state.update(width=max(1000, int(width)), height=max(680, int(height)), maximized=False)

        def save_move(x, y):
            state.update(x=int(x), y=int(y))

        def save_maximized():
            state.update(maximized=True)

        def save_restored():
            state.update(maximized=False)

        def close_backend():
            if http_server is not None:
                threading.Thread(target=http_server.shutdown, daemon=True).start()

        window.events.resized += save_resize
        window.events.moved += save_move
        window.events.maximized += save_maximized
        window.events.restored += save_restored
        window.events.closed += close_backend

        LOGGER.info("Desktop window starting at %s", url)
        webview.start(
            gui="edgechromium",
            debug=False,
            private_mode=False,
            storage_path=str(WEBVIEW_DATA_DIR),
        )
        return 0
    except Exception as exc:
        LOGGER.exception("Desktop application failed")
        native_message(
            "ADB 设备管理器启动失败",
            f"{exc}\n\n详细日志：{LOG_PATH}",
            error=True,
        )
        return 1
    finally:
        if http_server is not None:
            try:
                http_server.shutdown()
                http_server.server_close()
            except Exception:
                LOGGER.exception("Failed to close embedded HTTP server")
        if server_thread is not None and server_thread.is_alive():
            server_thread.join(timeout=2)
        instance.close()


if __name__ == "__main__":
    raise SystemExit(run())
