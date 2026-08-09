"""
ADB 设备管理 Web 服务
- 可视化查看/管理多台 adb 手机
- 一键连接新设备（IP:端口）、断开连接、USB 设备一键开启无线调试
- 设备详情：型号、品牌、Android 版本、分辨率、电量、IP
- 在线截图预览

依赖：仅 Python 标准库（无需 pip 安装任何包）

启动：
    .\.venv\Scripts\python.exe adb_manager\server.py            # 默认 0.0.0.0:8000
    .\.venv\Scripts\python.exe adb_manager\server.py --port 9000
"""

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
PROJECT_DIR = os.path.dirname(BASE_DIR)  # 项目根目录（taptap_auto_login.py 所在目录）
TAPTAP_SCRIPT = os.path.join(PROJECT_DIR, "taptap_auto_login.py")

# 运行自动化脚本用的 Python：优先使用项目 venv
_VENV_PY = os.path.join(PROJECT_DIR, ".venv", "Scripts", "python.exe")
PYTHON_PATH = _VENV_PY if os.path.isfile(_VENV_PY) else sys.executable

ADB_PATH = None

# 电池 status 数值含义（dumpsys battery）
BATTERY_STATUS = {1: "未知", 2: "充电中", 3: "放电中", 4: "未充电", 5: "已充满"}

_state_lock = threading.Lock()


def find_adb() -> str | None:
    """探测 adb 可执行文件路径（PATH 优先，其次常见安装位置）。"""
    p = shutil.which("adb")
    if p:
        return p
    for cand in [
        os.path.join(os.path.expanduser("~"), "platform-tools", "adb.exe"),
        os.path.join(os.path.expanduser("~"), "adb", "adb.exe"),
        r"D:\platform-tools\adb.exe",
        r"C:\Users\admin.LAPTOP\adb.exe",
        r"D:\android\platform-tools\adb.exe",
        r"C:\adb\adb.exe",
        r"E:\edgeDownload\platform-tools\adb.exe",
    ]:
        if os.path.isfile(cand):
            return cand
    return None


def adb(*args, timeout=10):
    """执行 adb 命令，返回 CompletedProcess；异常/超时返回 None。"""
    try:
        return subprocess.run(
            [ADB_PATH, *map(str, args)],
            capture_output=True,
            text=True,
            timeout=timeout,
            errors="replace",
        )
    except Exception:
        return None


def adb_text(*args, timeout=10) -> str:
    r = adb(*args, timeout=timeout)
    return (r.stdout + r.stderr) if r else ""


def shell(serial: str, cmd: str, timeout=8) -> str:
    """在指定设备上执行 shell 命令。"""
    args = ["-s", serial, "shell", cmd] if serial else ["shell", cmd]
    return adb_text(*args, timeout=timeout)


# ============ 设备信息 ============


def _clean_model(v: str | None) -> str | None:
    """过滤无效/无意义的 model 值（如无线调试的 mDNS 服务名）。"""
    if not v:
        return None
    v = v.strip()
    if not v or v.startswith("adb-") or "_adb-tls-connect" in v:
        return None
    return v


def list_devices_raw() -> list[dict]:
    """解析 `adb devices -l`，返回 [{serial, state, type, model}]。"""
    out = adb_text("devices", "-l", timeout=5)
    devices = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("List of") or line.startswith("* "):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        model = None
        if state == "device":
            for p in parts[2:]:
                if p.startswith("model:"):
                    model = p.split(":", 1)[1]
        d = {
            "serial": serial,
            "state": state,
            "model": _clean_model(model),
            "type": _conn_type(serial),
        }
        devices.append(d)
    return devices


def _conn_type(serial: str) -> str:
    if ":" in serial or "_adb-tls-connect" in serial or serial.startswith("adb-"):
        return "无线"
    if serial.startswith("emulator-"):
        return "模拟器"
    return "USB"


