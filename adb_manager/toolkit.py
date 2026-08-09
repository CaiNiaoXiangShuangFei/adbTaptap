"""ADB 设备工具箱：应用、文件、Logcat、录制回放与脚本插件。"""

from __future__ import annotations

import collections
import base64
import json
import os
import re
import shlex
import subprocess
import threading
import time
import uuid


ADB_PATH = "adb"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "toolkit_data")
PLUGIN_DIR = os.path.join(BASE_DIR, "plugins")
RECORDINGS_PATH = os.path.join(DATA_DIR, "recordings.json")
PLUGIN_STATE_PATH = os.path.join(DATA_DIR, "plugins.json")

_lock = threading.RLock()
_logcat_tasks: dict[str, dict] = {}
_replay_tasks: dict[str, dict] = {}
_plugin_tasks: dict[str, dict] = {}


def configure(adb_path: str, base_dir: str | None = None) -> None:
    global ADB_PATH, BASE_DIR, DATA_DIR, PLUGIN_DIR, RECORDINGS_PATH, PLUGIN_STATE_PATH
    ADB_PATH = adb_path
    if base_dir:
        BASE_DIR = base_dir
        DATA_DIR = os.path.join(BASE_DIR, "toolkit_data")
        PLUGIN_DIR = os.path.join(BASE_DIR, "plugins")
        RECORDINGS_PATH = os.path.join(DATA_DIR, "recordings.json")
        PLUGIN_STATE_PATH = os.path.join(DATA_DIR, "plugins.json")
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(PLUGIN_DIR, exist_ok=True)


def _adb(serial: str, *args, timeout: int = 30, binary: bool = False):
    command = [ADB_PATH, "-s", serial, *map(str, args)]
    try:
        return subprocess.run(
            command,
            capture_output=True,
            timeout=timeout,
            text=not binary,
            errors=None if binary else "replace",
        )
    except Exception as exc:
        return exc


def _result(result, success_message: str) -> dict:
    if isinstance(result, Exception):
        return {"ok": False, "message": str(result)}
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    lowered = output.lower()
    failed_output = any(marker in lowered for marker in (
        "failure [", "error:", "failed to", "no activities found", "monkey aborted",
    ))
    succeeded = result.returncode == 0 and not failed_output
    return {
        "ok": succeeded,
        "message": success_message if succeeded else (output or f"ADB 退出码 {result.returncode}"),
        "output": output,
    }


# ---------- APK 与应用 ----------

def list_apps(serial: str, scope: str = "third_party") -> dict:
    flag = "-3" if scope != "all" else ""
    args = ["shell", "pm", "list", "packages"] + ([flag] if flag else [])
    result = _adb(serial, *args, timeout=20)
    parsed = _result(result, "应用列表已刷新")
    if not parsed["ok"]:
        return parsed
    packages = sorted({
        line.split(":", 1)[1].strip()
        for line in (result.stdout or "").splitlines()
        if line.strip().startswith("package:")
    })
    return {"ok": True, "packages": packages, "count": len(packages), "scope": scope}


def app_action(serial: str, package: str, action: str) -> dict:
    package = str(package or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.]+", package):
        return {"ok": False, "message": "应用包名格式不正确"}
    commands = {
        "launch": ("shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"),
        "stop": ("shell", "am", "force-stop", package),
        "clear": ("shell", "pm", "clear", package),
        "uninstall": ("uninstall", package),
    }
    command = commands.get(action)
    if not command:
        return {"ok": False, "message": "不支持的应用操作"}
    return _result(_adb(serial, *command, timeout=60), f"已执行 {action}: {package}")


def install_apk(serial: str, local_path: str) -> dict:
    return _result(_adb(serial, "install", "-r", local_path, timeout=300), "APK 安装成功")


# ---------- 文件管理 ----------

def normalize_remote_path(path: str, allow_root: bool = True) -> str:
    value = str(path or "/sdcard").strip().replace("\\", "/")
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError("远程路径包含非法字符")
    if not value.startswith("/"):
        value = "/sdcard/" + value
    value = os.path.normpath(value).replace("\\", "/")
    allowed = value == "/sdcard" or value.startswith("/sdcard/") or value == "/storage/emulated/0" or value.startswith("/storage/emulated/0/")
    if not allowed:
        raise ValueError("文件管理仅允许访问手机共享存储目录")
    if not allow_root and value in {"/sdcard", "/storage/emulated/0"}:
        raise ValueError("不能修改共享存储根目录")
    return value


