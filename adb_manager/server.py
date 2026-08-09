r"""
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
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
PROJECT_DIR = os.path.dirname(BASE_DIR)  # 项目根目录（taptap_auto_login.py 所在目录）
TAPTAP_SCRIPT = os.path.join(PROJECT_DIR, "taptap_auto_login.py")
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from adb_locator import find_adb
from automation_config import (
    COUNTRY_OPTIONS,
    DEFAULT_JFBYM_API_URL,
    DEFAULT_JFBYM_TOKEN,
    DEFAULT_JFBYM_TYPE,
    DEFAULT_PHONE_COUNTRY,
    DEFAULT_SMS_API_URL,
    DEFAULT_SMS_TOKEN,
    normalize_country,
    parse_accounts_text,
)

# 运行自动化脚本用的 Python：优先使用项目 venv
_VENV_PY = os.path.join(PROJECT_DIR, ".venv", "Scripts", "python.exe")
PYTHON_PATH = _VENV_PY if os.path.isfile(_VENV_PY) else sys.executable

ADB_PATH = None

SETTINGS_PATH = os.path.join(BASE_DIR, "settings.json")
ACCOUNTS_DIR = os.path.join(BASE_DIR, "accounts")
ACCOUNT_STATE_PATH = os.path.join(BASE_DIR, "account_queue.json")
DEVICE_LOGS_DIR = os.path.join(BASE_DIR, "device_logs")
UI_DUMPS_DIR = os.path.join(BASE_DIR, "ui_dumps")
DEFAULT_SETTINGS = {
    "country": DEFAULT_PHONE_COUNTRY,
    "sms_api_url": DEFAULT_SMS_API_URL,
    "sms_token": DEFAULT_SMS_TOKEN,
    "jfbym_api_url": DEFAULT_JFBYM_API_URL,
    "jfbym_token": DEFAULT_JFBYM_TOKEN,
    "jfbym_type": DEFAULT_JFBYM_TYPE,
}
_SETTING_KEYS = set(DEFAULT_SETTINGS)

# 电池 status 数值含义（dumpsys battery）
BATTERY_STATUS = {1: "未知", 2: "充电中", 3: "放电中", 4: "未充电", 5: "已充满"}

_state_lock = threading.Lock()
_screenshot_lock = threading.Lock()
_settings_lock = threading.Lock()
_account_lock = threading.RLock()
_ui_dump_lock = threading.Lock()
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


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


def capture_screenshot(serial: str) -> tuple[bytes | None, str | None]:
    """获取原始 PNG；无线设备瞬时断连时自动重连一次。"""
    if not serial:
        return None, "缺少设备序列号"

    last_error = "截图失败"
    with _screenshot_lock:
        for attempt in range(2):
            try:
                r = subprocess.run(
                    [ADB_PATH, "-s", serial, "exec-out", "screencap", "-p"],
                    capture_output=True,
                    timeout=15,
                )
            except subprocess.TimeoutExpired:
                last_error = "截图超时"
                r = None
            except OSError as exc:
                return None, f"adb 执行失败: {exc}"

            if r is not None:
                data = r.stdout or b""
                if r.returncode == 0 and data.startswith(_PNG_SIGNATURE):
                    # exec-out 返回的是原始二进制，不能替换 CRLF，否则会破坏 PNG。
                    return data, None
                stderr = (r.stderr or b"").decode("utf-8", errors="replace").strip()
                last_error = stderr or f"adb 退出码 {r.returncode}"

            if attempt == 0 and ":" in serial:
                adb("connect", serial, timeout=8)
                time.sleep(0.25)

    return None, last_error


# ============ 自动化设置与账号文件 ============


def _read_settings_unlocked() -> dict:
    settings = dict(DEFAULT_SETTINGS)
    if os.path.isfile(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as file:
                saved = json.load(file)
            if isinstance(saved, dict):
                for key in _SETTING_KEYS:
                    if key in saved and isinstance(saved[key], str):
                        settings[key] = saved[key]
        except (OSError, ValueError):
            pass
    settings["country"] = normalize_country(settings.get("country"))
    return settings


def load_settings() -> dict:
    with _settings_lock:
        return _read_settings_unlocked()


def _write_settings_unlocked(settings: dict) -> None:
    temp_path = SETTINGS_PATH + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as file:
        json.dump(settings, file, ensure_ascii=False, indent=2)
    os.replace(temp_path, SETTINGS_PATH)


def api_settings_get() -> dict:
    settings = load_settings()
    return {
        "ok": True,
        "settings": {key: settings[key] for key in _SETTING_KEYS},
        "countries": [
            {"value": value, "label": label}
            for value, label in COUNTRY_OPTIONS.items()
        ],
    }


def api_settings_save(data: dict) -> dict:
    with _settings_lock:
        settings = _read_settings_unlocked()
        if "country" in data:
            settings["country"] = normalize_country(data.get("country"))
        for key in (
            "sms_api_url", "sms_token", "jfbym_api_url", "jfbym_token", "jfbym_type",
        ):
            if key in data:
                value = str(data.get(key) or "").strip()
                if len(value) > 4096:
                    return {"ok": False, "message": f"{key} 内容过长"}
                settings[key] = value
        if not settings["sms_api_url"]:
            return {"ok": False, "message": "短信 API 地址不能为空"}
        if not settings["jfbym_api_url"]:
            return {"ok": False, "message": "云码 API 地址不能为空"}
        _write_settings_unlocked(settings)
    result = api_settings_get()
    result["message"] = "设置已保存，将在下次任务启动时生效"
    return result


# ============ 多账号队列 ============


def _account_id(phone: str) -> str:
    return hashlib.sha256(phone.encode("utf-8")).hexdigest()[:16]


def _empty_account_state() -> dict:
    return {"version": 1, "source_filename": "", "imported_at": "", "accounts": []}


def _read_account_state_unlocked() -> dict:
    state = _empty_account_state()
    if os.path.isfile(ACCOUNT_STATE_PATH):
        try:
            with open(ACCOUNT_STATE_PATH, "r", encoding="utf-8") as file:
                saved = json.load(file)
            if isinstance(saved, dict) and isinstance(saved.get("accounts"), list):
                state.update(saved)
        except (OSError, ValueError):
            pass

    cleaned = []
    seen = set()
    for position, raw in enumerate(state.get("accounts", []), 1):
        if not isinstance(raw, dict):
            continue
        phone = re.sub(r"\D", "", str(raw.get("phone") or ""))
        if not phone or phone in seen:
            continue
        seen.add(phone)
        status = str(raw.get("status") or "pending")
        if status == "running":
            status = "pending"
        if status not in {"pending", "failed", "completed"}:
            status = "pending"
        record = {
            "id": _account_id(phone),
            "position": position,
            "phone": phone,
            "country": normalize_country(raw.get("country")),
            "sms_api_url": str(raw.get("sms_api_url") or ""),
            "sms_token": str(raw.get("sms_token") or ""),
            "selected": bool(raw.get("selected", status != "completed")) and status != "completed",
            "assigned_device": str(raw.get("assigned_device") or ""),
            "status": status,
            "last_device": str(raw.get("last_device") or ""),
            "last_error": str(raw.get("last_error") or ""),
            "completed_at": str(raw.get("completed_at") or ""),
        }
        cleaned.append(record)
    state["accounts"] = cleaned
    return state


def _write_account_state_unlocked(state: dict) -> None:
    temp_path = ACCOUNT_STATE_PATH + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)
    os.replace(temp_path, ACCOUNT_STATE_PATH)


def load_account_state() -> dict:
    with _account_lock:
        return _read_account_state_unlocked()


def _public_account(record: dict) -> dict:
    result = {
        key: record.get(key)
        for key in (
            "id", "position", "phone", "country", "selected", "assigned_device",
            "status", "last_device", "last_error", "completed_at",
        )
    }
    result["has_sms_url"] = bool(record.get("sms_api_url"))
    return result


def api_accounts_get() -> dict:
    state = load_account_state()
    accounts = [_public_account(record) for record in state["accounts"]]
    active_status = {}
    with _TASK_LOCK:
        for task in TASKS.values():
            if task.get("status") != "running":
                continue
            for account_id in task.get("remaining_account_ids", task.get("account_ids", [])):
                active_status[account_id] = "queued"
            if task.get("current_account_id"):
                active_status[task["current_account_id"]] = "running"
    for account in accounts:
        if account["id"] in active_status:
            account["status"] = active_status[account["id"]]
    return {
        "ok": True,
        "source_filename": state.get("source_filename", ""),
        "imported_at": state.get("imported_at", ""),
        "accounts": accounts,
        "summary": {
            "total": len(accounts),
            "selected": sum(bool(item["selected"]) for item in accounts),
            "completed": sum(item["status"] == "completed" for item in accounts),
            "failed": sum(item["status"] == "failed" for item in accounts),
        },
    }


def api_accounts_import(filename: str, content: str) -> dict:
    if _active_account_ids():
        return {"ok": False, "message": "有账号正在排队或运行，暂时不能重新导入"}
    original_name = os.path.basename(filename or "").strip()
    if not original_name.lower().endswith(".txt"):
        return {"ok": False, "message": "请选择 .txt 账号文件"}
    if not isinstance(content, str):
        return {"ok": False, "message": "账号文件内容无效"}
    if len(content.encode("utf-8")) > 1024 * 1024:
        return {"ok": False, "message": "账号文件不能超过 1 MB"}
    try:
        parsed_accounts = parse_accounts_text(content)
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}

    with _account_lock:
        old_state = _read_account_state_unlocked()
        old_by_phone = {item["phone"]: item for item in old_state["accounts"]}
        records = []
        for position, account in enumerate(parsed_accounts, 1):
            phone = account["phone"]
            previous = old_by_phone.get(phone, {})
            previous_status = previous.get("status", "pending")
            completed = previous_status == "completed"
            records.append({
                "id": _account_id(phone),
                "position": position,
                "phone": phone,
                "country": normalize_country(account.get("country")),
                "sms_api_url": str(account.get("sms_api_url") or ""),
                "sms_token": str(account.get("sms_token") or ""),
                "selected": bool(previous.get("selected", True)) and not completed,
                "assigned_device": str(previous.get("assigned_device") or ""),
                "status": "completed" if completed else previous_status if previous_status == "failed" else "pending",
                "last_device": str(previous.get("last_device") or ""),
                "last_error": str(previous.get("last_error") or ""),
                "completed_at": str(previous.get("completed_at") or ""),
            })
        state = {
            "version": 1,
            "source_filename": original_name,
            "imported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "accounts": records,
        }
        _write_account_state_unlocked(state)

    result = api_accounts_get()
    result["message"] = f"已解析 {len(parsed_accounts)} 个不重复账号"
    return result


def _active_account_ids() -> set[str]:
    with _TASK_LOCK:
        active = set()
        for task in TASKS.values():
            if task.get("status") == "running":
                active.update(task.get("reserved_account_ids", task.get("account_ids", [])))
        return active


def api_account_update(account_id: str, selected=None, assigned_device=None) -> dict:
    active_ids = _active_account_ids()
    with _account_lock:
        state = _read_account_state_unlocked()
        record = next((item for item in state["accounts"] if item["id"] == account_id), None)
        if not record:
            return {"ok": False, "message": "账号不存在或账号列表已更新"}
        if account_id in active_ids:
            return {"ok": False, "message": "账号正在运行，暂时不能修改"}
        if selected is not None:
            if bool(selected) and record["status"] == "completed":
                return {"ok": False, "message": "已完成账号不会重复执行"}
            record["selected"] = bool(selected)
        if assigned_device is not None:
            device = str(assigned_device or "").strip()
            if len(device) > 256:
                return {"ok": False, "message": "设备序列号过长"}
            record["assigned_device"] = device
        _write_account_state_unlocked(state)
    result = api_accounts_get()
    result["message"] = "账号设置已保存"
    return result


def api_accounts_select_all(selected: bool) -> dict:
    active_ids = _active_account_ids()
    with _account_lock:
        state = _read_account_state_unlocked()
        for record in state["accounts"]:
            if record["id"] not in active_ids and record["status"] != "completed":
                record["selected"] = bool(selected)
        _write_account_state_unlocked(state)
    result = api_accounts_get()
    result["message"] = "账号勾选状态已更新"
    return result


def api_accounts_clear() -> dict:
    if _active_account_ids():
        return {"ok": False, "message": "有账号正在运行，不能清空列表"}
    with _account_lock:
        _write_account_state_unlocked(_empty_account_state())
    return {"ok": True, "message": "账号列表已清空", "accounts": [], "summary": {
        "total": 0, "selected": 0, "completed": 0, "failed": 0,
    }}


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


def api_dump_ui_elements(serial: str) -> dict:
    """抓取指定设备当前 UI 层级，并按逐元素 JSON 格式保存日志。"""
    serial = str(serial or "").strip()
    if not serial:
        return {"ok": False, "message": "缺少设备序列号"}
    remote_path = "/sdcard/window_dump.xml"
    with _ui_dump_lock:
        dump_result = adb(
            "-s", serial, "shell", "uiautomator", "dump", "--compressed", remote_path,
            timeout=20,
        )
        if not dump_result or dump_result.returncode != 0:
            # 少数旧版 Android 不支持 --compressed，自动回退普通 dump。
            dump_result = adb(
                "-s", serial, "shell", "uiautomator", "dump", remote_path,
                timeout=20,
            )
        if not dump_result or dump_result.returncode != 0:
            message = ""
            if dump_result:
                message = ((dump_result.stdout or "") + (dump_result.stderr or "")).strip()
            return {"ok": False, "message": "页面元素获取失败: " + (message or "设备无响应")}
        xml_result = adb("-s", serial, "exec-out", "cat", remote_path, timeout=12)
        if not xml_result or xml_result.returncode != 0:
            message = ""
            if xml_result:
                message = ((xml_result.stdout or "") + (xml_result.stderr or "")).strip()
            return {"ok": False, "message": "UI XML 读取失败: " + (message or "设备无响应")}

        xml_text = (xml_result.stdout or "").strip()
        xml_start = xml_text.find("<hierarchy")
        declaration_start = xml_text.find("<?xml")
        if declaration_start >= 0:
            xml_text = xml_text[declaration_start:]
        elif xml_start >= 0:
            xml_text = xml_text[xml_start:]
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            return {"ok": False, "message": f"UI XML 解析失败: {exc}"}

        captured_at = time.strftime("%Y-%m-%dT%H:%M:%S") + f".{int(time.time() % 1 * 1000):03d}"
        timestamp = time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time() % 1 * 1000):03d}"
        safe_serial = re.sub(r"[^0-9A-Za-z._-]+", "_", serial).strip("._")[:90] or "device"
        filename = f"{safe_serial}_{timestamp}.log"
        os.makedirs(UI_DUMPS_DIR, exist_ok=True)
        path = os.path.join(UI_DUMPS_DIR, filename)
        nodes = list(root.iter("node"))
        try:
            with open(path, "w", encoding="utf-8") as file:
                file.write(
                    f"[UI-ELEMENTS-BEGIN] device={serial} captured_at={captured_at} count={len(nodes)}\n"
                )
                for index, node in enumerate(nodes):
                    attributes = dict(node.attrib)
                    bounds = attributes.get("bounds", "")
                    bounds_match = re.fullmatch(
                        r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", bounds
                    )
                    element = {
                        "index": index,
                        "resource_id": attributes.get("resource-id") or None,
                        "text": attributes.get("text") or "",
                        "content_description": attributes.get("content-desc") or None,
                        "class_name": attributes.get("class") or None,
                        "rect": [int(value) for value in bounds_match.groups()] if bounds_match else None,
                        "bounds": bounds or None,
                        "clickable": attributes.get("clickable") == "true",
                        "enabled": attributes.get("enabled") == "true",
                        "checkable": attributes.get("checkable") == "true",
                        "checked": attributes.get("checked") == "true",
                        "focusable": attributes.get("focusable") == "true",
                        "focused": attributes.get("focused") == "true",
                        "selected": attributes.get("selected") == "true",
                        "scrollable": attributes.get("scrollable") == "true",
                        "password": attributes.get("password") == "true",
                        "attributes": attributes,
                    }
                    file.write(
                        "[UI-ELEMENT] "
                        + json.dumps(element, ensure_ascii=False, separators=(",", ":"))
                        + "\n"
                    )
                file.write(f"[UI-ELEMENTS-END] device={serial} count={len(nodes)}\n")
        except OSError as exc:
            return {"ok": False, "message": f"页面元素日志保存失败: {exc}"}

    print(f"[UI] 设备 {serial} 当前页面元素已保存: {path} ({len(nodes)} 个)")
    return {
        "ok": True,
        "message": f"已保存 {len(nodes)} 个页面元素",
        "serial": serial,
        "count": len(nodes),
        "filename": filename,
        "folder": UI_DUMPS_DIR,
        "path": path,
    }


# ============ 自动化任务（运行 taptap_auto_login.py） ============

TASKS = {}
_TASK_LOCK = threading.RLock()
_DEVICE_LOG_LOCK = threading.Lock()


def build_task_command(serial: str, settings: dict, account_path: str | None) -> list[str]:
    command = [
        PYTHON_PATH, "-u", TAPTAP_SCRIPT,
        "--device", serial,
        "--adb", ADB_PATH,
        "--country", settings["country"],
        "--sms-api", settings["sms_api_url"],
        "--sms-token", settings["sms_token"],
        "--jfbym-api", settings["jfbym_api_url"],
        "--jfbym-token", settings["jfbym_token"],
        "--jfbym-type", settings["jfbym_type"],
    ]
    if account_path:
        command.extend(["--account-file", account_path])
    return command


def _safe_device_log_name(serial: str) -> str:
    safe = re.sub(r"[^0-9A-Za-z._-]+", "_", serial).strip("._")[:80] or "device"
    suffix = hashlib.sha256(serial.encode("utf-8")).hexdigest()[:8]
    return f"{safe}_{suffix}.log"


def _device_log_path(serial: str) -> str:
    return os.path.join(DEVICE_LOGS_DIR, _safe_device_log_name(serial))


def _append_task_line(task: dict, line: str) -> None:
    text = str(line).rstrip("\r\n")
    if not text:
        return
    with _TASK_LOCK:
        task["lines"].append(text)
        if len(task["lines"]) > 5000:
            removed = len(task["lines"]) - 5000
            task["lines"] = task["lines"][removed:]
            task["line_base"] = task.get("line_base", 0) + removed
    try:
        os.makedirs(DEVICE_LOGS_DIR, exist_ok=True)
        with _DEVICE_LOG_LOCK, open(_device_log_path(task["serial"]), "a", encoding="utf-8") as file:
            file.write(text + "\n")
    except OSError:
        pass


def _account_snapshot(account_id: str) -> dict | None:
    with _account_lock:
        state = _read_account_state_unlocked()
        record = next((item for item in state["accounts"] if item["id"] == account_id), None)
        return dict(record) if record else None


def _prepare_account_file(record: dict) -> str:
    os.makedirs(ACCOUNTS_DIR, exist_ok=True)
    path = os.path.join(ACCOUNTS_DIR, f"queue_{record['id']}.txt")
    payload = {
        "phone": record["phone"],
        "country": record.get("country") or "auto",
    }
    if record.get("sms_api_url"):
        payload["sms_api_url"] = record["sms_api_url"]
    if record.get("sms_token"):
        payload["sms_token"] = record["sms_token"]
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
    os.replace(temp_path, path)
    return path


def _mark_account_started(account_id: str, serial: str) -> None:
    with _account_lock:
        state = _read_account_state_unlocked()
        record = next((item for item in state["accounts"] if item["id"] == account_id), None)
        if record:
            record["last_device"] = serial
            record["last_error"] = ""
            _write_account_state_unlocked(state)


def _mark_account_finished(account_id: str, serial: str, success: bool, error: str = "") -> None:
    with _account_lock:
        state = _read_account_state_unlocked()
        record = next((item for item in state["accounts"] if item["id"] == account_id), None)
        if not record:
            return
        record["last_device"] = serial
        if success:
            record["status"] = "completed"
            record["selected"] = False
            record["completed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            record["last_error"] = ""
        else:
            record["status"] = "failed"
            record["selected"] = True
            record["last_error"] = error or "自动化任务执行失败"
        _write_account_state_unlocked(state)


def _run_device_queue(task: dict, account_ids: list[str], settings: dict) -> None:
    serial = task["serial"]
    failures = 0
    completed = 0
    _append_task_line(task, "=" * 60)
    _append_task_line(task, f"[系统] 设备线程启动: {serial} | 分配账号 {len(account_ids)} 个")
    for queue_index, account_id in enumerate(account_ids, 1):
        if task["stop_event"].is_set():
            break
        record = _account_snapshot(account_id)
        if not record or not record.get("selected") or record.get("status") == "completed":
            continue

        with _TASK_LOCK:
            task["current_account_id"] = account_id
            task["current_phone"] = record["phone"]
            task["progress"] = queue_index
        _mark_account_started(account_id, serial)
        _append_task_line(task, "-" * 60)
        _append_task_line(
            task,
            f"[系统] 开始账号 {queue_index}/{len(account_ids)}: {record['phone']} | 设备: {serial}",
        )

        account_settings = dict(settings)
        account_settings["country"] = record.get("country") or settings["country"]
        if record.get("sms_api_url"):
            account_settings["sms_api_url"] = record["sms_api_url"]
            # 账号文件中的提取链接必须原样 GET，不追加全局 Token/Key。
            account_settings["sms_token"] = ""
        elif record.get("sms_token"):
            account_settings["sms_token"] = record["sms_token"]
        try:
            account_path = _prepare_account_file(record)
            command = build_task_command(serial, account_settings, account_path)
            env = dict(os.environ)
            env["PYTHONUNBUFFERED"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            env["ADB_PATH"] = ADB_PATH
            proc = subprocess.Popen(
                command,
                cwd=PROJECT_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
            )
            with _TASK_LOCK:
                task["proc"] = proc
            for line in proc.stdout:
                _append_task_line(task, line)
            return_code = proc.wait()
        except Exception as exc:
            return_code = -1
            _append_task_line(task, f"[系统] 账号任务启动失败: {exc}")
        finally:
            with _TASK_LOCK:
                task["proc"] = None

        stopped = task["stop_event"].is_set()
        success = return_code == 0 and not stopped
        if success:
            completed += 1
            _mark_account_finished(account_id, serial, True)
            _append_task_line(task, f"[系统] 账号完成并自动取消勾选: {record['phone']}")
        else:
            failures += 1
            reason = "用户停止任务" if stopped else f"任务退出码 {return_code}"
            _mark_account_finished(account_id, serial, False, reason)
            _append_task_line(task, f"[系统] 账号未完成，保留勾选供下次续跑: {record['phone']} | {reason}")
        with _TASK_LOCK:
            task["completed"] = completed
            task["failures"] = failures
            if account_id in task.get("remaining_account_ids", []):
                task["remaining_account_ids"].remove(account_id)
            if success and account_id in task.get("reserved_account_ids", []):
                task["reserved_account_ids"].remove(account_id)
        if stopped:
            break

    with _TASK_LOCK:
        task["current_account_id"] = None
        task["current_phone"] = ""
        task["completed"] = completed
        task["failures"] = failures
        if task["stop_event"].is_set():
            task["status"] = "stopped"
        elif failures:
            task["status"] = "partial"
        else:
            task["status"] = "success"
    _append_task_line(
        task,
        f"[系统] 设备线程结束: 完成 {completed} 个，失败 {failures} 个，剩余账号下次可继续",
    )
    _append_task_line(task, "=" * 60)


def api_task_run(serials) -> dict:
    """为每台选中设备启动一个线程，线程内按顺序逐个执行账号。"""
    if not os.path.isfile(TAPTAP_SCRIPT):
        return {"ok": False, "message": f"找不到脚本: {TAPTAP_SCRIPT}"}
    if isinstance(serials, str):
        serials = [serials]
    requested = []
    for serial in serials or []:
        serial = str(serial or "").strip()
        if serial and serial not in requested:
            requested.append(serial)
    if not requested:
        return {"ok": False, "message": "请至少勾选一台在线设备"}

    online = {item["serial"] for item in list_devices_raw() if item["state"] == "device"}
    unavailable = [serial for serial in requested if serial not in online]
    if unavailable:
        return {"ok": False, "message": "以下设备不在线: " + ", ".join(unavailable)}

    settings = load_settings()
    with _TASK_LOCK:
        busy = [
            serial for serial in requested
            if TASKS.get(serial, {}).get("status") == "running"
        ]
        if busy:
            return {"ok": False, "message": "以下设备已有任务: " + ", ".join(busy)}
        active_ids = set()
        for task in TASKS.values():
            if task.get("status") == "running":
                active_ids.update(task.get("reserved_account_ids", task.get("account_ids", [])))

        state = load_account_state()
        candidates = [
            record for record in state["accounts"]
            if record.get("selected") and record.get("status") != "completed"
            and record["id"] not in active_ids
        ]
        if not candidates:
            return {"ok": False, "message": "没有已勾选且未完成的账号"}

        queues = {serial: [] for serial in requested}
        skipped_assigned = []
        automatic = []
        for record in candidates:
            assigned = record.get("assigned_device") or ""
            if assigned:
                if assigned in queues:
                    queues[assigned].append(record["id"])
                else:
                    skipped_assigned.append(record["phone"])
            else:
                automatic.append(record)
        for record in automatic:
            serial = min(requested, key=lambda item: (len(queues[item]), requested.index(item)))
            queues[serial].append(record["id"])

        batch_id = f"batch_{int(time.time() * 1000)}"
        started = []
        threads = []
        for serial in requested:
            account_ids = queues[serial]
            if not account_ids:
                continue
            task = {
                "id": f"{batch_id}_{hashlib.sha256(serial.encode('utf-8')).hexdigest()[:8]}",
                "batch_id": batch_id,
                "serial": serial,
                "status": "running",
                "lines": [],
                "line_base": 0,
                "started": time.strftime("%H:%M:%S"),
                "ts": time.time(),
                "proc": None,
                "stop_event": threading.Event(),
                "current_account_id": None,
                "current_phone": "",
                "progress": 0,
                "total": len(account_ids),
                "completed": 0,
                "failures": 0,
                "account_ids": list(account_ids),
                "remaining_account_ids": list(account_ids),
                "reserved_account_ids": list(account_ids),
            }
            TASKS[serial] = task
            thread = threading.Thread(
                target=_run_device_queue,
                args=(task, account_ids, dict(settings)),
                daemon=True,
                name=f"taptap-{serial}",
            )
            threads.append(thread)
            started.append(serial)
        if not started:
            message = "所选设备没有可运行的账号"
            if skipped_assigned:
                message += "；部分账号已分配给其他设备"
            return {"ok": False, "message": message}
        for thread in threads:
            thread.start()

    message = f"已启动 {len(started)} 个设备线程，共分配 {sum(len(queues[s]) for s in started)} 个账号"
    if skipped_assigned:
        message += f"；跳过 {len(skipped_assigned)} 个分配给其他设备的账号"
    return {"ok": True, "batch_id": batch_id, "serials": started, "message": message}


def _task_summary(task: dict) -> dict:
    return {
        "id": task["id"],
        "batch_id": task.get("batch_id"),
        "serial": task["serial"],
        "status": task["status"],
        "started": task["started"],
        "current_phone": task.get("current_phone", ""),
        "progress": task.get("progress", 0),
        "total": task.get("total", 0),
        "completed": task.get("completed", 0),
        "failures": task.get("failures", 0),
    }


def api_task_overview() -> dict:
    with _TASK_LOCK:
        tasks = [_task_summary(task) for task in TASKS.values()]
    return {"ok": True, "tasks": sorted(tasks, key=lambda item: item["serial"])}


def api_task_state(serial: str, offset: int = 0) -> dict:
    with _TASK_LOCK:
        task = TASKS.get(serial)
        if not task:
            return {"task": None}
        line_base = task.get("line_base", 0)
        line_total = line_base + len(task["lines"])
        reset = offset < line_base or offset > line_total
        if reset:
            offset = line_base
        result = _task_summary(task)
        result["lines"] = task["lines"][offset - line_base:]
        result["line_total"] = line_total
        result["reset"] = reset
        return {"task": result}


def api_task_stop(serials=None) -> dict:
    requested = set(serials or []) if not isinstance(serials, str) else {serials}
    stopped = []
    with _TASK_LOCK:
        for serial, task in TASKS.items():
            if requested and serial not in requested:
                continue
            if task["status"] != "running":
                continue
            task["stop_event"].set()
            proc = task.get("proc")
            if proc:
                try:
                    proc.kill()
                except OSError:
                    pass
            stopped.append(serial)
    if stopped:
        return {"ok": True, "message": f"已停止 {len(stopped)} 个设备任务", "serials": stopped}
    return {"ok": False, "message": "当前没有匹配的运行中任务"}


def api_task_clear_log(serial: str) -> dict:
    if not serial:
        return {"ok": False, "message": "请选择要清空日志的设备"}
    with _TASK_LOCK:
        task = TASKS.get(serial)
        if task:
            task["lines"] = []
            task["line_base"] = 0
    try:
        os.makedirs(DEVICE_LOGS_DIR, exist_ok=True)
        with _DEVICE_LOG_LOCK, open(_device_log_path(serial), "w", encoding="utf-8"):
            pass
    except OSError as exc:
        return {"ok": False, "message": f"日志清空失败: {exc}"}
    return {"ok": True, "message": f"已清空设备 {serial} 的日志"}


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

        if path == "/api/settings":
            return self._send_json(api_settings_get())

        if path == "/api/accounts":
            return self._send_json(api_accounts_get())

        if path == "/api/task":
            offset = 0
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if qs.get("offset"):
                try:
                    offset = max(0, int(qs["offset"][0]))
                except ValueError:
                    offset = 0
            serial = (qs.get("serial") or [""])[0]
            if serial:
                return self._send_json(api_task_state(serial, offset))
            return self._send_json(api_task_overview())

        if path.startswith("/api/devices/") and path.endswith("/screenshot"):
            serial = urllib.parse.unquote(path[len("/api/devices/"):-len("/screenshot")])
            return self._send_screenshot(serial)

        self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 2 * 1024 * 1024:
            return self._send_json({"ok": False, "message": "请求内容不能超过 2 MB"}, 413)
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
            return self._send_json(api_task_run(data.get("serials") or data.get("serial", "")))
        if path == "/api/task/stop":
            return self._send_json(api_task_stop(data.get("serials") or data.get("serial")))
        if path == "/api/task/log/clear":
            return self._send_json(api_task_clear_log(data.get("serial", "")))
        if path == "/api/settings":
            return self._send_json(api_settings_save(data))
        if path == "/api/accounts/import":
            return self._send_json(api_accounts_import(
                data.get("filename", ""),
                data.get("content", ""),
            ))
        if path == "/api/accounts/update":
            return self._send_json(api_account_update(
                data.get("id", ""),
                data.get("selected") if "selected" in data else None,
                data.get("assigned_device") if "assigned_device" in data else None,
            ))
        if path == "/api/accounts/select-all":
            return self._send_json(api_accounts_select_all(bool(data.get("selected"))))
        if path == "/api/accounts/clear":
            return self._send_json(api_accounts_clear())
        if path == "/api/account/select":
            return self._send_json(api_accounts_import(
                data.get("filename", ""),
                data.get("content", ""),
            ))
        if path == "/api/account/clear":
            return self._send_json(api_accounts_clear())
        if path == "/api/disconnect":
            return self._send_json(api_disconnect(data.get("address", "")))
        if path == "/api/tcpip":
            return self._send_json(api_tcpip(data.get("serial", "")))

        # 屏幕输入控制：/api/devices/<serial>/tap|swipe|key
        m = re.fullmatch(r"/api/devices/([^/]+)/(tap|swipe|key|dump-ui)", path)
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
            if action == "dump-ui":
                return self._send_json(api_dump_ui_elements(serial))

        self._send_json({"error": "not found"}, 404)

    # ---- 截图 ----
    def _send_screenshot(self, serial: str):
        data, error = capture_screenshot(serial)
        if data is None:
            return self._send_json({"error": error or "截图失败（设备可能离线）"}, 503)
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
    parser.add_argument("--adb", help="adb 可执行文件或 platform-tools 目录；默认自动查找")
    args = parser.parse_args()

    ADB_PATH = find_adb(args.adb, base_dirs=[PROJECT_DIR])
    if not ADB_PATH:
        print(
            "[FAIL] 找不到 adb。请将 platform-tools 放入项目目录、加入 PATH，"
            "或通过 --adb/ADB_PATH 指定。"
        )
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
