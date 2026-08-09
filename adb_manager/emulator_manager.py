"""项目内置 Android Emulator 管理器（运行时与 AVD 数据始终避开 C 盘）。"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import zipfile


def _preferred_runtime_dir(project_dir: str) -> str:
    """Keep emulator payloads off C:, while staying project-local on D:."""
    override = os.environ.get("ADBTAPTAP_RUNTIME_DIR", "").strip()
    if override:
        return os.path.abspath(override)
    project_dir = os.path.abspath(project_dir)
    drive = os.path.splitdrive(project_dir)[0].upper()
    if drive == "D:":
        return os.path.join(project_dir, "runtime")
    if os.path.isdir("D:\\"):
        project_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", os.path.basename(project_dir)).strip("._") or "adbTaptap"
        return os.path.join("D:\\", "adbTaptap-runtime", project_name[:48])
    if drive == "C:":
        raise RuntimeError("未检测到 D 盘。为避免写入 C 盘，虚拟安卓运行时不会启动安装")
    return os.path.join(project_dir, "runtime")


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)
RUNTIME_DIR = _preferred_runtime_dir(PROJECT_DIR)
SDK_DIR = os.path.join(RUNTIME_DIR, "android-sdk")
AVD_HOME = os.path.join(RUNTIME_DIR, "avd")
ANDROID_USER_HOME = os.path.join(RUNTIME_DIR, "android-home")
JDK_DIR = os.path.join(RUNTIME_DIR, "jdk")
DOWNLOAD_DIR = os.path.join(RUNTIME_DIR, "downloads")
TEMP_DIR = os.path.join(RUNTIME_DIR, "temp")
PROFILE_PATH = os.path.join(RUNTIME_DIR, "emulator_profiles.json")
INSTALL_LOCK_PATH = os.path.join(RUNTIME_DIR, "emulator_install.lock")
DEFAULT_SYSTEM_IMAGE = "system-images;android-35;google_apis;x86_64"
REPOSITORY_URL = "https://dl.google.com/android/repository/repository2-1.xml"
JDK_URL = "https://api.adoptium.net/v3/binary/latest/17/ga/windows/x64/jre/hotspot/normal/eclipse"

ADB_PATH = "adb"
_lock = threading.RLock()
_install_task = {"status": "idle", "progress": 0, "message": "尚未安装", "lines": [], "error": ""}
_processes: dict[str, subprocess.Popen] = {}


def configure(adb_path: str, project_dir: str | None = None) -> None:
    global ADB_PATH, PROJECT_DIR, RUNTIME_DIR, SDK_DIR, AVD_HOME, ANDROID_USER_HOME
    global JDK_DIR, DOWNLOAD_DIR, TEMP_DIR, PROFILE_PATH, INSTALL_LOCK_PATH
    ADB_PATH = adb_path
    if project_dir:
        PROJECT_DIR = os.path.abspath(project_dir)
        RUNTIME_DIR = _preferred_runtime_dir(PROJECT_DIR)
        SDK_DIR = os.path.join(RUNTIME_DIR, "android-sdk")
        AVD_HOME = os.path.join(RUNTIME_DIR, "avd")
        ANDROID_USER_HOME = os.path.join(RUNTIME_DIR, "android-home")
        JDK_DIR = os.path.join(RUNTIME_DIR, "jdk")
        DOWNLOAD_DIR = os.path.join(RUNTIME_DIR, "downloads")
        TEMP_DIR = os.path.join(RUNTIME_DIR, "temp")
        PROFILE_PATH = os.path.join(RUNTIME_DIR, "emulator_profiles.json")
        INSTALL_LOCK_PATH = os.path.join(RUNTIME_DIR, "emulator_install.lock")
    for folder in (RUNTIME_DIR, SDK_DIR, AVD_HOME, ANDROID_USER_HOME, DOWNLOAD_DIR, TEMP_DIR):
        os.makedirs(folder, exist_ok=True)


def runtime_env() -> dict:
    env = dict(os.environ)
    local_home = os.path.join(RUNTIME_DIR, "home")
    sdk_home = os.path.join(RUNTIME_DIR, "sdk-home")
    roaming = os.path.join(local_home, "AppData", "Roaming")
    local_appdata = os.path.join(local_home, "AppData", "Local")
    for folder in (local_home, sdk_home, TEMP_DIR, ANDROID_USER_HOME, AVD_HOME, roaming, local_appdata):
        os.makedirs(folder, exist_ok=True)
    env.update({
        "ANDROID_HOME": SDK_DIR,
        "ANDROID_SDK_ROOT": SDK_DIR,
        "ANDROID_USER_HOME": ANDROID_USER_HOME,
        "ANDROID_AVD_HOME": AVD_HOME,
        "ANDROID_SDK_HOME": sdk_home,
        "JAVA_HOME": JDK_DIR,
        "HOME": local_home,
        "USERPROFILE": local_home,
        "TEMP": TEMP_DIR,
        "TMP": TEMP_DIR,
        "APPDATA": roaming,
        "LOCALAPPDATA": local_appdata,
        "GRADLE_USER_HOME": os.path.join(RUNTIME_DIR, "gradle-home"),
    })
    env["PATH"] = os.pathsep.join([
        os.path.join(JDK_DIR, "bin"),
        os.path.join(SDK_DIR, "platform-tools"),
        os.path.join(SDK_DIR, "emulator"),
        env.get("PATH", ""),
    ])
    return env


def _exe(*parts: str) -> str:
    return os.path.join(SDK_DIR, *parts)


def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, ValueError):
        return default


def _write_json(path: str, value) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as file:
        json.dump(value, file, ensure_ascii=False, indent=2)
    os.replace(temp, path)


def _log(message: str, progress: int | None = None) -> None:
    with _lock:
        _install_task["message"] = message
        if progress is not None:
            _install_task["progress"] = progress
        _install_task["lines"].append(f"[{time.strftime('%H:%M:%S')}] {message}")
        _install_task["lines"] = _install_task["lines"][-300:]


def _install_lock_active() -> bool:
    try:
        with open(INSTALL_LOCK_PATH, "r", encoding="ascii") as file:
            pid = int(file.read().strip())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def _download(url: str, target: str, title: str) -> None:
    os.makedirs(os.path.dirname(target), exist_ok=True)
    partial = target + ".part"
    existing = os.path.getsize(partial) if os.path.isfile(partial) else 0
    headers = {"User-Agent": "adbTaptap-emulator-bootstrap/1.0"}
    if existing:
        headers["Range"] = f"bytes={existing}-"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        resumed = existing > 0 and getattr(response, "status", 200) == 206
        if not resumed:
            existing = 0
        output_mode = "ab" if resumed else "wb"
        output = open(partial, output_mode)
        try:
            total = int(response.headers.get("Content-Length") or 0) + existing
            received = existing
            last_report = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                received += len(chunk)
                now = time.time()
                if now - last_report > 2:
                    detail = f"{received / 1024 / 1024:.0f} MB"
                    if total:
                        detail += f" / {total / 1024 / 1024:.0f} MB"
                    _log(f"正在下载 {title}: {detail}")
                    last_report = now
        finally:
            output.close()
    os.replace(partial, target)


def _extract_single_root(zip_path: str, target: str, required_relative: str) -> None:
    extract_dir = os.path.join(TEMP_DIR, "extract_" + uuid.uuid4().hex)
    os.makedirs(extract_dir, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as archive:
            for info in archive.infolist():
                destination = os.path.abspath(os.path.join(extract_dir, info.filename))
                if os.path.commonpath([extract_dir, destination]) != os.path.abspath(extract_dir):
                    raise RuntimeError("压缩包包含不安全路径")
            archive.extractall(extract_dir)
        source_root = None
        for current, _, _ in os.walk(extract_dir):
            if os.path.isfile(os.path.join(current, required_relative)):
                source_root = current
                break
        if not source_root:
            raise RuntimeError(f"压缩包缺少 {required_relative}")
        if os.path.isdir(target):
            shutil.rmtree(target)
        shutil.copytree(source_root, target)
    finally:
        if os.path.isdir(extract_dir):
            shutil.rmtree(extract_dir, ignore_errors=True)


def _latest_commandline_tools_url() -> str:
    request = urllib.request.Request(REPOSITORY_URL, headers={"User-Agent": "adbTaptap-emulator-bootstrap/1.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        root = ET.fromstring(response.read())
    candidates = []
    for package in root.iter():
        if not package.tag.endswith("remotePackage"):
            continue
        path = package.attrib.get("path", "")
        if not path.startswith("cmdline-tools;"):
            continue
        revision = []
        for node in package.iter():
            tag = node.tag.rsplit("}", 1)[-1]
            if tag in {"major", "minor", "micro"} and node.text and node.text.isdigit():
                revision.append(int(node.text))
        for archive in package.iter():
            if not archive.tag.endswith("archive"):
                continue
            host_os = ""
            archive_url = ""
            for node in archive.iter():
                tag = node.tag.rsplit("}", 1)[-1]
                if tag == "host-os":
                    host_os = (node.text or "").strip()
                elif tag == "url":
                    archive_url = (node.text or "").strip()
            if host_os == "windows" and archive_url:
                candidates.append((tuple(revision[:3]), archive_url))
    if not candidates:
        raise RuntimeError("无法从 Google SDK 仓库找到 Windows Command-line Tools")
    archive = max(candidates, key=lambda item: item[0])[1]
    return "https://dl.google.com/android/repository/" + archive


def _run(command: list[str], *, input_text: str | None = None, timeout: int = 1800) -> subprocess.CompletedProcess:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(
        command,
        cwd=PROJECT_DIR,
        env=runtime_env(),
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=creationflags,
    )


def _install_worker(system_image: str) -> None:
    lock_created = False
    try:
        try:
            lock_fd = os.open(INSTALL_LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if _install_lock_active():
                raise RuntimeError("另一个虚拟安卓安装任务正在运行")
            os.remove(INSTALL_LOCK_PATH)
            lock_fd = os.open(INSTALL_LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(lock_fd, "w", encoding="ascii") as lock_file:
            lock_file.write(str(os.getpid()))
        lock_created = True
        _log("准备项目内 Android Emulator 运行环境", 2)
        java_exe = os.path.join(JDK_DIR, "bin", "java.exe")
        if not os.path.isfile(java_exe):
            jdk_zip = os.path.join(DOWNLOAD_DIR, "temurin17-jre.zip")
            _download(JDK_URL, jdk_zip, "Temurin JRE 17")
            _log(f"正在解压 Java 运行时到 {JDK_DIR}", 15)
            _extract_single_root(jdk_zip, JDK_DIR, os.path.join("bin", "java.exe"))
        _log("Java 运行时已就绪", 22)

        sdkmanager = _exe("cmdline-tools", "latest", "bin", "sdkmanager.bat")
        if not os.path.isfile(sdkmanager):
            tools_zip = os.path.join(DOWNLOAD_DIR, "android-commandline-tools.zip")
            _download(_latest_commandline_tools_url(), tools_zip, "Android Command-line Tools")
            _log("正在解压 Android Command-line Tools", 32)
            _extract_single_root(tools_zip, os.path.join(SDK_DIR, "cmdline-tools", "latest"), os.path.join("bin", "sdkmanager.bat"))
        _log("Android 命令行工具已就绪", 38)

        packages = ["platform-tools", "emulator", system_image]
        _log("正在下载 Emulator、Platform Tools 和 Android 系统镜像（文件较大）", 45)
        result = _run([sdkmanager, f"--sdk_root={SDK_DIR}", *packages], input_text="y\n" * 200, timeout=3600)
        combined = ((result.stdout or "") + (result.stderr or "")).strip()
        if result.returncode != 0:
            raise RuntimeError(combined[-4000:] or f"sdkmanager 退出码 {result.returncode}")
        _log("正在确认 Android SDK 许可", 92)
        _run([sdkmanager, f"--sdk_root={SDK_DIR}", "--licenses"], input_text="y\n" * 300, timeout=600)
        ready = runtime_status()
        if not all(ready[key] for key in ("emulator_ready", "adb_ready", "jdk_ready", "system_image_ready")):
            raise RuntimeError("安装完成后运行时文件校验未通过，请再次点击安装以补全")
        with _lock:
            _install_task.update({"status": "success", "progress": 100, "message": "内置虚拟安卓运行时已安装", "error": ""})
        _log(f"内置虚拟安卓运行时已安装到 {RUNTIME_DIR}", 100)
    except Exception as exc:
        with _lock:
            _install_task.update({"status": "failed", "message": "安装失败", "error": str(exc)})
        _log(f"安装失败: {exc}")
    finally:
        if lock_created:
            try:
                os.remove(INSTALL_LOCK_PATH)
            except OSError:
                pass


def install_runtime(system_image: str = DEFAULT_SYSTEM_IMAGE) -> dict:
    if not re.fullmatch(r"system-images;android-\d+;[A-Za-z0-9_]+;[A-Za-z0-9_]+", system_image or ""):
        return {"ok": False, "message": "系统镜像名称不合法"}
    with _lock:
        if _install_task["status"] == "running":
            return {"ok": True, "message": "安装任务已在运行"}
        _install_task.update({"status": "running", "progress": 0, "message": "正在启动安装", "lines": [], "error": ""})
        threading.Thread(target=_install_worker, args=(system_image,), daemon=True, name="android-emulator-install").start()
    return {"ok": True, "message": f"已开始下载，所有文件将保存到 {RUNTIME_DIR}"}


def install_state() -> dict:
    with _lock:
        task = dict(_install_task)
    if _install_lock_active() and task.get("status") != "running":
        task.update({"status": "running", "message": "另一个安装进程正在补全运行时"})
    task.update(runtime_status())
    return {"ok": True, "install": task}


def runtime_status() -> dict:
    return {
        "runtime_dir": RUNTIME_DIR,
        "runtime_drive": os.path.splitdrive(os.path.abspath(RUNTIME_DIR))[0],
        "sdk_ready": os.path.isfile(_exe("cmdline-tools", "latest", "bin", "sdkmanager.bat")),
        "emulator_ready": os.path.isfile(_exe("emulator", "emulator.exe")),
        "adb_ready": os.path.isfile(_exe("platform-tools", "adb.exe")),
        "jdk_ready": os.path.isfile(os.path.join(JDK_DIR, "bin", "java.exe")),
        "system_image_ready": all(os.path.isfile(_exe("system-images", "android-35", "google_apis", "x86_64", name)) for name in ("package.xml", "system.img")),
    }


def _profiles() -> list[dict]:
    value = _read_json(PROFILE_PATH, [])
    return value if isinstance(value, list) else []


def _running_avds() -> dict[str, str]:
    result = {}
    try:
        devices = subprocess.run([ADB_PATH, "devices"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=8)
        for line in devices.stdout.splitlines():
            serial = line.split()[0] if line.strip() else ""
            if not serial.startswith("emulator-"):
                continue
            name_result = subprocess.run([ADB_PATH, "-s", serial, "emu", "avd", "name"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5)
            name = next((item.strip() for item in name_result.stdout.splitlines() if item.strip() and item.strip() != "OK"), "")
            if name:
                result[name] = serial
    except (OSError, subprocess.SubprocessError):
        pass
    return result


def profiles_list() -> dict:
    running = _running_avds()
    profiles = []
    for item in _profiles():
        copy = dict(item)
        copy["running"] = item["name"] in running
        copy["serial"] = running.get(item["name"], "")
        profiles.append(copy)
    return {"ok": True, "profiles": profiles, **runtime_status()}


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip()).strip("._")
    return cleaned[:48]


def _profile_values(data: dict) -> tuple[str, str, int, int, int, int]:
    width = min(2160, max(480, int(data.get("width") or 1080)))
    height = min(3840, max(800, int(data.get("height") or 2400)))
    dpi = min(640, max(160, int(data.get("dpi") or 420)))
    memory = min(8192, max(1536, int(data.get("memory") or 3072)))
    brand_label = re.sub(r"[\r\n=]+", " ", str(data.get("brand_label") or "Google")).strip()[:40]
    model_label = re.sub(r"[\r\n=]+", " ", str(data.get("model_label") or "Pixel Test Device")).strip()[:60]
    return brand_label, model_label, width, height, dpi, memory


def _update_avd_config(config_path: str, values: dict[str, str | int]) -> None:
    lines = []
    try:
        with open(config_path, "r", encoding="utf-8") as file:
            lines = file.read().splitlines()
    except OSError:
        pass
    remaining = dict(values)
    updated = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in remaining:
            updated.append(f"{key}={remaining.pop(key)}")
        else:
            updated.append(line)
    updated.extend(f"{key}={value}" for key, value in remaining.items())
    with open(config_path, "w", encoding="utf-8", newline="\n") as file:
        file.write("\n".join(updated).rstrip() + "\n")


def create_profile(data: dict) -> dict:
    status = runtime_status()
    if not status["emulator_ready"] or not status["system_image_ready"]:
        return {"ok": False, "message": "请先安装内置虚拟安卓运行时"}
    name = _safe_name(data.get("name") or f"TapTap_Test_{int(time.time())}")
    if not name:
        return {"ok": False, "message": "虚拟手机名称无效"}
    profiles = _profiles()
    if any(item.get("name") == name for item in profiles):
        return {"ok": False, "message": "同名虚拟手机已经存在"}
    try:
        brand_label, model_label, width, height, dpi, memory = _profile_values(data)
    except (TypeError, ValueError):
        return {"ok": False, "message": "分辨率、DPI 或内存配置不正确"}
    avdmanager = _exe("cmdline-tools", "latest", "bin", "avdmanager.bat")
    avd_path = os.path.abspath(os.path.join(AVD_HOME, name + ".avd"))
    if os.path.commonpath([AVD_HOME, avd_path]) != os.path.abspath(AVD_HOME):
        return {"ok": False, "message": "AVD 路径不安全"}
    command = [avdmanager, "create", "avd", "--force", "--name", name, "--package", DEFAULT_SYSTEM_IMAGE, "--device", "pixel_7", "--path", avd_path]
    result = _run(command, input_text="no\n", timeout=180)
    if result.returncode != 0:
        command[command.index("pixel_7")] = "pixel"
        result = _run(command, input_text="no\n", timeout=180)
    if result.returncode != 0:
        return {"ok": False, "message": ((result.stdout or "") + (result.stderr or "")).strip() or "AVD 创建失败"}
    config_path = os.path.join(avd_path, "config.ini")
    _update_avd_config(config_path, {
        "hw.lcd.width": width, "hw.lcd.height": height, "hw.lcd.density": dpi,
        "hw.ramSize": memory, "hw.device.manufacturer": brand_label,
        "hw.device.name": model_label, "disk.dataPartition.size": "8G",
        "hw.keyboard": "yes", "showDeviceFrame": "no",
    })
    profile = {
        "name": name, "brand_label": brand_label, "model_label": model_label,
        "width": width, "height": height, "dpi": dpi, "memory": memory,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    profiles.append(profile)
    _write_json(PROFILE_PATH, profiles)
    return {"ok": True, "message": f"全新虚拟手机 {name} 已创建", "profile": profile}


def update_profile(data: dict) -> dict:
    name = _safe_name(data.get("name", ""))
    profiles = _profiles()
    profile = next((item for item in profiles if item.get("name") == name), None)
    if not profile:
        return {"ok": False, "message": "虚拟手机不存在"}
    if name in _running_avds():
        return {"ok": False, "message": "请先停止虚拟手机再修改配置"}
    try:
        brand_label, model_label, width, height, dpi, memory = _profile_values(data)
    except (TypeError, ValueError):
        return {"ok": False, "message": "分辨率、DPI 或内存配置不正确"}
    avd_path = os.path.abspath(os.path.join(AVD_HOME, name + ".avd"))
    if os.path.commonpath([AVD_HOME, avd_path]) != os.path.abspath(AVD_HOME):
        return {"ok": False, "message": "AVD 路径校验失败"}
    config_path = os.path.join(avd_path, "config.ini")
    if not os.path.isfile(config_path):
        return {"ok": False, "message": "虚拟手机配置文件不存在"}
    _update_avd_config(config_path, {
        "hw.lcd.width": width, "hw.lcd.height": height, "hw.lcd.density": dpi,
        "hw.ramSize": memory, "hw.device.manufacturer": brand_label,
        "hw.device.name": model_label,
    })
    profile.update({
        "brand_label": brand_label, "model_label": model_label,
        "width": width, "height": height, "dpi": dpi, "memory": memory,
    })
    _write_json(PROFILE_PATH, profiles)
    return {"ok": True, "message": f"虚拟手机 {name} 配置已更新，下次启动生效", "profile": profile}


def _start_profile(profile: dict, wipe: bool = False) -> dict:
    emulator = _exe("emulator", "emulator.exe")
    if not os.path.isfile(emulator):
        return {"ok": False, "message": "内置 Emulator 尚未安装"}
    command = [
        emulator, "-avd", profile["name"], "-memory", str(profile.get("memory") or 3072),
        "-gpu", "auto", "-no-boot-anim", "-no-snapshot-load",
    ]
    if wipe:
        command.append("-wipe-data")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        process = subprocess.Popen(command, cwd=RUNTIME_DIR, env=runtime_env(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=creationflags)
    except OSError as exc:
        return {"ok": False, "message": f"虚拟手机启动失败: {exc}"}
    with _lock:
        _processes[profile["name"]] = process
    return {"ok": True, "message": "虚拟手机正在启动，首次开机可能需要数分钟"}


def profile_action(name: str, action: str) -> dict:
    name = _safe_name(name)
    profiles = _profiles()
    profile = next((item for item in profiles if item.get("name") == name), None)
    if not profile:
        return {"ok": False, "message": "虚拟手机不存在"}
    running = _running_avds()
    serial = running.get(name)
    if action == "start":
        if serial:
            return {"ok": True, "message": "虚拟手机已经运行", "serial": serial}
        return _start_profile(profile)
    if action == "stop":
        if serial:
            subprocess.run([ADB_PATH, "-s", serial, "emu", "kill"], capture_output=True, timeout=10)
        return {"ok": True, "message": "已发送关机命令"}
    if action in {"restart", "wipe"}:
        if serial:
            subprocess.run([ADB_PATH, "-s", serial, "emu", "kill"], capture_output=True, timeout=10)
            for _ in range(30):
                if name not in _running_avds():
                    break
                time.sleep(0.5)
        return _start_profile(profile, wipe=action == "wipe")
    if action == "delete":
        if serial:
            return {"ok": False, "message": "请先停止虚拟手机再删除"}
        avd_path = os.path.abspath(os.path.join(AVD_HOME, name + ".avd"))
        ini_path = os.path.abspath(os.path.join(AVD_HOME, name + ".ini"))
        if os.path.commonpath([AVD_HOME, avd_path]) != os.path.abspath(AVD_HOME):
            return {"ok": False, "message": "AVD 路径校验失败"}
        if os.path.isdir(avd_path):
            shutil.rmtree(avd_path)
        if os.path.isfile(ini_path):
            os.remove(ini_path)
        _write_json(PROFILE_PATH, [item for item in profiles if item.get("name") != name])
        return {"ok": True, "message": f"虚拟手机 {name} 已删除"}
    return {"ok": False, "message": "不支持的虚拟手机操作"}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["install", "status"], nargs="?", default="status")
    args = parser.parse_args()
    configure(os.path.join(PROJECT_DIR, "scrcpy", "adb.exe"), PROJECT_DIR)
    if args.command == "install":
        _install_task.update({"status": "running", "progress": 0, "message": "", "lines": [], "error": ""})
        _install_worker(DEFAULT_SYSTEM_IMAGE)
    print(json.dumps(install_state(), ensure_ascii=False, indent=2))