def list_files(serial: str, path: str) -> dict:
    try:
        remote = normalize_remote_path(path)
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}
    command = f"ls -1Ap {shlex.quote(remote)}"
    result = _adb(serial, "shell", command, timeout=20)
    parsed = _result(result, "文件列表已刷新")
    if not parsed["ok"]:
        return parsed
    entries = []
    for raw in (result.stdout or "").splitlines():
        name = raw.strip()
        if not name or name in {"./", "../"}:
            continue
        is_dir = name.endswith("/")
        clean_name = name[:-1] if is_dir else name
        entries.append({"name": clean_name, "directory": is_dir})
    parent = None
    if remote not in {"/sdcard", "/storage/emulated/0"}:
        parent = os.path.dirname(remote).replace("\\", "/") or "/sdcard"
    return {"ok": True, "path": remote, "parent": parent, "entries": entries}


def push_file(serial: str, local_path: str, remote_directory: str, filename: str) -> dict:
    try:
        directory = normalize_remote_path(remote_directory)
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}
    safe_name = os.path.basename(str(filename or "").replace("\\", "/")).strip()
    if not safe_name or safe_name in {".", ".."}:
        return {"ok": False, "message": "文件名无效"}
    remote = directory.rstrip("/") + "/" + safe_name
    return _result(_adb(serial, "push", local_path, remote, timeout=300), f"文件已上传到 {remote}")


def file_action(serial: str, path: str, action: str, name: str = "") -> dict:
    try:
        remote = normalize_remote_path(path, allow_root=action == "mkdir")
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}
    if action == "delete":
        command = f"rm -rf -- {shlex.quote(remote)}"
        return _result(_adb(serial, "shell", command, timeout=60), f"已删除 {remote}")
    if action == "mkdir":
        clean = os.path.basename(str(name or "").strip().replace("\\", "/"))
        if not clean or clean in {".", ".."}:
            return {"ok": False, "message": "请输入有效的文件夹名称"}
        target = remote.rstrip("/") + "/" + clean
        command = f"mkdir -p {shlex.quote(target)}"
        return _result(_adb(serial, "shell", command, timeout=30), f"已创建 {target}")
    return {"ok": False, "message": "不支持的文件操作"}


def read_remote_file(serial: str, path: str, max_bytes: int = 256 * 1024 * 1024):
    try:
        remote = normalize_remote_path(path, allow_root=False)
    except ValueError as exc:
        return None, str(exc)
    result = _adb(serial, "exec-out", "cat", remote, timeout=300, binary=True)
    if isinstance(result, Exception):
        return None, str(result)
    if result.returncode != 0:
        return None, (result.stderr or b"").decode("utf-8", errors="replace") or "文件读取失败"
    data = result.stdout or b""
    if len(data) > max_bytes:
        return None, "文件超过 256 MB，请使用 adb pull 传输"
    return data, None


# ---------- Logcat ----------

def _logcat_reader(task: dict) -> None:
    proc = task["proc"]
    try:
        for line in proc.stdout:
            with _lock:
                if len(task["lines"]) == task["lines"].maxlen:
                    task["base"] += 1
                task["lines"].append(line.rstrip("\r\n"))
            if task["stop"].is_set():
                break
        proc.wait()
    finally:
        with _lock:
            task["running"] = False
            task["proc"] = None