def fetch_details(serial: str) -> dict:
    """拉取单台设备的详细信息（供线程池并发调用）。"""
    detail = {}

    def get(cmd: str) -> str:
        return shell(serial, cmd, timeout=6).strip()

    detail["model"] = _clean_model(get("getprop ro.product.model"))
    detail["brand"] = get("getprop ro.product.brand") or None
    detail["android"] = get("getprop ro.build.version.release") or None
    detail["sdk"] = get("getprop ro.build.version.sdk") or None

    m = re.search(r"(\d+)x(\d+)", get("wm size"))
    detail["resolution"] = f"{m.group(1)}x{m.group(2)}" if m else None

    # 电量
    level = status = None
    batt = shell(serial, "dumpsys battery", timeout=6)
    for line in batt.splitlines():
        m2 = re.match(r"\s*(level|status):\s*(.+)", line)
        if m2:
            key, val = m2.group(1), m2.group(2).strip()
            if key == "level":
                level = val
            elif key == "status" and val.isdigit():
                status = BATTERY_STATUS.get(int(val), val)
    if level is not None:
        detail["battery"] = f"{level}%" + (f"（{status}）" if status else "")
    else:
        detail["battery"] = None

    # IP（从路由表取 src 地址）
    ip = None
    for line in shell(serial, "ip route", timeout=6).splitlines():
        m3 = re.search(r"src\s+(\d+\.\d+\.\d+\.\d+)", line)
        if m3:
            ip = m3.group(1)
            break
    detail["ip"] = ip
    return detail


def api_devices() -> list[dict]:
    devices = list_devices_raw()
    online = [d for d in devices if d["state"] == "device"]
    if online:
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(online)))) as ex:
            futures = {ex.submit(fetch_details, d["serial"]): d for d in online}
            for fut, d in futures.items():
                try:
                    d.update(fut.result())
                except Exception:
                    pass
    return devices


# ============ 设备操作 ============


def api_connect(address: str) -> dict:
    with _state_lock:
        address = address.strip()
        if ":" not in address:
            address = f"{address}:5555"
        out = adb_text("connect", address, timeout=10)
        msg = out.strip() or "无输出"
        return {"ok": "connected to" in out.lower(), "message": msg, "address": address}


def api_disconnect(address: str) -> dict:
    with _state_lock:
        out = adb_text("disconnect", address.strip(), timeout=10)
        return {"ok": True, "message": out.strip() or "已断开"}


def api_tcpip(serial: str) -> dict:
    with _state_lock:
        out = adb_text("-s", serial, "tcpip", "5555", timeout=10)
        return {"ok": True, "message": out.strip() or "已执行"}


# ============ 屏幕输入控制（实时预览用） ============


def api_tap(serial: str, x, y) -> dict:
    if x is None or y is None:
        return {"ok": False, "message": "缺少点击坐标"}
    with _state_lock:
        out = adb_text("-s", serial, "shell", "input", "tap", str(int(x)), str(int(y)), timeout=8)
        return {"ok": True, "message": out.strip() or f"已点击 ({int(x)}, {int(y)})"}


def api_swipe(serial: str, x1, y1, x2, y2, duration=300) -> dict:
    if None in (x1, y1, x2, y2):
        return {"ok": False, "message": "缺少滑动坐标"}
    with _state_lock:
        out = adb_text(
            "-s", serial, "shell", "input", "swipe",
            str(int(x1)), str(int(y1)), str(int(x2)), str(int(y2)),
            str(int(duration or 300)), timeout=8,
        )
        return {"ok": True, "message": out.strip() or "已滑动"}


def api_key(serial: str, key) -> dict:
    if key is None:
        return {"ok": False, "message": "缺少按键代码"}
    with _state_lock:
        out = adb_text("-s", serial, "shell", "input", "keyevent", str(int(key)), timeout=8)
        return {"ok": True, "message": out.strip() or f"已按键 {key}"}


# ============ 自动化任务（运行 taptap_auto_login.py） ============

TASKS = {}
_TASK_LOCK = threading.Lock()