def logcat_control(serial: str, action: str) -> dict:
    with _lock:
        task = _logcat_tasks.get(serial)
        if action == "clear":
            _adb(serial, "logcat", "-c", timeout=15)
            if task:
                task["lines"].clear(); task["base"] = 0
            return {"ok": True, "message": "Logcat 已清空"}
        if action == "stop":
            if task:
                task["stop"].set()
                proc = task.get("proc")
                if proc:
                    try: proc.kill()
                    except OSError: pass
            return {"ok": True, "message": "Logcat 已停止"}
        if action != "start":
            return {"ok": False, "message": "不支持的 Logcat 操作"}
        if task and task.get("running"):
            return {"ok": True, "message": "Logcat 已在运行"}
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.Popen(
                [ADB_PATH, "-s", serial, "logcat", "-v", "threadtime"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=creationflags,
            )
        except OSError as exc:
            return {"ok": False, "message": str(exc)}
        task = {"serial": serial, "proc": proc, "stop": threading.Event(), "running": True, "lines": collections.deque(maxlen=5000), "base": 0}
        _logcat_tasks[serial] = task
        threading.Thread(target=_logcat_reader, args=(task,), daemon=True, name=f"logcat-{serial}").start()
    return {"ok": True, "message": "Logcat 实时日志已启动"}


def logcat_state(serial: str, offset: int = 0) -> dict:
    with _lock:
        task = _logcat_tasks.get(serial)
        if not task:
            return {"ok": True, "running": False, "lines": [], "offset": 0, "reset": False}
        total = task["base"] + len(task["lines"])
        reset = offset < task["base"] or offset > total
        if reset: offset = task["base"]
        lines = list(task["lines"])[offset - task["base"]:]
        return {"ok": True, "running": task["running"], "lines": lines, "offset": total, "reset": reset}


# ---------- 操作录制与回放 ----------

def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as file:
            value = json.load(file)
            return value
    except (OSError, ValueError):
        return default


def _write_json(path: str, value) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as file:
        json.dump(value, file, ensure_ascii=False, indent=2)
    os.replace(temp, path)


def recordings_list() -> dict:
    with _lock:
        items = _read_json(RECORDINGS_PATH, [])
    return {"ok": True, "recordings": items if isinstance(items, list) else []}


def recording_save(name: str, actions: list) -> dict:
    name = str(name or "").strip()[:80] or time.strftime("操作录制 %Y-%m-%d %H:%M:%S")
    if not isinstance(actions, list) or not actions or len(actions) > 2000:
        return {"ok": False, "message": "录制动作数量必须在 1 到 2000 之间"}
    cleaned = []
    allowed = {"tap", "swipe", "key", "text", "quick"}
    for item in actions:
        if not isinstance(item, dict) or item.get("action") not in allowed:
            continue
        cleaned.append({"delay": min(60000, max(0, int(item.get("delay") or 0))), "action": item["action"], "body": item.get("body") if isinstance(item.get("body"), dict) else {}})
    if not cleaned:
        return {"ok": False, "message": "录制中没有可回放的动作"}
    record = {"id": uuid.uuid4().hex, "name": name, "created_at": time.strftime("%Y-%m-%d %H:%M:%S"), "actions": cleaned, "count": len(cleaned)}
    with _lock:
        items = _read_json(RECORDINGS_PATH, [])
        if not isinstance(items, list): items = []
        items.insert(0, record)
        _write_json(RECORDINGS_PATH, items[:100])
    return {"ok": True, "message": f"已保存 {len(cleaned)} 个动作", "recording": record}


def recording_delete(recording_id: str) -> dict:
    with _lock:
        items = _read_json(RECORDINGS_PATH, [])
        kept = [item for item in items if item.get("id") != recording_id]
        _write_json(RECORDINGS_PATH, kept)
    return {"ok": len(kept) != len(items), "message": "录制已删除" if len(kept) != len(items) else "录制不存在"}


def _replay_action(serial: str, action: dict, target_size: tuple[int, int] | None = None) -> None:
    kind, body = action["action"], action.get("body") or {}
    source_width = int(body.get("source_width") or 0)
    source_height = int(body.get("source_height") or 0)
    scale_x = target_size[0] / source_width if target_size and source_width else 1
    scale_y = target_size[1] / source_height if target_size and source_height else 1
    if kind == "tap": _adb(serial, "shell", "input", "tap", round(int(body.get("x", 0)) * scale_x), round(int(body.get("y", 0)) * scale_y), timeout=10)
    elif kind == "swipe": _adb(serial, "shell", "input", "swipe", round(int(body.get("x1", 0)) * scale_x), round(int(body.get("y1", 0)) * scale_y), round(int(body.get("x2", 0)) * scale_x), round(int(body.get("y2", 0)) * scale_y), int(body.get("duration", 300)), timeout=10)
    elif kind == "key": _adb(serial, "shell", "input", "keyevent", int(body.get("key", 0)), timeout=10)
    elif kind == "text":
        text_value = str(body.get("text", ""))
        ime_result = _adb(serial, "shell", "ime", "list", "-s", timeout=10)
        ime_output = "" if isinstance(ime_result, Exception) else (ime_result.stdout or "").lower()
        if not text_value.isascii() and "com.android.adbkeyboard" in ime_output:
            encoded = base64.b64encode(text_value.encode("utf-8")).decode("ascii")
            _adb(serial, "shell", "am", "broadcast", "-a", "ADB_INPUT_B64", "--es", "msg", encoded, timeout=10)
        else:
            _adb(serial, "shell", "input", "text", text_value.replace("%", "\\%").replace(" ", "%s"), timeout=10)
    elif kind == "quick" and body.get("action") in {"home", "back", "recent", "wake", "sleep"}:
        code = {"home": 3, "back": 4, "recent": 187, "wake": 224, "sleep": 223}[body["action"]]
        _adb(serial, "shell", "input", "keyevent", code, timeout=10)


def _replay_worker(task: dict, recording: dict) -> None:
    try:
        size_result = _adb(task["serial"], "shell", "wm", "size", timeout=10)
        size_match = None if isinstance(size_result, Exception) else re.search(r"(\d+)x(\d+)", size_result.stdout or "")
        target_size = (int(size_match.group(1)), int(size_match.group(2))) if size_match else None
        for index, action in enumerate(recording["actions"], 1):
            if task["stop"].wait(action.get("delay", 0) / 1000): break
            _replay_action(task["serial"], action, target_size)
            with _lock: task["progress"] = index
        with _lock: task["status"] = "stopped" if task["stop"].is_set() else "success"
    except Exception as exc:
        with _lock: task["status"] = "failed"; task["error"] = str(exc)


def replay_run(recording_id: str, serials: list[str]) -> dict:
    recordings = recordings_list()["recordings"]
    recording = next((item for item in recordings if item.get("id") == recording_id), None)
    targets = list(dict.fromkeys(str(item).strip() for item in (serials or []) if str(item).strip()))
    if not recording or not targets:
        return {"ok": False, "message": "请选择有效录制和设备"}
    started = []
    with _lock:
        for serial in targets:
            task_id = uuid.uuid4().hex
            task = {"id": task_id, "serial": serial, "recording_id": recording_id, "name": recording["name"], "status": "running", "progress": 0, "total": len(recording["actions"]), "stop": threading.Event(), "error": ""}
            _replay_tasks[task_id] = task
            threading.Thread(target=_replay_worker, args=(task, recording), daemon=True, name=f"replay-{serial}").start()
            started.append(task_id)
    return {"ok": True, "message": f"已在 {len(started)} 台设备开始回放", "task_ids": started}


def replay_state() -> dict:
    with _lock:
        tasks = [{key: value for key, value in task.items() if key != "stop"} for task in _replay_tasks.values()]
    return {"ok": True, "tasks": tasks}


def replay_stop(task_ids: list[str] | None = None) -> dict:
    wanted = set(task_ids or [])
    count = 0
    with _lock:
        for task_id, task in _replay_tasks.items():
            if wanted and task_id not in wanted: continue
            if task["status"] == "running": task["stop"].set(); count += 1
    return {"ok": True, "message": f"已停止 {count} 个回放任务"}


# ---------- 脚本插件 ----------

def discover_plugins() -> list[dict]:
    os.makedirs(PLUGIN_DIR, exist_ok=True)
    enabled = set(_read_json(PLUGIN_STATE_PATH, {}).get("enabled", []))
    result = []
    for name in sorted(os.listdir(PLUGIN_DIR)):
        folder = os.path.join(PLUGIN_DIR, name)
        manifest_path = os.path.join(folder, "plugin.json")
        if not os.path.isdir(folder) or not os.path.isfile(manifest_path): continue
        manifest = _read_json(manifest_path, {})
        plugin_id = str(manifest.get("id") or name)
        entry = str(manifest.get("entry") or "run.py")
        entry_path = os.path.abspath(os.path.join(folder, entry))
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", plugin_id) or os.path.commonpath([folder, entry_path]) != os.path.abspath(folder) or not os.path.isfile(entry_path):
            continue
        result.append({"id": plugin_id, "name": str(manifest.get("name") or plugin_id), "description": str(manifest.get("description") or ""), "version": str(manifest.get("version") or "1.0.0"), "enabled": plugin_id in enabled, "entry": entry, "folder": folder})
    return result


def plugins_list() -> dict:
    public_keys = ("id", "name", "description", "version", "enabled")
    return {
        "ok": True,
        "plugins": [
            {key: plugin[key] for key in public_keys}
            for plugin in discover_plugins()
        ],
    }


def plugin_toggle(plugin_id: str, enabled: bool) -> dict:
    available = {item["id"] for item in discover_plugins()}
    if plugin_id not in available: return {"ok": False, "message": "插件不存在或清单无效"}
    with _lock:
        state = _read_json(PLUGIN_STATE_PATH, {})
        selected = set(state.get("enabled", []))
        if enabled: selected.add(plugin_id)
        else: selected.discard(plugin_id)
        _write_json(PLUGIN_STATE_PATH, {"enabled": sorted(selected)})
    return {"ok": True, "message": "插件已启用" if enabled else "插件已停用"}


def _plugin_worker(task: dict, plugins: list[dict]) -> None:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        for plugin in plugins:
            if task["stop"].is_set(): break
            with _lock: task["current"] = plugin["name"]
            command = [os.environ.get("PYTHON_EXECUTABLE") or os.sys.executable, os.path.join(plugin["folder"], plugin["entry"]), "--device", task["serial"], "--adb", ADB_PATH]
            proc = subprocess.Popen(command, cwd=plugin["folder"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1, creationflags=creationflags)
            with _lock: task["proc"] = proc
            for line in proc.stdout:
                with _lock:
                    if len(task["lines"]) == task["lines"].maxlen: task["base"] += 1
                    task["lines"].append(f"[{plugin['name']}] {line.rstrip()}")
            code = proc.wait()
            with _lock: task["proc"] = None; task["progress"] += 1
            if task["stop"].is_set():
                break
            if code != 0:
                raise RuntimeError(f"插件 {plugin['name']} 退出码 {code}")
        with _lock: task["status"] = "stopped" if task["stop"].is_set() else "success"
    except Exception as exc:
        with _lock: task["status"] = "failed"; task["error"] = str(exc); task["proc"] = None


def plugin_run(serials: list[str], plugin_ids: list[str] | None = None) -> dict:
    plugins = discover_plugins()
    wanted = set(plugin_ids or [])
    selected = [item for item in plugins if item["enabled"] and (not wanted or item["id"] in wanted)]
    targets = list(dict.fromkeys(str(item).strip() for item in (serials or []) if str(item).strip()))
    if not selected: return {"ok": False, "message": "没有已启用的插件"}
    if not targets: return {"ok": False, "message": "请选择运行设备"}
    task_ids = []
    with _lock:
        for serial in targets:
            task_id = uuid.uuid4().hex
            task = {"id": task_id, "serial": serial, "status": "running", "current": "", "progress": 0, "total": len(selected), "error": "", "lines": collections.deque(maxlen=3000), "base": 0, "stop": threading.Event(), "proc": None}
            _plugin_tasks[task_id] = task
            threading.Thread(target=_plugin_worker, args=(task, selected), daemon=True, name=f"plugin-{serial}").start()
            task_ids.append(task_id)
    return {"ok": True, "message": f"已在 {len(targets)} 台设备运行 {len(selected)} 个插件", "task_ids": task_ids}


def plugin_tasks(offsets: dict | None = None) -> dict:
    offsets = offsets or {}
    output = []
    with _lock:
        for task in _plugin_tasks.values():
            offset = int(offsets.get(task["id"], 0) or 0)
            total_lines = task["base"] + len(task["lines"])
            reset = offset < task["base"] or offset > total_lines
            if reset: offset = task["base"]
            output.append({"id": task["id"], "serial": task["serial"], "status": task["status"], "current": task["current"], "progress": task["progress"], "total": task["total"], "error": task["error"], "lines": list(task["lines"])[offset-task["base"]:], "offset": total_lines, "reset": reset})
    return {"ok": True, "tasks": output}


def plugin_stop(task_ids: list[str] | None = None) -> dict:
    wanted = set(task_ids or [])
    count = 0
    with _lock:
        for task_id, task in _plugin_tasks.items():
            if wanted and task_id not in wanted: continue
            if task["status"] != "running": continue
            task["stop"].set(); count += 1
            proc = task.get("proc")
            if proc:
                try: proc.kill()
                except OSError: pass
    return {"ok": True, "message": f"已停止 {count} 个插件任务"}