def api_task_run(serial: str) -> dict:
    """启动 TapTap 自动登录脚本（子进程，实时采集日志）。同一时间只允许一个任务。"""
    if not os.path.isfile(TAPTAP_SCRIPT):
        return {"ok": False, "message": f"找不到脚本: {TAPTAP_SCRIPT}"}
    with _TASK_LOCK:
        for t in TASKS.values():
            if t["status"] == "running":
                return {"ok": False, "message": "已有任务在运行，请先等待完成或停止"}
        task = {
            "id": f"task_{int(time.time() * 1000)}",
            "serial": serial,
            "status": "running",
            "lines": [],
            "started": time.strftime("%H:%M:%S"),
            "ts": time.time(),
            "proc": None,
        }
        TASKS[task["id"]] = task

    try:
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.Popen(
            [PYTHON_PATH, "-u", TAPTAP_SCRIPT, "--device", serial],
            cwd=PROJECT_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )
    except Exception as e:
        with _TASK_LOCK:
            task["status"] = "error"
            task["lines"].append(f"[系统] 任务启动失败: {e}")
        return {"ok": False, "message": f"任务启动失败: {e}"}

    task["proc"] = proc
    threading.Thread(target=_task_reader, args=(task["id"], proc), daemon=True).start()
    return {"ok": True, "id": task["id"], "message": "任务已启动"}


def _task_reader(task_id: str, proc) -> None:
    task = TASKS.get(task_id)
    try:
        for line in proc.stdout:
            line = line.rstrip("\n")
            if line and task:
                task["lines"].append(line)
                if len(task["lines"]) > 2000:
                    task["lines"] = task["lines"][-2000:]
    except Exception:
        pass
    rc = proc.wait()
    if task:
        task["status"] = "success" if rc == 0 else "error"
        task["lines"].append(f"[系统] 任务结束，退出码 {rc}")
        task["proc"] = None


def api_task_state(offset: int = 0) -> dict:
    """返回最近一次任务的状态与新增日志行。"""
    with _TASK_LOCK:
        if not TASKS:
            return {"task": None}
        t = max(TASKS.values(), key=lambda x: x["ts"])
        new_lines = t["lines"][offset:]
        return {
            "task": {
                "id": t["id"],
                "serial": t["serial"],
                "status": t["status"],
                "started": t["started"],
                "lines": new_lines,
                "total": len(t["lines"]),
            }
        }


def api_task_stop() -> dict:
    with _TASK_LOCK:
        for t in TASKS.values():
            if t["status"] == "running" and t.get("proc"):
                t["proc"].kill()
                return {"ok": True, "message": "已发送停止指令，进程将被终止"}
    return {"ok": False, "message": "当前没有运行中的任务"}


def api_pair(address: str, code: str) -> dict:
    """Android 11+ 无线调试配对：adb pair IP:配对端口，输入 6 位配对码。"""
    with _state_lock:
        address = address.strip()
        code = code.strip()
        if ":" not in address:
            return {"ok": False, "message": "配对地址格式应为 IP:端口（手机「使用配对码配对设备」中显示的）"}
        if not code:
            return {"ok": False, "message": "请填写 6 位配对码"}
        try:
            r = subprocess.run(
                [ADB_PATH, "pair", address],
                input=code + "\n",
                capture_output=True,
                text=True,
                timeout=20,
                errors="replace",
            )
        except Exception as e:
            return {"ok": False, "message": f"配对失败：{e}"}
        out = (r.stdout or "") + (r.stderr or "")
        msg = out.strip() or "无输出"
        return {"ok": "successfully paired" in out.lower(), "message": msg}


# ============ HTTP 服务 ============


class Handler(BaseHTTPRequestHandler):
    server_version = "AdbManager/1.0"

    def log_message(self, fmt, *args):
        print(f"[{time.strftime('%H:%M:%S')}] {fmt % args}")

    # ---- 响应辅助 ----
    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, data, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, path):
        if not os.path.isfile(path):
            return self._send_json({"error": "not found"}, 404)
        ext = os.path.splitext(path)[1].lower()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".png": "image/png",
            ".ico": "image/x-icon",
        }.get(ext, "application/octet-stream")
        with open(path, "rb") as f:
            self._send_bytes(f.read(), ctype)

    # ---- 路由 ----
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path

        if path in ("/", "/index.html"):
            return self._send_file(os.path.join(STATIC_DIR, "index.html"))

        if path.startswith("/static/"):
            rel = os.path.relpath(path, "/static")
            return self._send_file(os.path.join(STATIC_DIR, rel))

        if path == "/api/devices":
            return self._send_json({"ok": True, "devices": api_devices()})

        if path == "/api/task":
            offset = 0
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if qs.get("offset"):
                try:
                    offset = max(0, int(qs["offset"][0]))
                except ValueError:
                    offset = 0
            return self._send_json(api_task_state(offset))

        if path.startswith("/api/devices/") and path.endswith("/screenshot"):
            serial = urllib.parse.unquote(path[len("/api/devices/"):-len("/screenshot")])
            return self._send_screenshot(serial)

        self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8")) if raw.strip() else {}
        except Exception:
            data = {}
        path = urllib.parse.urlparse(self.path).path

        if path == "/api/connect":
            return self._send_json(api_connect(data.get("address", "")))
        if path == "/api/pair":
            return self._send_json(api_pair(data.get("address", ""), data.get("code", "")))
        if path == "/api/task/run":
            return self._send_json(api_task_run(data.get("serial", "")))
        if path == "/api/task/stop":
            return self._send_json(api_task_stop())
        if path == "/api/disconnect":
            return self._send_json(api_disconnect(data.get("address", "")))
        if path == "/api/tcpip":
            return self._send_json(api_tcpip(data.get("serial", "")))

        # 屏幕输入控制：/api/devices/<serial>/tap|swipe|key
        m = re.fullmatch(r"/api/devices/([^/]+)/(tap|swipe|key)", path)
        if m:
            serial = urllib.parse.unquote(m.group(1))
            action = m.group(2)
            if action == "tap":
                return self._send_json(api_tap(serial, data.get("x"), data.get("y")))
            if action == "swipe":
                return self._send_json(api_swipe(
                    serial, data.get("x1"), data.get("y1"),
                    data.get("x2"), data.get("y2"), data.get("duration"),
                ))
            if action == "key":
                return self._send_json(api_key(serial, data.get("key")))

        self._send_json({"error": "not found"}, 404)

    # ---- 截图 ----
    def _send_screenshot(self, serial: str):
        try:
            r = subprocess.run(
                [ADB_PATH, "-s", serial, "exec-out", "screencap", "-p"],
                capture_output=True,
                timeout=15,
            )
            data = r.stdout.replace(b"\r\n", b"\n")  # 修复 Windows 下 screencap 的换行问题
        except Exception:
            data = b""
        if not data or data.startswith(b"error"):
            return self._send_json({"error": "截图失败（设备可能离线）"}, 500)
        self._send_bytes(data, "image/png")


def _lan_ip() -> str | None:
    """获取本机局域网 IP（用于提示局域网访问地址）。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def main():
    global ADB_PATH
    parser = argparse.ArgumentParser(description="ADB 设备管理 Web 服务")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址，默认 0.0.0.0（局域网可访问）")
    parser.add_argument("--port", type=int, default=8000, help="监听端口，默认 8000")
    args = parser.parse_args()

    ADB_PATH = find_adb()
    if not ADB_PATH:
        print("[FAIL] 找不到 adb.exe，请安装 platform-tools 或设置环境变量")
        return

    subprocess.run([ADB_PATH, "start-server"], capture_output=True, timeout=10)
    print(f"[OK] 使用 adb: {ADB_PATH}")
    print("[OK] 服务已启动，请在浏览器打开：")
    print(f"     本机访问:   http://127.0.0.1:{args.port}")
    ip = _lan_ip()
    if ip:
        print(f"     局域网访问: http://{ip}:{args.port}")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[OK] 服务已停止")


if __name__ == "__main__":
    main()
