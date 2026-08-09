"""
TapTap 自动登录脚本
- 使用 XML 解析处理大部分 UI 操作（稳定、快速）
- 使用 AI 模型处理安全验证（动态验证码）
"""

import argparse
import base64
import io
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
from datetime import datetime

sys.stdout.reconfigure(errors='replace')
from io import BytesIO

import requests
from PIL import Image

from adb_locator import find_adb
from phone_agent.adb import *

# ============ 日志系统 ============
_LOG_FILE = None
_LOG_LOCK = threading.Lock()
_WORKFLOW_COMPLETED = False

# 点击后的 UI 验证采用短轮询；网络、短信和下载等长耗时操作另行设置超时。
CLICK_SETTLE_DELAY = 0.25
CLICK_RETRY_DELAY = 0.4
CLICK_VERIFY_TIMEOUT = 2.5
CLICK_POLL_INTERVAL = 0.25

def _init_log():
    global _LOG_FILE
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)))
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(log_dir, f"taptap_log_{timestamp}.log")
    with _LOG_LOCK:
        _LOG_FILE = open(log_path, "w", encoding="utf-8")
    return log_path


def _write_log_file(msg: str = ""):
    """只写本地日志，避免全量 UI 元素刷满控制台。"""
    with _LOG_LOCK:
        if _LOG_FILE:
            _LOG_FILE.write(msg + "\n")
            _LOG_FILE.flush()


def _log(msg: str = ""):
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()
    _write_log_file(msg)

def _log_action(action: str, elem=None, x: int = None, y: int = None):
    """记录操作：动作类型 + 元素标识 + 坐标"""
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    parts = [f"[{ts}] {action}"]
    if elem:
        rid = elem.resource_id or "-"
        txt = (elem.text or "").replace("\n", "\\n")[:40]
        cls = elem.class_name or "-"
        r = elem.rect
        parts.append(f"| id={rid} | text='{txt}' | class={cls} | rect={r} | center={(r[0]+r[2])//2},{(r[1]+r[3])//2}")
    if x is not None and y is not None:
        parts.append(f"| tap=({x},{y})")
    _log("".join(parts))

def _close_log():
    global _LOG_FILE
    with _LOG_LOCK:
        if _LOG_FILE:
            _LOG_FILE.close()
            _LOG_FILE = None

# ============ 配置 ============
TAPTAP_PACKAGE = "com.taptap"
SMS_API_URL = "http://a.62-us.com/api/get_sms?key=03b891d2d74603649eb43c0dff4fe43a"
PHONE_NUMBER = "3412640535"
PHONE_COUNTRY = "United States"
GAME_NAME = "我的休闲时光"
GAME_PACKAGE = "com.zhixing.wdxxsg"

DEFAULT_DEVICE_ID = "192.168.31.244:37145"

# 云码识别配置（用于安全验证）
JFBYM_API_URL = "http://api.jfbym.com/api/YmServer/customApi"
JFBYM_TOKEN = "E7LhAfiKssKDUGCudpvAhgSfOoSeYuSoc5_CsEM5ONI"
JFBYM_TYPE = "50009"


# ============ 命令行参数 ============
def parse_args():
    parser = argparse.ArgumentParser(description="TapTap 自动登录 + 下载脚本")
    parser.add_argument("--device", "-d", help="设备 ID（IP:PORT 或序列号），不指定则列出可用设备")
    parser.add_argument("--adb", help="adb 可执行文件或 platform-tools 目录；默认自动查找")
    parser.add_argument("--sms-api", help="短信验证码 API 地址", default=None)
    parser.add_argument("--phone", help="手机号", default=None)
    parser.add_argument("--game", help="要搜索下载的游戏名", default=None)
    parser.add_argument("--game-package", help="目标游戏包名", default=None)
    return parser.parse_args()


def resolve_device_id(args_device: str | None) -> str:
    """解析目标设备 ID。支持 --device 参数指定，否则列出设备供选择。"""
    if args_device:
        return args_device

    devices = list_devices()
    online = [d for d in devices if d.state == "device"]

    if len(online) == 1:
        dev = online[0]
        print(f"    [OK] 自动选择唯一在线设备: {dev.serial}")
        return dev.serial

    if len(online) > 1:
        print("    [INFO] 检测到多台在线设备：")
        for i, dev in enumerate(online):
            print(f"           [{i}] {dev.serial}")
        while True:
            try:
                idx = int(input("    请选择设备序号: ").strip())
                if 0 <= idx < len(online):
                    return online[idx].serial
            except (ValueError, IndexError):
                pass
            print("    输入无效，请重新输入")

    # 无在线设备，使用默认配置
    print(f"    [WARN] 无在线设备，使用默认配置: {DEFAULT_DEVICE_ID}")
    return DEFAULT_DEVICE_ID


ARGS = parse_args()

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_ADB_PATH = find_adb(ARGS.adb, base_dirs=[_SCRIPT_DIR])
if not _ADB_PATH:
    print(
        "[FAIL] 找不到 adb。请将 platform-tools 放入项目目录、加入 PATH，"
        "或通过 --adb/ADB_PATH 指定。"
    )
    sys.exit(1)

# 替换 phone_agent 里的 _get_adb_prefix，确保所有调用使用同一个动态路径。
import phone_agent.adb.device as _dev_mod
import phone_agent.adb.input as _inp_mod
import phone_agent.adb.screenshot as _scr_mod
import phone_agent.adb.uiautomator as _ui_mod

_orig_prefix = _dev_mod._get_adb_prefix


def _adb_prefix(device_id=None):
    prefix = _orig_prefix(device_id)
    prefix[0] = _ADB_PATH
    return prefix


_dev_mod._get_adb_prefix = _adb_prefix
_inp_mod._get_adb_prefix = _adb_prefix
_scr_mod._get_adb_prefix = _adb_prefix
_ui_mod._get_adb_prefix = _adb_prefix

print(f"    [OK] 使用 adb: {_ADB_PATH}")
DEVICE_ID = resolve_device_id(ARGS.device)

# 用 CLI 参数覆盖配置
if ARGS.sms_api:
    SMS_API_URL = ARGS.sms_api
if ARGS.phone:
    PHONE_NUMBER = ARGS.phone
if ARGS.game:
    GAME_NAME = ARGS.game
if ARGS.game_package:
    GAME_PACKAGE = ARGS.game_package


# ============ 辅助函数 ============

def adb_cmd(*args, device_id: str | None = None, timeout: float = 15) -> subprocess.CompletedProcess:
    """使用已探测到的 adb 执行设备命令。"""
    target = DEVICE_ID if device_id is None else device_id
    command = [_ADB_PATH]
    if target:
        command.extend(["-s", target])
    command.extend(map(str, args))
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        errors="replace",
    )


def _extract_ip(device_id: str) -> str:
    """从 device_id 中提取纯 IP（去掉端口部分）。"""
    return device_id.split(":")[0]


# 保存最初配置的端口，用于重连时尝试
_CONFIGURED_PORT = DEVICE_ID.split(":")[1] if ":" in DEVICE_ID else "5555"
_UI_DUMP_LOCK = threading.Lock()
_RECONNECT_LOCK = threading.Lock()
_LAST_FOREGROUND_UI_ACTIVITY = 0.0


def _is_device_online(device_id: str, attempts: int = 2) -> bool:
    """只检查设备状态，不触发 adb connect。"""
    for attempt in range(attempts):
        try:
            check = subprocess.run(
                [_ADB_PATH, "-s", device_id, "get-state"],
                capture_output=True,
                text=True,
                timeout=3,
                errors="replace",
            )
            if check.returncode == 0 and check.stdout.strip() == "device":
                return True
        except (OSError, subprocess.SubprocessError):
            pass
        if attempt + 1 < attempts:
            time.sleep(0.2)
    return False


def ensure_device_connected() -> bool:
    """检查设备是否在线，不在线则尝试重连（自动尝试多个端口）。"""
    global DEVICE_ID
    with _RECONNECT_LOCK:
        # UI dump 的瞬时失败不代表设备离线；在线时绝不重复 connect。
        if _is_device_online(DEVICE_ID):
            return True

        # USB 设备没有可供 adb connect 使用的网络端口。
        if ":" not in DEVICE_ID:
            return False

        ip = _extract_ip(DEVICE_ID)
        current_port = DEVICE_ID.split(":", 1)[1]
        ports_to_try = [current_port, _CONFIGURED_PORT, "5555"]
        ports = list(dict.fromkeys(ports_to_try))

        for port in ports:
            target = f"{ip}:{port}"
            for attempt in range(2):
                try:
                    subprocess.run(
                        [_ADB_PATH, "connect", target],
                        capture_output=True,
                        text=True,
                        timeout=5,
                        errors="replace",
                    )
                except (OSError, subprocess.SubprocessError) as exc:
                    print(f"    [WARN] adb 重连命令执行失败: {exc}")
                    return False

                time.sleep(0.4)
                if _is_device_online(target):
                    if DEVICE_ID != target:
                        DEVICE_ID = target
                        print(f"    [OK] 设备端口已更新: {target}")
                    else:
                        print(f"    [OK] 设备已重新连接: {target}")
                    return True

                if attempt == 0:
                    time.sleep(1)

        return False


def get_ui_elements_safe(device_id, *, background: bool = False) -> list:
    """串行获取 UI 层级；仅在设备确实离线时执行重连。"""
    global _LAST_FOREGROUND_UI_ACTIVITY

    if not background:
        _LAST_FOREGROUND_UI_ACTIVITY = time.monotonic()

    acquired = _UI_DUMP_LOCK.acquire(blocking=not background)
    if not acquired:
        return []

    last_error = None
    try:
        current_device = DEVICE_ID
        for attempt in range(5):
            try:
                return get_ui_elements(current_device)
            except RuntimeError as exc:
                last_error = exc
                message = str(exc).lower()
                recoverable = any(
                    marker in message
                    for marker in ("not found", "offline", "no devices")
                )
                if not recoverable:
                    raise

                if _is_device_online(current_device):
                    # 常见原因是 uiautomator dump 瞬时失败；不要对在线设备 connect。
                    if not background and attempt == 1:
                        print("    [WARN] UI 信息获取暂时失败，设备仍在线，正在重试...")
                else:
                    if not background:
                        print("    [WARN] 确认设备已离线，尝试重连...")
                    if not ensure_device_connected():
                        if attempt == 4:
                            raise
                    current_device = DEVICE_ID

                time.sleep(min(0.3 * (attempt + 1), 1.2))

        if last_error:
            raise last_error
        return []
    finally:
        _UI_DUMP_LOCK.release()


def log_global_ui_elements(step_label: str) -> list:
    """获取当前全局 UI 元素，并以 JSON Lines 形式保存到本地运行日志。"""
    captured_at = datetime.now().isoformat(timespec="milliseconds")
    begin_marker = f"[UI-ELEMENTS-BEGIN] step={step_label} captured_at={captured_at}"
    _write_log_file(begin_marker)
    try:
        elements = get_ui_elements_safe(DEVICE_ID)
    except Exception as exc:
        _write_log_file(
            f"[UI-ELEMENTS-ERROR] step={step_label} "
            f"error={type(exc).__name__}: {exc}"
        )
        _write_log_file(f"[UI-ELEMENTS-END] step={step_label} count=0")
        _log(f"    [WARN] 第 {step_label} 步全局元素获取失败，详情已写入日志")
        return []

    for index, elem in enumerate(elements):
        rect = getattr(elem, "rect", None)
        bounds = getattr(elem, "bounds", None)
        record = {
            "index": index,
            "resource_id": getattr(elem, "resource_id", None),
            "text": getattr(elem, "text", None),
            "content_description": getattr(
                elem,
                "content_description",
                getattr(elem, "content_desc", None),
            ),
            "class_name": getattr(elem, "class_name", None),
            "rect": list(rect) if isinstance(rect, (list, tuple)) else rect,
            "bounds": list(bounds) if isinstance(bounds, (list, tuple)) else bounds,
            "clickable": getattr(elem, "clickable", None),
            "enabled": getattr(elem, "enabled", None),
            "checkable": getattr(elem, "checkable", None),
            "checked": getattr(elem, "checked", None),
            "focusable": getattr(elem, "focusable", None),
            "focused": getattr(elem, "focused", None),
            "selected": getattr(elem, "selected", None),
            "scrollable": getattr(elem, "scrollable", None),
            "password": getattr(elem, "password", None),
        }
        _write_log_file(
            "[UI-ELEMENT] "
            + json.dumps(record, ensure_ascii=False, default=str, separators=(",", ":"))
        )

    _write_log_file(f"[UI-ELEMENTS-END] step={step_label} count={len(elements)}")
    _log(f"    [UI] 第 {step_label} 步全局元素已保存到日志，共 {len(elements)} 个")
    return elements


def clear_app_data(package: str) -> bool:
    """清除应用数据。"""
    print(f"[1] 清除 {package} 数据...")
    log_global_ui_elements("1 清除应用数据前")
    commands = [
        ("shell", "pm", "clear", "--user", "0", package),
        ("shell", "pm", "clear", package),
    ]
    last_message = "未知错误"
    for attempt, command in enumerate(commands, start=1):
        try:
            # 先停止应用，避免清数据时与前台进程或弹窗监控争用。
            adb_cmd("shell", "am", "force-stop", package, timeout=8)
            result = adb_cmd(*command, timeout=15)
        except (OSError, subprocess.SubprocessError) as exc:
            last_message = str(exc)
            result = None

        if result is not None:
            output = "\n".join(
                part.strip() for part in (result.stdout, result.stderr) if part.strip()
            )
            output_lines = {line.strip().lower() for line in output.splitlines()}
            if result.returncode == 0 and "success" in output_lines:
                print("    [OK] 数据已清除")
                return True
            last_message = output or f"adb 退出码 {result.returncode}"

        if attempt < len(commands):
            print(f"    [WARN] 清除失败，检查连接后重试: {last_message}")
            ensure_device_connected()

    print(f"    [FAIL] 清除失败: {last_message}")
    return False


def _package_is_running(package: str) -> bool:
    try:
        result = adb_cmd("shell", "pidof", package, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode == 0 and result.stdout.strip():
        return True
    try:
        result = adb_cmd("shell", "ps", "-A", timeout=6)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and any(
        line.split() and line.split()[-1] == package
        for line in result.stdout.splitlines()
    )


def _package_is_installed(package: str) -> bool:
    try:
        result = adb_cmd("shell", "pm", "path", package, timeout=6)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and "package:" in result.stdout


def _wait_package_running(package: str, timeout: float = 8) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _package_is_running(package):
            return True
        time.sleep(0.5)
    return False


def _package_is_foreground(package: str) -> bool:
    try:
        result = adb_cmd("shell", "dumpsys", "window", "windows", timeout=8)
    except (OSError, subprocess.SubprocessError):
        return False
    output = result.stdout or ""
    if result.returncode == 0 and any(
        package in line and any(
            marker in line for marker in ("mCurrentFocus", "mFocusedApp", "mObscuringWindow")
        )
        for line in output.splitlines()
    ):
        return True
    try:
        result = adb_cmd("shell", "dumpsys", "activity", "activities", timeout=8)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and any(
        package in line and any(marker in line for marker in ("mResumedActivity", "topResumedActivity"))
        for line in result.stdout.splitlines()
    )


def _wait_package_foreground(package: str, timeout: float = 12) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _package_is_running(package) and _package_is_foreground(package):
            return True
        time.sleep(0.8)
    return False


def launch_app_safe(app_name: str) -> bool:
    """启动应用并验证目标进程确实存在。"""
    print(f"[2] 启动 {app_name}...")
    log_global_ui_elements("2 启动应用前")
    for attempt in range(3):
        if launch_app(app_name, DEVICE_ID) and _wait_package_running(TAPTAP_PACKAGE):
            print(f"    [OK] 应用已启动并检测到进程: {TAPTAP_PACKAGE}")
            return True
        if attempt < 2:
            print("    [WARN] 未检测到应用进程，正在重试启动...")
        time.sleep(1)
    print("    [FAIL] 启动后未检测到应用进程")
    return False


def _elem_desc(elem) -> str:
    """生成元素的简短描述（resource-id / text / bounds）。"""
    parts = []
    if elem.resource_id:
        parts.append(elem.resource_id)
    if elem.text:
        parts.append(f"'{elem.text}'")
    if not parts:
        parts.append(elem.class_name)
    parts.append(f"[{elem.rect[0]},{elem.rect[1]}][{elem.rect[2]},{elem.rect[3]}]")
    return " | ".join(parts)

# ---- 保存原始引用（避免被批量替换覆盖） ----
_raw_tap_element = tap_element
_raw_tap = tap

# ---- 带日志的点击封装 ----
def _tap_elem(elem, label: str = ""):
    """点击元素并记录日志。"""
    _log_action(f"CLICK {label}" if label else "CLICK", elem=elem)
    _raw_tap_element(elem, DEVICE_ID)

def _tap_xy(x: int, y: int, label: str = ""):
    """点击坐标并记录日志。"""
    _log_action(f"TAP {label}" if label else "TAP", x=x, y=y)
    _raw_tap(x, y, DEVICE_ID)


def _find_element_by_text_candidates(elements, texts):
    """按候选文本查找元素，不强制 clickable（点击坐标同样有效）。"""
    for text in texts:
        for elem in elements:
            elem_text = (elem.text or "").strip()
            if elem_text == text or text in elem_text:
                return elem
    return None


def _find_element_by_id_candidates(elements, resource_ids):
    for resource_id in resource_ids:
        for elem in elements:
            if elem.resource_id == resource_id or (elem.resource_id or "").endswith(
                f":id/{resource_id}"
            ):
                return elem
    return None


def find_permission_allow_element(elements):
    """识别 Android/厂商权限弹窗中的允许按钮，通知权限优先。"""
    allow_texts = [
        "始终允许",
        "允许通知",
        "使用应用时允许",
        "使用期间允许",
        "仅在使用中允许",
        "允许",
    ]
    for text in allow_texts:
        for elem in elements:
            if (elem.text or "").strip() == text:
                return elem
    return _find_element_by_id_candidates(elements, [
        "permission_allow_always_button",
        "permission_allow_button",
        "permission_allow_foreground_only_button",
    ])


def _find_phone_input(elements):
    elem = _find_element_by_id_candidates(elements, [
        "phone_number_box",
        "phone_number",
        "phone_input",
        "et_phone",
    ])
    if elem:
        return elem
    for candidate in elements:
        if "EditText" in (candidate.class_name or ""):
            text = candidate.text or ""
            if "手机" in text or "phone" in text.lower() or not text:
                return candidate
    return None


def _phone_number_is_present(elements, phone_number: str) -> bool:
    expected = re.sub(r"\D", "", phone_number)
    for elem in elements:
        is_phone_field = bool(elem.resource_id) and any(
            marker in elem.resource_id.lower()
            for marker in ("phone", "mobile")
        )
        is_text_input = "EditText" in (elem.class_name or "")
        if is_phone_field or is_text_input:
            actual = re.sub(r"\D", "", elem.text or "")
            if actual == expected or actual.endswith(expected):
                return True
    return False


def input_phone_number(phone_number: str, elements=None) -> bool:
    """输入手机号并通过 UI 层级确认；失败时切换到原生 input 命令。"""
    if not elements:
        elements = get_ui_elements_safe(DEVICE_ID)
    input_elem = _find_phone_input(elements)
    if not input_elem:
        print("    [FAIL] 未找到手机号输入框")
        return False

    _log(f"      → 点击输入框: {_elem_desc(input_elem)}")
    _tap_elem(input_elem)
    time.sleep(CLICK_SETTLE_DELAY)

    # 首选项目原有的 ADB Keyboard 输入方式。
    original_ime = None
    try:
        original_ime = detect_and_set_adb_keyboard(DEVICE_ID)
        time.sleep(CLICK_SETTLE_DELAY)
        clear_text(DEVICE_ID)
        time.sleep(0.1)
        type_text(phone_number, DEVICE_ID)
        time.sleep(0.5)
    except Exception as exc:
        print(f"    [WARN] ADB Keyboard 输入失败: {exc}")
    finally:
        if original_ime is not None:
            try:
                restore_keyboard(original_ime, DEVICE_ID)
            except Exception:
                pass

    if _phone_number_is_present(get_ui_elements_safe(DEVICE_ID), phone_number):
        return True

    # 数字内容可安全使用 Android 原生 input text，不依赖特定输入法。
    print("    [WARN] 未检测到手机号，改用 Android 原生输入重试...")
    _tap_elem(input_elem)
    time.sleep(CLICK_SETTLE_DELAY)
    try:
        clear_text(DEVICE_ID)
    except Exception:
        pass
    result = adb_cmd("shell", "input", "text", phone_number, timeout=8)
    time.sleep(0.5)
    if result.returncode == 0 and _phone_number_is_present(
        get_ui_elements_safe(DEVICE_ID), phone_number
    ):
        return True

    # 最后逐个发送数字按键，规避部分 ROM 对 input text 的限制。
    print("    [WARN] 原生文本输入未生效，改用数字按键重试...")
    _tap_elem(input_elem)
    time.sleep(CLICK_SETTLE_DELAY)
    try:
        clear_text(DEVICE_ID)
    except Exception:
        pass
    for digit in phone_number:
        if digit.isdigit():
            adb_cmd("shell", "input", "keyevent", str(7 + int(digit)), timeout=4)
    time.sleep(0.5)
    return _phone_number_is_present(get_ui_elements_safe(DEVICE_ID), phone_number)


def find_agreement_radio_element(elements):
    """查找登录页“勾选即代表同意…”对应的 RadioButton。"""
    radio_buttons = [
        elem
        for elem in elements
        if "RadioButton" in (elem.class_name or "")
    ]
    for elem in radio_buttons:
        resource_id = (elem.resource_id or "").lower()
        if any(marker in resource_id for marker in (
            "agreement", "protocol", "privacy", "agree", "radio",
        )):
            return elem
    if radio_buttons:
        # 当前登录页只有一个 RadioButton；资源 id 改名时仍可识别。
        return radio_buttons[0]
    return None


def find_agreement_control_element(elements):
    """查找协议点击目标，优先使用 checkbox 图标而非协议链接文字。"""
    radio = find_agreement_radio_element(elements)
    if radio is not None:
        return radio

    control = _find_element_by_id_candidates(elements, [
        "checkbox",
        "click_space",
        "protocol",
        "protocolV2",
        "protocol_back",
    ])
    if control is not None:
        return control

    return next(
        (
            elem
            for elem in elements
            if "勾选即代表同意" in (elem.text or "")
            and "服务协议" in (elem.text or "")
            and "隐私政策" in (elem.text or "")
        ),
        None,
    )


def tap_agreement_control(elem):
    """点击协议控件；优先精确点击 checkbox，自定义布局则点击左侧圆点。"""
    resource_id = elem.resource_id or ""
    if "RadioButton" in (elem.class_name or ""):
        _tap_elem(elem, "协议 RadioButton")
        return
    if resource_id.endswith(":id/checkbox"):
        _tap_elem(elem, "协议 checkbox 图标")
        return

    x1, y1, _, y2 = elem.rect
    if resource_id.endswith(":id/click_space") \
            or resource_id.endswith(":id/protocolV2") \
            or resource_id.endswith(":id/protocol_back"):
        indicator_x = x1 + 42
    else:
        indicator_x = max(1, x1 - 40)
    indicator_y = (y1 + y2) // 2
    _tap_xy(indicator_x, indicator_y, "协议 RadioButton 左侧圆点")


def agreement_radio_is_checked(elements) -> bool:
    radio = find_agreement_radio_element(elements)
    return radio is not None and bool(getattr(radio, "checked", False))


def wait_for_ui_condition(
    predicate,
    timeout: float = 5,
    interval: float = CLICK_POLL_INTERVAL,
):
    """轮询 UI，返回首个满足后置条件的元素列表，超时返回 None。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            elements = get_ui_elements_safe(DEVICE_ID)
        except RuntimeError:
            time.sleep(interval)
            continue
        if predicate(elements):
            return elements
        time.sleep(interval)
    return None


def _same_ui_element(candidate, target) -> bool:
    target_id = target.resource_id or ""
    if target_id:
        return candidate.resource_id == target_id
    return (
        (candidate.text or "") == (target.text or "")
        and (candidate.class_name or "") == (target.class_name or "")
        and candidate.rect == target.rect
    )


def tap_and_verify_disappeared(
    elem,
    label: str,
    *,
    timeout: float = CLICK_VERIFY_TIMEOUT,
    retries: int = 2,
) -> bool:
    """点击元素，并确认原元素从 UI 层级消失。"""
    for attempt in range(retries):
        _tap_elem(elem, label)
        after = wait_for_ui_condition(
            lambda elements: not any(_same_ui_element(item, elem) for item in elements),
            timeout=timeout,
        )
        if after is not None:
            return True
        if attempt + 1 < retries:
            print(f"    [WARN] {label} 点击后页面未变化，正在重试...")
    return False


def _input_text_is_present(elements, expected: str) -> bool:
    expected_normalized = "".join(expected.split())
    input_values = []
    for elem in elements:
        if "EditText" not in (elem.class_name or ""):
            continue
        actual = "".join((elem.text or "").split())
        if actual:
            input_values.append((elem.rect[0], elem.rect[1], actual))
        if actual == expected_normalized or actual.endswith(expected_normalized):
            return True
    # 部分验证码页面将每一位拆成独立 EditText。
    combined = "".join(
        value for _, _, value in sorted(input_values, key=lambda item: (item[1], item[0]))
    )
    if combined == expected_normalized:
        return True
    return False


def input_text_verified(input_elem, value: str, *, clear: bool = True) -> bool:
    """输入普通文本并从 UI 读回验证，不成功则返回 False。"""
    _tap_elem(input_elem)
    time.sleep(CLICK_SETTLE_DELAY)
    original_ime = None
    try:
        original_ime = detect_and_set_adb_keyboard(DEVICE_ID)
        time.sleep(CLICK_SETTLE_DELAY)
        if clear:
            clear_text(DEVICE_ID)
            time.sleep(0.1)
        type_text(value, DEVICE_ID)
        time.sleep(0.5)
    except Exception as exc:
        print(f"    [WARN] 文本输入命令失败: {exc}")
    finally:
        if original_ime is not None:
            try:
                restore_keyboard(original_ime, DEVICE_ID)
            except Exception:
                pass

    if _input_text_is_present(get_ui_elements_safe(DEVICE_ID), value):
        return True

    # 纯 ASCII 文本可使用系统 input text 作为回退。
    if value.isascii() and " " not in value:
        _tap_elem(input_elem)
        if clear:
            try:
                clear_text(DEVICE_ID)
            except Exception:
                pass
        result = adb_cmd("shell", "input", "text", value, timeout=8)
        time.sleep(0.5)
        if result.returncode == 0 and _input_text_is_present(
            get_ui_elements_safe(DEVICE_ID), value
        ):
            return True
    return False


def is_home_page(elements) -> bool:
    home_ids = (
        "com.taptap:id/tb_layout_home_bottom_bar",
        "com.taptap:id/viewSearchContent",
        "com.taptap:id/tsi_search_banner_key_text",
    )
    return any(find_element_by_id(resource_id, elements) for resource_id in home_ids)


def is_login_page(elements) -> bool:
    has_phone = _find_phone_input(elements) is not None
    has_login = any(
        (elem.text or "").strip() == "登录"
        or (elem.resource_id or "").endswith(":id/login_register_btn")
        for elem in elements
    )
    return has_phone and has_login


def is_country_list_page(elements) -> bool:
    area_rows = [
        elem for elem in elements
        if "area_name" in (elem.resource_id or "")
    ]
    return len(area_rows) >= 2 or any(
        (elem.text or "").strip() in ("国家/地区", "选择国家或地区")
        for elem in elements
    )


def is_us_country_selected(elements) -> bool:
    if not is_login_page(elements):
        return False
    for elem in elements:
        resource_id = elem.resource_id or ""
        text = (elem.text or "").replace(" ", "").upper()
        if "tv_area_code" in resource_id and (
            "+1" in text or text.startswith("US") or "美国" in text
        ):
            return True
    return False


def is_search_page(elements) -> bool:
    search_ids = (
        "com.taptap:id/input_box",
        "com.taptap:id/tvSure",
    )
    return any(find_element_by_id(resource_id, elements) for resource_id in search_ids) \
        or any("EditText" in (elem.class_name or "") for elem in elements)


def is_game_detail_page(elements, game_name: str | None = None) -> bool:
    action_texts = ("下载", "安装", "启动", "打开")
    screen_bottom = max((elem.rect[3] for elem in elements), default=0)
    has_bottom_action = any(
        elem.text
        and elem.text.strip() in action_texts
        and elem.rect[3] >= screen_bottom * 0.7
        for elem in elements
    )
    has_game_title = not game_name or any(
        game_name in (elem.text or "")
        and "EditText" not in (elem.class_name or "")
        for elem in elements
    )
    return has_bottom_action and has_game_title


def search_results_visible(elements, game_name: str) -> bool:
    for elem in elements:
        if "EditText" in (elem.class_name or ""):
            continue
        if game_name and game_name in (elem.text or ""):
            return True
        resource_id = (elem.resource_id or "").lower()
        if any(marker in resource_id for marker in ("brand_app", "search_result", "game_title")):
            return True
    return False


def is_username_or_home_page(elements) -> bool:
    if is_home_page(elements):
        return True
    if find_element_by_id("com.taptap:id/tv_user_name", elements):
        return True
    has_name_hint = any(
        elem.text and any(marker in elem.text for marker in ("用户名", "昵称", "取个名字"))
        for elem in elements
    )
    has_input = any("EditText" in (elem.class_name or "") for elem in elements)
    return has_name_hint and has_input


def is_home_or_profile_page(elements) -> bool:
    return is_home_page(elements) or bool(
        find_element_by_id("com.taptap:id/tv_user_name", elements)
    )


def download_has_started(elements) -> bool:
    state_markers = ("下载中", "暂停", "继续", "安装", "等待", "排队", "%")
    return any(
        elem.text and any(marker in elem.text for marker in state_markers)
        for elem in elements
    )


def find_and_tap_safe(text: str = None, desc: str = None, res_id: str = None,
                      class_name: str = None, clickable: bool = True,
                      retries: int = 3, delay: float = CLICK_SETTLE_DELAY) -> bool:
    """带重试的查找并点击（捕获 uiautomator 暂时失败）。"""
    for i in range(retries):
        try:
            elem = find_with_multiple_conditions(
                text=text, desc=desc, res_id=res_id,
                class_name=class_name, clickable=clickable,
                device_id=DEVICE_ID
            )
        except RuntimeError as e:
            if i < retries - 1:
                time.sleep(CLICK_RETRY_DELAY)
                continue
            return False
        if elem:
            _log(f"      → 点击元素: {_elem_desc(elem)}")
            _tap_elem(elem)
            time.sleep(delay)
            return True
        if i < retries - 1:
            time.sleep(CLICK_RETRY_DELAY)
    return False


def wait_and_tap(text: str = None, desc: str = None, timeout: float = 4.0) -> bool:
    """等待元素出现并点击。"""
    if text:
        elem = wait_for_element(
            find_element_by_text,
            timeout=timeout, check_interval=CLICK_POLL_INTERVAL,
            text=text, device_id=DEVICE_ID
        )
    elif desc:
        elem = wait_for_element(
            find_element_by_desc,
            timeout=timeout, check_interval=CLICK_POLL_INTERVAL,
            desc=desc, device_id=DEVICE_ID
        )
    else:
        return False
    if elem:
        print(f"      → 点击元素: {_elem_desc(elem)}")
        _tap_elem(elem)
        time.sleep(CLICK_SETTLE_DELAY)
        return True
    return False


def scroll_down_in_list(steps: int = 3) -> None:
    """在列表中向下滑动（用于选择国家/地区）。"""
    adb_cmd("shell", "input", "swipe", "500", "1500", "500", "500", "300")
    time.sleep(CLICK_RETRY_DELAY)


def _swipe_slider(x1: int, y1: int, x2: int, y2: int) -> None:
    """用 motionevent 序列模拟真实手指拖动滑块（WebView 验证码必须用这种方式）。"""
    steps = 40
    dx = (x2 - x1) // steps
    # DOWN
    adb_cmd("shell", "input", "motionevent", "DOWN", str(x1), str(y1))
    # MOVE 逐步拖动（先慢后快模拟真人）
    for i in range(1, steps + 1):
        tx = x1 + dx * i
        adb_cmd("shell", "input", "motionevent", "MOVE", str(tx), str(y2))
        time.sleep(0.015)
    # 在终点停留一下
    time.sleep(0.3)
    # UP
    adb_cmd("shell", "input", "motionevent", "UP", str(x2), str(y2))


# ============ 安全验证（云码识别） ============

def find_captcha_question(elements) -> str:
    """
    从 UI 元素中查找验证码问题文本（如"点击【E】"）。
    对应的 XPath: *[resourceId='tcaptcha_iframe'] > ... > *[class='android.widget.TextView']
    """
    # 先找明确包含点击指令且含字母的文本
    for elem in elements:
        if not elem.text:
            continue
        t = elem.text.strip()
        if any(kw in t for kw in ["点击", "选择", "确认", "请"]) and re.search(r'[A-Za-z\u4e00-\u9fff]', t):
            return t

    # 退一步：找任何含大写字母的短文本
    for elem in elements:
        if elem.text and re.search(r'[A-Z]', elem.text) and len(elem.text) < 30:
            return elem.text

    # 再退一步：找 resource-id 含 vtt 或 ctcontainer 的相邻 TextView
    for elem in elements:
        if elem.resource_id and ("vtt" in elem.resource_id or "ct" in elem.resource_id):
            return elem.text or ""

    return ""


def find_captcha_image(elements, device_id: str):
    """
    查找验证码图片区域 tcaptcha-img。
    返回捕获到的 PIL Image，或 None。
    """
    captcha_elem = find_element_by_id("tcaptcha-img", elements)
    if captcha_elem:
        return captcha_elem

    # 备选：找 resource-id 含 imgarea 或 captcha-img 的元素
    for elem in elements:
        if elem.resource_id and ("imgarea" in elem.resource_id or "captcha-img" in elem.resource_id or "img" in elem.resource_id.lower()):
            return elem

    return None


def crop_captcha_image(full_screenshot, captcha_elem) -> str:
    """
    从全屏截图中裁剪验证码区域，返回 base64 字符串。
    """
    x1, y1, x2, y2 = captcha_elem.rect
    if x2 - x1 <= 0 or y2 - y1 <= 0:
        return ""

    cropped = full_screenshot.crop((x1, y1, x2, y2))
    buffered = io.BytesIO()
    cropped.save(buffered, format="PNG")
    return base64.b64encode(buffered.getvalue()).decode()


def _take_captcha_snapshot(captcha_elem):
    """截图 + 裁剪验证码区域，返回 (img_b64, captcha_elem) 或出错返回 (None, None)。"""
    try:
        elems = get_ui_elements_safe(DEVICE_ID)
        new_captcha = find_captcha_image(elems, DEVICE_ID) or captcha_elem
        ss = get_screenshot(DEVICE_ID)
        if ss.is_sensitive:
            return None, None
        full = Image.open(io.BytesIO(base64.b64decode(ss.base64_data)))
        b64 = crop_captcha_image(full, new_captcha)
        return b64, new_captcha
    except Exception:
        return None, None


def verify_captcha_with_jfbym() -> bool:
    """
    使用云码（jfbym.com）识别安全验证码。
    流程：获取问题文本 → 截图裁剪验证码区域 → 提交 API → 解析坐标 → 点击
    每次重试都重新截图 + 刷新验证码，避免同一张图片反复失败。
    """
    print("\n[安全验证] 使用云码识别...")

    elements = get_ui_elements_safe(DEVICE_ID)

    # 1. 查找问题文本
    question_text = find_captcha_question(elements)
    if question_text:
        print(f"    问题: {question_text}")
    else:
        print("    [WARN] 未找到问题文本，使用空字符串")

    # 2. 查找验证码图片元素
    captcha_elem = find_captcha_image(elements, DEVICE_ID)
    if not captcha_elem:
        print("    [FAIL] 未找到验证码图片 (tcaptcha-img)")
        return False
    print(f"    验证码图片: {captcha_elem.bounds}")

    img_x1, img_y1, img_x2, img_y2 = captcha_elem.rect

    # 3. 提交 jfbym API（最多 5 次重试，每次重新截图）
    for attempt in range(5):
        print(f"    云码识别尝试 {attempt + 1}/5...")

        # 从第2次开始，先刷新验证码再截图
        if attempt > 0:
            print("    刷新验证码图片...")
            elems_now = get_ui_elements_safe(DEVICE_ID)
            reload_btn = find_element_by_id("reload", elems_now)
            if reload_btn:
                _tap_elem(reload_btn)
                time.sleep(0.5)
            else:
                # 备用：点击验证码图片区域触发刷新
                tap((img_x1 + img_x2) // 2, (img_y1 + img_y2) // 2, DEVICE_ID)
                time.sleep(0.5)
            # 重新获取 captcha_elem
            fresh_elems = get_ui_elements_safe(DEVICE_ID)
            fresh_captcha = find_captcha_image(fresh_elems, DEVICE_ID)
            if fresh_captcha:
                captcha_elem = fresh_captcha
                img_x1, img_y1, img_x2, img_y2 = captcha_elem.rect

        # 截图 + 裁剪
        img_b64, captcha_elem = _take_captcha_snapshot(captcha_elem)
        if not img_b64:
            print("    [WARN] 截图/裁剪失败，重试...")
            time.sleep(1)
            continue

        try:
            resp = requests.post(
                JFBYM_API_URL,
                json={
                    "token": JFBYM_TOKEN,
                    "type": JFBYM_TYPE,
                    "extra": question_text,
                    "image": img_b64,
                },
                headers={"Content-Type": "application/json"},
                timeout=15,
            )
            raw_text = resp.text[:500]
            try:
                result = resp.json()
            except Exception as json_err:
                print(f"    [WARN] JSON 解析失败: {json_err}")
                print(f"    [DEBUG] 原始响应({resp.status_code}): {raw_text}")
                time.sleep(1)
                continue
            msg = result.get("msg", "")
            code = result.get("code", -1)
            data = result.get("data", {})
            print(f"    API: {msg}")

            if code == 10000:
                coords_str = data.get("data", "")
                if not coords_str or "," not in coords_str:
                    print(f"    [WARN] 未解析到坐标: {coords_str}")
                    continue

                parts = coords_str.split(",")
                rel_x, rel_y = int(parts[0]), int(parts[1])

                # 计算屏幕实际坐标
                screen_x = img_x1 + rel_x
                screen_y = img_y1 + rel_y
                print(f"    点击 ({screen_x}, {screen_y}) <- 相对({rel_x},{rel_y}) + 图片({img_x1},{img_y1})")

                _tap_xy(screen_x, screen_y)
                time.sleep(0.75)

                # 检查验证码是否消失
                elems_after = get_ui_elements_safe(DEVICE_ID)
                still_exists = find_element_by_id("tcaptcha-img", elems_after)
                if not still_exists:
                    print("    [OK] 验证通过")
                    return True
                else:
                    print("    [WARN] 未通过，下一轮重试...")
                    continue
            else:
                print(f"    [WARN] 识别失败: {msg}")
                # 识别失败也要刷新图片，继续重试
                continue
        except Exception as e:
            print(f"    [WARN] API 请求异常: {e}")
            time.sleep(1)

    print("    [FAIL] 验证码识别失败次数过多")
    return False


# ============ 获取验证码 ============

def fetch_sms_code() -> str:
    """从 API 获取短信验证码。"""
    print(f"\n[获取验证码] 请求 SMS API...")
    try:
        resp = requests.get(SMS_API_URL, timeout=10)
        data = resp.text.strip()
        print(f"    API 返回: {data}")

        # 格式1: yes|[TapTap]338016 is your...|(TapTap)|...
        if data.startswith("yes|"):
            parts = data.split("|")
            if len(parts) >= 2:
                msg = parts[1]
                m = re.search(r'TapTap\D*(\d{6})', msg)
                if not m:
                    m = re.search(r'(?<!\d)(\d{6})(?!\d|\d{2}-\d{2})', msg)
                if m:
                    code = m.group(1)
                    print(f"    [OK] 提取到验证码: {code}")
                    return code

        # TapTap 验证码固定 6 位数字，排除日期如 2026-07-29
        m = re.search(r'TapTap\D*(\d{6})', data)
        if not m:
            m = re.search(r'(?<!\d)(\d{6})(?!\d|\d{2}-\d{2})', data)
        if m:
            code = m.group(1)
            print(f"    [OK] 提取到验证码: {code}")
            return code

        print(f"    [WARN] 未找到验证码，原始数据: {data}")
    except Exception as e:
        print(f"    [FAIL] API 请求失败: {e}")
    return ""


def wait_for_sms_code(max_wait: int = 60, interval: int = 5) -> str:
    """等待并获取短信验证码（轮询）。"""
    print(f"    将在 {max_wait} 秒内轮询获取验证码...")
    waited = 0
    while waited < max_wait:
        code = fetch_sms_code()
        if code:
            return code
        time.sleep(interval)
        waited += interval
        print(f"    已等待 {waited} 秒...")
    return ""


# ============ 后台弹窗监控 ============

class PopupMonitor:
    """后台弹窗监控线程 - 在脚本执行全过程中自动检测并关闭弹窗。

    主线程处理流程步骤的同时，后台线程每隔 interval 秒检测一次常见的弹窗
    （btn_dismiss、系统权限等），发现即点击关闭，避免弹窗阻塞流程。

    注意：
    - 「安装弹窗」（TapTap正尝试安装应用）由主线程步骤 [18] 统一处理，
      后台线程检测到时跳过，避免误点「取消」导致安装中断。
    - 「更新弹窗」只会弹出一次，处理过后不再重复检测。
    """

    def __init__(self, device_id: str, interval: float = 2.0):
        self.device_id = device_id
        self.interval = interval
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._update_handled = False

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="PopupMonitor")
        self._thread.start()
        print(f"    [OK] 弹窗监控已启动（间隔 {self.interval}s）")

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3)
        print("    [OK] 弹窗监控已停止")

    def _run(self):
        # 首次检查也等待一个周期，让主流程先完成启动页处理。
        while not self._stop_event.wait(self.interval):
            try:
                self._check_popups()
            except RuntimeError:
                pass
            except Exception:
                pass

    def _check_popups(self):
        # 主流程刚执行过 UI 操作时跳过本轮，避免与主流程争抢 uiautomator dump。
        if time.monotonic() - _LAST_FOREGROUND_UI_ACTIVITY < self.interval:
            return

        # 重连可能更新全局 DEVICE_ID（例如无线调试端口变化）。
        device_id = DEVICE_ID
        self.device_id = device_id
        try:
            # 后台线程不等待 UI 锁；主流程繁忙时直接留到下一轮。
            elements = get_ui_elements_safe(device_id, background=True)
        except RuntimeError:
            return
        if not elements:
            return

        # ---- 安装弹窗保护检测 ----
        # 如果页面有「继续」(button3) 说明是 TapTap 安装弹窗，
        # 后台线程不处理，由主线程步骤[18]统一处理
        install_popup_active = find_with_multiple_conditions(
            res_id="android:id/button3", text="继续",
            clickable=True, elements=elements, device_id=device_id
        ) is not None

        # 1) btn_dismiss 通用关闭按钮
        dismiss = find_with_multiple_conditions(
            res_id="com.taptap:id/btn_dismiss", clickable=True,
            elements=elements, device_id=device_id
        )
        if dismiss:
            print(f"    [弹窗监控] → 关闭 btn_dismiss: {_elem_desc(dismiss)}")
            _tap_elem(dismiss)
            return

        # 2) 系统权限弹窗（通知、定位等）
        sys_perm = find_permission_allow_element(elements)
        if sys_perm:
            print(f"    [弹窗监控] → 系统权限「{sys_perm.text or '允许'}」")
            _tap_elem(sys_perm)
            return

        # 3) 更新弹窗（取消/以后再说/稍后/忽略/暂不更新）
        #    "取消" 需在屏幕下半部分（y>1000），排除顶部导航栏的返回按钮
        if not self._update_handled and not install_popup_active:
            for btn_text in ["取消", "以后再说", "稍后", "忽略", "暂不更新"]:
                btn = find_with_multiple_conditions(
                    text=btn_text, clickable=True, exact_match=True,
                    elements=elements, device_id=device_id
                )
                if btn:
                    cy = (btn.rect[1] + btn.rect[3]) // 2
                    if btn_text == "取消" and cy < 800:
                        continue  # 顶部导航栏按钮，跳过
                    print(f"    [弹窗监控] → 关闭更新弹窗「{btn_text}」")
                    _tap_elem(btn)
                    self._update_handled = True
                    return


# ============ 主流程 ============

def main():
    global _WORKFLOW_COMPLETED
    _WORKFLOW_COMPLETED = False
    log_path = _init_log()
    _log("=" * 60)
    _log("TapTap 自动登录脚本")
    _log(f"日志文件: {log_path}")
    _log("=" * 60)

    # ---- 第 1 步：清除数据 ----
    if not clear_app_data(TAPTAP_PACKAGE):
        print("    [FAIL] 第 1 步未通过验证，任务停止")
        return

    # ---- 第 2 步：启动 TapTap ----
    if not launch_app_safe("TapTap"):
        print("    [FAIL] 第 2 步未通过验证，任务停止")
        return

    # 应用启动后再监控弹窗，避免 UI dump 与清数据命令并发执行。
    monitor = PopupMonitor(DEVICE_ID, interval=2.0)
    monitor.start()


    # ---- 第 3 步：隐私政策和权限弹窗 ----
    _log("\n[3] 处理初始化弹窗（隐私政策 + 权限）...")
    init_elements = log_global_ui_elements("3 处理初始化弹窗前")

    handled = {"privacy": False}
    idle_rounds = 0

    # “同意”后继续检测通知权限弹窗；连续两轮无弹窗即结束。
    for init_attempt in range(20):
        # 每轮只 dump 一次，所有查找共享
        try:
            elements = (
                init_elements
                if init_attempt == 0 and init_elements
                else get_ui_elements_safe(DEVICE_ID)
            )
        except RuntimeError:
            time.sleep(CLICK_RETRY_DELAY)
            continue
        clicked = False

        # 1) 隐私政策/用户协议
        if not handled["privacy"]:
            for btn_text in ["同意", "我已阅读并同意", "登录", "下一步"]:
                elem = find_with_multiple_conditions(
                    text=btn_text, clickable=True, exact_match=True,
                    elements=elements, device_id=DEVICE_ID
                )
                if elem:
                    print(f"      → {_elem_desc(elem)}")
                    if tap_and_verify_disappeared(elem, f"初始化「{btn_text}」"):
                        print(f"    [OK] 「{btn_text}」已点击且原弹窗已消失")
                        handled["privacy"] = True
                        clicked = True
                    else:
                        print(f"    [WARN] 「{btn_text}」点击后弹窗仍存在")
                    break

        # 2) Android/厂商权限弹窗，包括“是否允许 TapTap 发送通知”。
        if not clicked:
            permission_allow = find_permission_allow_element(elements)
            if permission_allow:
                print(f"      → {_elem_desc(permission_allow)}")
                permission_text = permission_allow.text or "允许"
                if tap_and_verify_disappeared(
                    permission_allow, f"权限「{permission_text}」"
                ):
                    print(f"    [OK] 权限「{permission_text}」已生效，弹窗已消失")
                    clicked = True
                else:
                    print(f"    [WARN] 权限「{permission_text}」点击后弹窗仍存在")

        # 3) 更新弹窗（每轮都检测，可能随时出现）。
        if not clicked:
            for btn_text in ["取消", "以后再说", "稍后", "忽略", "暂不更新"]:
                elem = find_with_multiple_conditions(
                    text=btn_text, clickable=True, exact_match=True,
                    elements=elements, device_id=DEVICE_ID
                )
                if elem:
                    print(f"      → {_elem_desc(elem)}")
                    if tap_and_verify_disappeared(elem, f"更新弹窗「{btn_text}」"):
                        print(f"    [OK] 更新弹窗「{btn_text}」已关闭")
                        clicked = True
                    else:
                        print(f"    [WARN] 更新弹窗「{btn_text}」仍存在")
                    break

        if not clicked:
            btn_dismiss = find_with_multiple_conditions(
                res_id="com.taptap:id/btn_dismiss", clickable=True,
                elements=elements, device_id=DEVICE_ID
            )
            if btn_dismiss:
                print(f"      → {_elem_desc(btn_dismiss)}")
                if tap_and_verify_disappeared(btn_dismiss, "初始化关闭按钮"):
                    clicked = True

        if clicked:
            idle_rounds = 0
            time.sleep(CLICK_RETRY_DELAY)
            continue

        idle_rounds += 1
        if idle_rounds >= 2:
            print(f"    - 弹窗处理完毕（连续 {idle_rounds} 轮无更多弹窗）")
            break
        time.sleep(CLICK_SETTLE_DELAY)
    else:
        print("    [FAIL] 达到最大处理轮数，初始化弹窗仍未确认处理完毕")
        return

    final_init_elements = get_ui_elements_safe(DEVICE_ID)
    if not handled["privacy"] or find_permission_allow_element(final_init_elements):
        print("    [FAIL] 第 3 步未通过验证：隐私确认或权限弹窗仍未完成")
        return
    print("    [OK] 第 3 步验证通过：初始化弹窗均已处理")

    time.sleep(CLICK_SETTLE_DELAY)

    # ---- 第 5 步：返回主页，点击头像进入登录 ----
    _log("\n[5] 导航到登录页...")
    home_elems = (
        log_global_ui_elements("5 导航到登录页前")
        or get_ui_elements_safe(DEVICE_ID)
    )

    # 检查是否在主页：查找底部导航栏 tb_layout_home_bottom_bar
    home_tab = find_element_by_id("com.taptap:id/tb_layout_home_bottom_bar", home_elems)
    if not home_tab:
        print("    [WARN] 不在主页，按返回键回到主页...")
        back(DEVICE_ID)
        if wait_for_ui_condition(is_home_page, timeout=3) is None:
            print("    [WARN] 返回后仍未确认主页，继续尝试定位头像")
    else:
        print("    [OK] 已在主页")

    # 循环：弹窗检测 + 头像查找合并在一起，失败自动重试
    avatar_found = False
    for attempt in range(6):
        try:
            elems = get_ui_elements_safe(DEVICE_ID)
        except RuntimeError:
            time.sleep(CLICK_RETRY_DELAY)
            continue

        # 1) 检查是否有弹窗挡住（更新弹窗、评分弹窗等）
        popup_dismissed = False

        dismiss = find_with_multiple_conditions(
            res_id="com.taptap:id/btn_dismiss", clickable=True,
            elements=elems, device_id=DEVICE_ID
        )
        if dismiss:
            print(f"    发现弹窗: {_elem_desc(dismiss)}")
            _tap_elem(dismiss)
            time.sleep(CLICK_SETTLE_DELAY)
            continue

        cancel = find_with_multiple_conditions(
            text="取消", clickable=True, exact_match=True,
            elements=elems, device_id=DEVICE_ID
        )
        if cancel:
            print(f"    发现弹窗: {_elem_desc(cancel)}")
            _tap_elem(cancel)
            time.sleep(CLICK_SETTLE_DELAY)
            continue

        for t in ["以后再说", "忽略", "稍后"]:
            c = find_with_multiple_conditions(
                text=t, clickable=True, exact_match=True,
                elements=elems, device_id=DEVICE_ID
            )
            if c:
                print(f"    发现弹窗: {_elem_desc(c)}")
                _tap_elem(c)
                time.sleep(CLICK_SETTLE_DELAY)
                popup_dismissed = True
                break
        if popup_dismissed:
            continue

        # 2) 没弹窗，找头像
        avatar_elem = find_element_by_id("com.taptap:id/viewHeader", elems)
        if not avatar_elem:
            avatar_elem = find_element_by_desc("头像", elems, exact_match=False)
        if not avatar_elem:
            screen_width = 1080
            right_x = int(screen_width * 0.9)
            right_top = find_elements_by_bounds(right_x - 100, 0, right_x + 100, 200, elems)
            for e in right_top:
                if e.clickable and e.class_name and 'Image' in e.class_name:
                    avatar_elem = e
                    break

        if avatar_elem:
            print(f"    找到头像: {_elem_desc(avatar_elem)}")
            _tap_elem(avatar_elem)
            if wait_for_ui_condition(is_login_page, timeout=3) is not None:
                avatar_found = True
                print("    [OK] 头像点击已生效，登录页已出现")
                break
            print("    [WARN] 点击头像后未进入登录页，继续重试...")

        # 3) 没弹窗也没头像 → 可能不在主页，按返回重试
        if attempt < 4:
            print(f"    [WARN] 第{attempt+1}次未找到头像，按返回重试...")
            back(DEVICE_ID)
            time.sleep(CLICK_RETRY_DELAY)
        else:
            print(f"    [WARN] 第{attempt+1}次未找到头像，尝试在右上角区域点击...")
            _tap_xy(1000, 100)
            time.sleep(CLICK_RETRY_DELAY)

    if not avatar_found:
        _log("    [FAIL] 第 5 步未通过验证：未进入登录页")
        return

    # ---- 第 6 步：切换国家/地区 ----
    _log("\n[6] 切换国家到美国...")
    country_elements = log_global_ui_elements("6 切换国家前")

    # 当前界面显示 "CN+86"，点击后必须确认国家列表已出现。
    time.sleep(CLICK_SETTLE_DELAY)
    country_list_open = False
    for open_attempt in range(4):
        elements = (
            country_elements
            if open_attempt == 0 and country_elements
            else get_ui_elements_safe(DEVICE_ID)
        )
        country_button = _find_element_by_id_candidates(elements, ["tv_area_code"])
        if not country_button:
            country_button = next(
                (
                    elem for elem in elements
                    if any(marker in (elem.text or "") for marker in ("CN+86", "+86", "中国"))
                ),
                None,
            )
        if country_button:
            _tap_elem(country_button)
        else:
            phone_input = _find_phone_input(elements)
            if not phone_input:
                break
            x1, y1, _, y2 = phone_input.rect
            _tap_xy(max(0, x1 - 100), (y1 + y2) // 2, "国家切换区域")

        if wait_for_ui_condition(is_country_list_page, timeout=3) is not None:
            country_list_open = True
            print("    [OK] 国家切换点击已生效，国家列表已出现")
            break
        print("    [WARN] 点击后未出现国家列表，正在重试...")

    if not country_list_open:
        print("    [FAIL] 第 6 步失败：无法打开国家列表")
        return

    # 在国家列表中下滑并选择美国
    print("    搜索「United States」...")
    found = False
    for scroll_attempt in range(10):
        elements = get_ui_elements_safe(DEVICE_ID)
        us_elem = find_element_by_text("United States", elements, exact_match=False)
        if not us_elem:
            us_elem = find_element_by_text("美国", elements, exact_match=False)
        if not us_elem:
            us_elem = find_element_by_desc("United States", elements, exact_match=False)

        if us_elem and us_elem.clickable and us_elem.enabled:
            _tap_elem(us_elem)
            if wait_for_ui_condition(is_us_country_selected, timeout=3) is not None:
                print("    [OK] 已选择 United States，区号已验证为 +1")
                found = True
                break
        elif us_elem:
            _tap_elem(us_elem)
            if wait_for_ui_condition(is_us_country_selected, timeout=3) is not None:
                print("    [OK] 已选择 United States，区号已验证为 +1")
                found = True
                break

        if not found:
            scroll_down_in_list()
            time.sleep(CLICK_SETTLE_DELAY)

    if not found:
        print("    [WARN] 未找到 United States，尝试直接输入查找...")
        search_inputs = find_text_input_elements(get_ui_elements_safe(DEVICE_ID))
        if search_inputs and input_text_verified(search_inputs[0], "United States"):
            for _ in range(5):
                elements = get_ui_elements_safe(DEVICE_ID)
                us_elem = find_element_by_text("United States", elements, exact_match=False)
                if us_elem:
                    _tap_elem(us_elem)
                    if wait_for_ui_condition(is_us_country_selected, timeout=3) is not None:
                        print("    [OK] 通过搜索选择 United States，区号已验证为 +1")
                        found = True
                        break
                time.sleep(CLICK_RETRY_DELAY)

    if not found:
        print("    [FAIL] 第 6 步未通过验证：国家区号不是 United States +1")
        return

    time.sleep(CLICK_SETTLE_DELAY)

    # ---- 第 7 步：输入手机号 ----
    print(f"\n[7] 输入手机号 {PHONE_NUMBER}...")
    phone_elements = log_global_ui_elements("7 输入手机号前")

    if input_phone_number(PHONE_NUMBER, phone_elements):
        print(f"    [OK] 手机号已输入并验证: {PHONE_NUMBER}")
    else:
        print("    [FAIL] 手机号输入失败，停止任务，避免提交空号码")
        return

    # 收起键盘，避免挡住登录按钮
    print("    [INFO] 收起键盘...")
    adb_cmd("shell", "input", "keyevent", "4")
    time.sleep(CLICK_SETTLE_DELAY)
    # 再点一下输入框外的空白区域确保键盘完全收起
    _tap_xy(500, 500)
    time.sleep(CLICK_SETTLE_DELAY)

    # ---- 第 8 步：勾选协议 RadioButton 并点击登录 ----
    print("\n[8] 勾选服务协议和隐私政策并点击登录...")
    agreement_elements = log_global_ui_elements("8 勾选协议并登录前")
    agreement_ready = False
    agreement_state_readable = False
    for attempt in range(1, 4):
        if attempt > 1 or not agreement_elements:
            agreement_elements = get_ui_elements_safe(DEVICE_ID)
        agreement_control = find_agreement_control_element(agreement_elements)
        if agreement_control is None:
            print(f"    [WARN] 第 {attempt} 次未找到协议控件或 protocol 文本")
            time.sleep(CLICK_RETRY_DELAY)
            continue

        if "RadioButton" in (agreement_control.class_name or ""):
            agreement_state_readable = True
            if getattr(agreement_control, "checked", False):
                agreement_ready = True
                print("    [OK] 协议 RadioButton 已是选中状态（checked=true）")
                break

            print(f"      → 协议 RadioButton: {_elem_desc(agreement_control)}")
            tap_agreement_control(agreement_control)
            checked_elements = wait_for_ui_condition(
                agreement_radio_is_checked,
                timeout=1.5,
                interval=CLICK_POLL_INTERVAL,
            )
            if checked_elements is not None:
                agreement_ready = True
                print("    [OK] 协议 RadioButton 已勾选并读回 checked=true")
                break
            print(f"    [WARN] 第 {attempt} 次点击后 RadioButton 仍未选中")
            continue

        print(f"      → 协议区域: {_elem_desc(agreement_control)}")
        tap_agreement_control(agreement_control)
        agreement_ready = True
        print("    [ACTION] 已点击协议 checkbox 区域，等待登录跳转验证勾选结果")
        break

    if not agreement_ready:
        print("    [FAIL] 无法找到或操作服务协议控件，停止登录")
        return

    if not agreement_state_readable:
        current_elements = get_ui_elements_safe(DEVICE_ID)
        if find_agreement_control_element(current_elements) is None:
            print("    [FAIL] 点击协议区域后登录页控件状态异常，停止登录")
            return
        print("    [INFO] 当前版本不暴露 checked 属性，将由登录页跳转作最终验证")

    login_clicked = find_and_tap_safe(text="登录", clickable=True, retries=3)
    if login_clicked:
        print("    [ACTION] 已执行登录点击，等待页面状态验证")
    else:
        print("    [WARN] 尝试其他方式...")
        if find_and_tap_safe(desc="登录", clickable=True, retries=3):
            print("    [ACTION] 已通过 desc 执行登录点击，等待页面状态验证")
        else:
            print("    [FAIL] 未找到登录按钮")
            return

    time.sleep(CLICK_SETTLE_DELAY)

    # ---- 第 9 步：验证登录提交结果 ----
    print("\n[9] 验证协议勾选和登录提交状态...")
    submission_elements = log_global_ui_elements("9 验证登录提交状态")
    submission_verified = False
    login_retried = False
    non_login_rounds = 0
    for attempt in range(10):
        try:
            elems = (
                submission_elements
                if attempt == 0 and submission_elements
                else get_ui_elements_safe(DEVICE_ID)
            )
        except Exception as exc:
            print(f"    [WARN] 读取登录提交状态失败，正在重试: {exc}")
            time.sleep(CLICK_RETRY_DELAY)
            continue

        if not elems:
            print("    [WARN] UI 数据为空，暂不判断登录页已离开")
            time.sleep(CLICK_RETRY_DELAY)
            continue

        if not is_login_page(elems):
            non_login_rounds += 1
            if non_login_rounds >= 2:
                submission_verified = True
                print("    [OK] 连续两次确认已离开登录页")
                break
            time.sleep(CLICK_SETTLE_DELAY)
            continue

        non_login_rounds = 0
        agreement_control = find_agreement_control_element(elems)
        if agreement_control is None:
            print("    [FAIL] 仍在登录页，但协议控件已无法识别")
            return
        agreement_radio = find_agreement_radio_element(elems)
        if agreement_radio is not None and not getattr(agreement_radio, "checked", False):
            print("    [WARN] 登录页协议 RadioButton 变为未选中，重新勾选并验证...")
            tap_agreement_control(agreement_radio)
            if wait_for_ui_condition(
                agreement_radio_is_checked,
                timeout=1.5,
                interval=CLICK_POLL_INTERVAL,
            ) is None:
                print("    [FAIL] 协议 RadioButton 重新勾选失败")
                return
            print("    [OK] 协议 RadioButton 已重新验证为 checked=true")

        if attempt >= 2 and not login_retried:
            login_button = _find_element_by_id_candidates(
                elems, ["login_register_btn"]
            )
            if not login_button:
                login_button = next(
                    (elem for elem in elems if (elem.text or "").strip() == "登录"),
                    None,
                )
            if login_button is None:
                print("    [FAIL] 仍在登录页，但无法找到登录按钮")
                return
            print("    [WARN] 仍停留在登录页，重新点击登录并继续验证...")
            _tap_elem(login_button)
            login_retried = True

        time.sleep(CLICK_SETTLE_DELAY)

    if not submission_verified:
        print("    [FAIL] 协议虽已操作，但点击登录后仍未离开登录页")
        return
    if agreement_state_readable:
        print("    [OK] 第 8/9 步验证通过：RadioButton 已勾选且登录已提交")
    else:
        print("    [OK] 第 8/9 步验证通过：协议区域点击已由登录页跳转确认")

    # 给验证码 WebView/短信页一个很短的加载窗口，替代原来的固定 20 秒等待。
    time.sleep(1)
    print("\n[9.5] 检查是否有安全验证...")
    elements = (
        log_global_ui_elements("9.5 检查安全验证前")
        or get_ui_elements_safe(DEVICE_ID)
    )
    # 等待10秒

    has_captcha = find_element_by_id("tcaptcha-img", elements) is not None
    security_texts = [e.text for e in elements if e.text and any(
        kw in e.text for kw in ["验证", "安全", "captcha", "CAPTCHA", "滑块","安全验证"]
    )]
    if has_captcha or security_texts:
        if security_texts:
            print(f"    发现安全验证: {security_texts}")
        else:
            print("    发现安全验证 (tcaptcha-img)")
        if not verify_captcha_with_jfbym():
            print("    [WARN] 云码识别未完成，尝试重新验证...")
            # 再试一轮（刷新验证码重新识别）
            if not verify_captcha_with_jfbym():
                print("    [FAIL] 安全验证多次失败，停止任务")
                return
        print("    [OK] 安全验证已通过")
    else:
        print("    - 无安全验证，直接进入登录流程")

    time.sleep(CLICK_SETTLE_DELAY)

    # ---- 第 10 步：获取并输入短信验证码 ----
    print("\n[10] 获取短信验证码...")
    log_global_ui_elements("10 获取短信验证码前")
    code = wait_for_sms_code(max_wait=120, interval=5)

    if not code:
        print("    [FAIL] 未能获取验证码")
        return

    print(f"\n[11] 输入验证码: {code}...")
    elements = (
        log_global_ui_elements("11 输入短信验证码前")
        or get_ui_elements_safe(DEVICE_ID)
    )

    # 找验证码输入框
    code_inputs = [e for e in elements if 'EditText' in e.class_name]
    if not code_inputs:
        print("    [FAIL] 第 11 步失败：未找到验证码输入框")
        return
    if not input_text_verified(code_inputs[0], code):
        print("    [FAIL] 第 11 步失败：验证码未能从输入框读回确认")
        return
    print(f"    [OK] 验证码已输入并读回验证: {code}")

    time.sleep(CLICK_SETTLE_DELAY)

    # 确认/提交验证码
    print("\n[12] 提交验证码...")
    submit_elements = log_global_ui_elements("12 提交短信验证码前")
    # 查找并点击提交按钮（确认/确定/下一步/提交）
    submitted = False
    for submit_attempt in range(8):
        if submit_attempt == 0 and submit_elements:
            elements = submit_elements
        else:
            time.sleep(CLICK_SETTLE_DELAY)
            elements = get_ui_elements_safe(DEVICE_ID)
        if is_username_or_home_page(elements):
            print("    [OK] 验证码已自动提交，后续页面已出现")
            submitted = True
            break
        for btn_text in ["确认", "确定", "下一步", "提交", "登录"]:
            btn = find_with_multiple_conditions(
                text=btn_text, clickable=True, exact_match=True,
                elements=elements, device_id=DEVICE_ID
            )
            if btn and btn.rect[3] > 1800:  # 底部按钮
                print(f"    [ACTION] 点击「{btn_text}」提交验证码")
                _tap_elem(btn)
                if wait_for_ui_condition(is_username_or_home_page, timeout=5) is not None:
                    print("    [OK] 验证码提交已生效，后续页面已出现")
                    submitted = True
                break
        if submitted:
            break

    if not submitted:
        print("    [FAIL] 第 12 步未通过验证：提交后未进入用户名页或主页")
        return

    time.sleep(CLICK_SETTLE_DELAY)

    # ============================================================
    # 后续操作：新账号用户名 → 搜索游戏 → 下载 → 安装 → 启动
    # ============================================================

    # ---- 第 13 步：处理新账号用户名填写 ----
    print("\n[13] 检查用户名填写页面...")
    username_elements = log_global_ui_elements("13 检查用户名页面")
    if not is_username_or_home_page(username_elements):
        username_elements = wait_for_ui_condition(
            is_username_or_home_page,
            timeout=8,
        )
    if username_elements is None:
        print("    [FAIL] 第 13 步失败：未出现用户名页、个人页或主页")
        return

    if is_home_or_profile_page(username_elements):
        print("    [OK] 无需填写用户名，已验证进入个人页或主页")
    else:
        input_elems = find_text_input_elements(username_elements)
        if not input_elems:
            print("    [FAIL] 用户名页面未找到输入框")
            return

        random_name = "User" + str(random.randint(10000, 99999))
        if not input_text_verified(input_elems[0], random_name):
            print("    [FAIL] 用户名输入后无法读回验证")
            return
        print(f"    [OK] 用户名已输入并验证: {random_name}")

        elements = get_ui_elements_safe(DEVICE_ID)
        done_btn = None
        for button_text in ["完成", "确定", "确认", "提交", "下一步"]:
            done_btn = find_with_multiple_conditions(
                text=button_text,
                clickable=True,
                exact_match=True,
                elements=elements,
                device_id=DEVICE_ID,
            )
            if done_btn:
                break
        if not done_btn:
            print("    [FAIL] 用户名已输入，但未找到提交按钮")
            return
        _tap_elem(done_btn)
        if wait_for_ui_condition(is_home_or_profile_page, timeout=6) is None:
            print("    [FAIL] 用户名提交后未进入个人页或主页")
            return
        print("    [OK] 用户名提交已生效，后续页面已出现")

    time.sleep(CLICK_SETTLE_DELAY)

    # ---- 第 14 步：回到主页并点击搜索栏 ----
    print("\n[14] 回到主页...")
    home_elements = log_global_ui_elements("14 返回主页前")
    home_ready = False
    for attempt in range(8):
        if attempt == 0 and home_elements:
            elements = home_elements
        else:
            time.sleep(CLICK_SETTLE_DELAY)
            elements = get_ui_elements_safe(DEVICE_ID)

        # 多条件检测是否已在主页
        is_home = False
        home_indicators = [
            "com.taptap:id/tb_layout_home_bottom_bar",
            "com.taptap:id/viewSearchContent",
            "com.taptap:id/tsi_search_banner_key_text",
        ]
        for rid in home_indicators:
            if find_element_by_id(rid, elements):
                is_home = True
                break
        if not is_home:
            bottom_bar = find_with_multiple_conditions(
                text="找游戏", elements=elements, device_id=DEVICE_ID
            )
            if bottom_bar:
                is_home = True
        if not is_home:
            for text_keyword in ["找游戏", "排行榜", "我的游戏"]:
                if find_with_multiple_conditions(text=text_keyword, elements=elements, device_id=DEVICE_ID):
                    is_home = True
                    break

        if is_home:
            print("    [OK] 已在主页")
            home_ready = True
            break

        dismiss = find_element_by_id("com.taptap:id/btn_dismiss", elements)
        if dismiss:
            _tap_elem(dismiss)
            time.sleep(CLICK_SETTLE_DELAY)
            continue

        container = find_element_by_id("com.taptap:id/btn_container", elements)
        if container:
            _tap_elem(container)
            time.sleep(CLICK_SETTLE_DELAY)
            continue

        back(DEVICE_ID)
        time.sleep(CLICK_SETTLE_DELAY)

    if not home_ready:
        print("    [FAIL] 第 14 步失败：无法验证已回到主页")
        return

    print("    点击搜索栏...")
    # search_clicked = find_and_tap_safe(
    #     res_id="com.taptap:id/tsi_search_banner_key_text", clickable=True, retries=5
    # )
    # if not search_clicked:
    #     search_clicked = find_and_tap_safe(
    #         res_id="com.taptap:id/tvSearchKey", clickable=True, retries=5
    #     )
    # if not search_clicked:
    search_clicked = find_and_tap_safe(
        res_id="com.taptap:id/viewSearchContent", clickable=True, retries=3
    )
    if search_clicked:
        if wait_for_ui_condition(is_search_page, timeout=3) is None:
            print("    [FAIL] 搜索栏点击后未进入搜索页面")
            return
        print("    [OK] 搜索栏点击已生效，搜索页面已出现")
    else:
        print("    [FAIL] 未找到搜索栏")
        return

    time.sleep(CLICK_SETTLE_DELAY)

    # ---- 第 15 步：搜索游戏 ----
    print(f"\n[15] 搜索游戏「{GAME_NAME}」...")
    elements = (
        log_global_ui_elements("15 搜索游戏前")
        or get_ui_elements_safe(DEVICE_ID)
    )

    # input_clicked = find_and_tap_safe(
    #     res_id="com.taptap:id/input_box", clickable=True, retries=5
    # )
    # if input_clicked:
    #     time.sleep(0.5)
    #     original_ime = detect_and_set_adb_keyboard(DEVICE_ID)
    #     time.sleep(0.5)
    #     clear_text(DEVICE_ID)
    #     time.sleep(0.3)
    #     type_text(GAME_NAME, DEVICE_ID)
    #     time.sleep(1)
    #     restore_keyboard(original_ime, DEVICE_ID)
    #     print("    [OK] 已输入搜索词")
    # else:
    inputs = find_text_input_elements(elements)
    if inputs:
        if not input_text_verified(inputs[0], GAME_NAME):
            print("    [FAIL] 搜索词输入后无法从输入框读回验证")
            return
        print(f"    [OK] 搜索词已输入并验证: {GAME_NAME}")
    else:
        print("    [FAIL] 无法输入搜索词")
        return

    time.sleep(CLICK_SETTLE_DELAY)
    print("    点击搜索按钮...")
    search_btn = find_and_tap_safe(
        text="搜索", clickable=True, retries=3
    )
    if not search_btn:
        search_btn = find_and_tap_safe(
            res_id="com.taptap:id/tvSure", clickable=True, retries=3
        )
    if not search_btn:
        enter_result = adb_cmd("shell", "input", "keyevent", "66")
        if enter_result.returncode != 0:
            print("    [FAIL] 搜索按钮和回车提交均执行失败")
            return

    if wait_for_ui_condition(
        lambda items: search_results_visible(items, GAME_NAME),
        timeout=6,
    ) is None:
        print("    [FAIL] 第 15 步未通过验证：未出现搜索结果")
        return
    print("    [OK] 搜索已提交，目标游戏结果已出现")

    time.sleep(CLICK_SETTLE_DELAY)

    # ---- 第 16 步：选择游戏 ----
    print(f"\n[16] 选择游戏「{GAME_NAME}」...")
    elements = (
        log_global_ui_elements("16 选择游戏前")
        or get_ui_elements_safe(DEVICE_ID)
    )
    game_clicked = False

    # 1) 搜索完成后，过滤掉输入框本身，只找结果列表中的游戏名
    # 排除输入框（搜索栏里的文字）
    candidates = [
        e for e in elements
        if not (e.class_name and "EditText" in e.class_name)
        and (e.resource_id and "title" in e.resource_id.lower())
    ]
    if not candidates:
        candidates = [
            e for e in elements
            if not (e.class_name and "EditText" in e.class_name)
        ]
    matches = find_elements_by_text(GAME_NAME, candidates, exact_match=False)
    if matches:
        title_elem = matches[0]
        cx = (title_elem.rect[0] + title_elem.rect[2]) // 2
        cy = (title_elem.rect[1] + title_elem.rect[3]) // 2
        print(f"    [ACTION] 找到「{GAME_NAME}」({cx},{cy})，点击中心")
        _tap_xy(cx, cy)
        time.sleep(CLICK_SETTLE_DELAY)
        game_clicked = True

    # 2) 备选：找 brand_app
    if not game_clicked:
        game_clicked = find_and_tap_safe(
            res_id="com.taptap:id/brand_app", clickable=True, retries=3
        )

    # 3) 再备选：找第一个游戏结果项
    if not game_clicked:
        print("    [WARN] 未找到品牌游戏，尝试点击第一个游戏结果...")
        for e in elements:
            if ("brand" in (e.resource_id or "").lower()
                    or "title" in (e.resource_id or "").lower()
                    or "item" in (e.resource_id or "").lower()):
                if e.clickable:
                    _tap_elem(e)
                    time.sleep(CLICK_SETTLE_DELAY)
                    game_clicked = True
                    break
        if not game_clicked:
            # 最后尝试：找 bounds 大的 ViewGroup
            for e in sorted(elements, key=lambda x: (x.rect[2]-x.rect[0])*(x.rect[3]-x.rect[1]), reverse=True):
                if e.class_name and "ViewGroup" in e.class_name and e.clickable:
                    _tap_elem(e)
                    time.sleep(CLICK_SETTLE_DELAY)
                    game_clicked = True
                    break

    if not game_clicked:
        print("    [FAIL] 未找到游戏结果")
        return

    if wait_for_ui_condition(
        lambda items: is_game_detail_page(items, GAME_NAME),
        timeout=5,
    ) is None:
        print("    [FAIL] 第 16 步未通过验证：点击后未进入目标游戏详情页")
        return
    print("    [OK] 已验证进入目标游戏详情页")
    time.sleep(CLICK_SETTLE_DELAY)

    # ---- 第 17 步：点击下载 ----
    print("\n[17] 点击下载...")
    elements = (
        log_global_ui_elements("17 点击下载前")
        or get_ui_elements_safe(DEVICE_ID)
    )
    download_clicked = False

    # 1) 页面底部可能有多个"下载"文本，取最底部的（真正的下载按钮在底部）
    download_texts = find_elements_by_text("下载", elements, exact_match=False)
    text_elem = None
    if download_texts:
        text_elem = max(download_texts, key=lambda e: e.rect[3])
        tx, ty = (text_elem.rect[0] + text_elem.rect[2]) // 2, (text_elem.rect[1] + text_elem.rect[3]) // 2
        # 向上找可点击的父容器（优先找 btn_container）
        for e in elements:
            if e.clickable and e.rect[0] <= tx <= e.rect[2] and e.rect[1] <= ty <= e.rect[3]:
                if "btn_container" in (e.resource_id or ""):
                    _tap_elem(e)
                    download_clicked = True
                    print("    [ACTION] 已点击「下载」按钮 (btn_container)")
                    break
        if not download_clicked:
            for e in elements:
                if e.clickable and e.rect[0] <= tx <= e.rect[2] and e.rect[1] <= ty <= e.rect[3]:
                    _tap_elem(e)
                    download_clicked = True
                    print(f"    [ACTION] 已点击下载父容器 ({e.resource_id or e.class_name})")
                    break

    # 2) 备选：btn_container
    if not download_clicked:
        download_clicked = find_and_tap_safe(
            res_id="com.taptap:id/btn_container", clickable=True, retries=3
        )

    if not download_clicked:
        print("    [FAIL] 未找到下载按钮")
        return

    if wait_for_ui_condition(download_has_started, timeout=8) is None:
        print("    [FAIL] 第 17 步未通过验证：下载状态没有发生变化")
        return
    print("    [OK] 下载点击已生效，已检测到下载/安装状态")

    time.sleep(CLICK_SETTLE_DELAY)
    print("\n[18] 监控下载进度 & 处理安装弹窗...")
    download_elements = log_global_ui_elements("18 监控下载和安装前")
    download_complete = False
    installed = False
    installer_checkbox_clicked = False

    for check_round in range(240):
        try:
            if check_round == 0 and download_elements:
                elements = download_elements
            else:
                time.sleep(1)
                elements = get_ui_elements_safe(DEVICE_ID)
        except RuntimeError:
            continue

        # ==== A) 检查下载状态 ====
        # 只在未完成下载时检测下载进度
        if not download_complete:
            btn_container = find_with_multiple_conditions(
                res_id="com.taptap:id/btn_container", elements=elements, device_id=DEVICE_ID
            )
            if btn_container:
                any_install_text = find_all_with_multiple_conditions(
                    text="安装", exact_match=False,
                    elements=elements, device_id=DEVICE_ID
                )
                if any_install_text:
                    print(f"    [OK] 下载完成，检测到「安装」文本")
                    download_complete = True
                elif check_round % 20 == 0:
                    any_download_text = find_with_multiple_conditions(
                        text="下载", exact_match=False,
                        elements=elements, device_id=DEVICE_ID
                    )
                    if any_download_text:
                        print(f"    下载中... (btn_container 存在，文本: {any_download_text.text})")
                    else:
                        print(f"    下载中... (btn_container 存在)")
            else:
                any_install_text = find_all_with_multiple_conditions(
                    text="安装", exact_match=False,
                    elements=elements, device_id=DEVICE_ID
                )
                if any_install_text:
                    print(f"    [OK] 检测到「安装」文本，下载完成")
                    download_complete = True
                elif check_round % 20 == 0:
                    print(f"    等待下载按钮出现... (round {check_round})")

            # 仍在下载中（进度提示）
            if check_round % 20 == 0 and not download_complete:
                progress_texts = [e.text for e in elements if e.text and any(
                    kw in e.text for kw in ["下载", "MB", "KB", "%"]
                )]
                if progress_texts:
                    print(f"    下载中... {progress_texts[:2]}")
        else:
            if check_round % 20 == 0:
                print(f"    安装处理中... (已等待 {(check_round+1)*1.5:.0f}s)")

        # ==== B) 同时检测 MIUI 安装弹窗（下载过程中随时可能出现） ====
        # 弹窗1: "TapTap正尝试安装应用" → "继续"
        continue_btn = find_with_multiple_conditions(
            res_id="android:id/button3", text="继续",
            clickable=True, elements=elements, device_id=DEVICE_ID
        )
        if continue_btn:
            download_complete = True
            print("    [ACTION] 发现安装请求弹窗，点击「继续」")
            _tap_elem(continue_btn)
            time.sleep(CLICK_RETRY_DELAY)
            continue

        # 弹窗2: "是否允许TapTap安装应用？" → "允许"
        allow_btn = find_with_multiple_conditions(
            res_id="android:id/button2", text="允许",
            clickable=True, elements=elements, device_id=DEVICE_ID
        )
        if allow_btn:
            download_complete = True
            print("    [ACTION] 发现安装权限弹窗，点击「允许」")
            _tap_elem(allow_btn)
            time.sleep(CLICK_RETRY_DELAY)
            continue

        # 弹窗2b: "TapTap频繁安装应用" → 点"验证"
        verify_title = find_with_multiple_conditions(
            text="TapTap频繁安装应用", exact_match=False,
            elements=elements, device_id=DEVICE_ID
        )
        if verify_title:
            verify_btn = find_with_multiple_conditions(
                text="验证", clickable=True, exact_match=False,
                elements=elements, device_id=DEVICE_ID
            )
            if verify_btn:
                download_complete = True
                print("    [ACTION] 发现频繁安装验证弹窗，点击「验证」")
                _tap_elem(verify_btn)
                time.sleep(CLICK_RETRY_DELAY)
                continue
            # 找不到按钮就点 title 区域下方（弹窗右下角）
            download_complete = True
            print("    [ACTION] 点击验证区域")
            _tap_xy(720, 2501)
            time.sleep(CLICK_RETRY_DELAY)
            continue

        # 弹窗2c: 拖动滑块验证（WebView 内嵌）
        slider_text = find_with_multiple_conditions(
            text="拖动滑块完成拼图", exact_match=False,
            elements=elements, device_id=DEVICE_ID
        )
        if slider_text:
            # 找实际的滑块把手：文字左侧/附近的窄 TextView（无文本、可点击）
            slider_btn = None
            for e in elements:
                if (not e.text and e.clickable
                        and e.class_name and "TextView" in e.class_name
                        and e.rect[1] >= slider_text.rect[1] - 200
                        and e.rect[3] <= slider_text.rect[3] + 200
                        and (e.rect[2] - e.rect[0]) < 350
                        and e.rect[2] < slider_text.rect[0] + 50):
                    slider_btn = e
                    break

            if slider_btn:
                sx = (slider_btn.rect[0] + slider_btn.rect[2]) // 2
                sy = (slider_btn.rect[1] + slider_btn.rect[3]) // 2
            else:
                sx = slider_text.rect[0] + 115
                sy = (slider_text.rect[1] + slider_text.rect[3]) // 2

            ex = sx + 700
            print(f"    [ACTION] 滑块验证: 把手={_elem_desc(slider_btn) if slider_btn else 'fallback'} 拖动 ({sx},{sy}) → ({ex},{sy})")
            # WebView 滑块需要用 touch motionevent 模拟真实手指拖动
            _swipe_slider(sx, sy, ex, sy)
            time.sleep(CLICK_RETRY_DELAY)
            continue

        # 弹窗3: MIUI / HyperOS 安装确认页
        # 先试 checkbox（勾选"已了解此安装包未经安全检测"）
        checkbox = (
            find_with_multiple_conditions(
                res_id="checkbox", exact_match=False,
                elements=elements, device_id=DEVICE_ID
            )
            or find_with_multiple_conditions(
                res_id="miui.packageinstaller:id/checkbox", exact_match=False,
                elements=elements, device_id=DEVICE_ID
            )
            or find_with_multiple_conditions(
                res_id="android:id/checkbox", exact_match=False,
                elements=elements, device_id=DEVICE_ID
            )
            or find_with_multiple_conditions(
                class_name="CheckBox", checkable=True,
                elements=elements, device_id=DEVICE_ID
            )
        )
        if checkbox and not installer_checkbox_clicked and not getattr(checkbox, "checked", False):
            print("    [ACTION] 勾选「已了解此安装包未经安全检测」")
            _tap_elem(checkbox)
            installer_checkbox_clicked = True
            time.sleep(0.3)
            continue

        # 找"继续安装" / "安装" / "确定" / "完成" 按钮
        install_btn = (
            find_with_multiple_conditions(
                text="完成", clickable=True, exact_match=False,
                elements=elements, device_id=DEVICE_ID
            )
            or find_with_multiple_conditions(
                text="完成", exact_match=False,
                elements=elements, device_id=DEVICE_ID
            )
            or find_with_multiple_conditions(
                text="继续安装", clickable=True, exact_match=False,
                elements=elements, device_id=DEVICE_ID
            )
            or find_with_multiple_conditions(
                text="继续安装", exact_match=False,
                elements=elements, device_id=DEVICE_ID
            )
            or find_with_multiple_conditions(
                text="安装", clickable=True, exact_match=False,
                elements=elements, device_id=DEVICE_ID
            )
            or find_with_multiple_conditions(
                text="确定", clickable=True, exact_match=False,
                elements=elements, device_id=DEVICE_ID
            )
        )
        if install_btn and install_btn.rect[3] > 2000:
            btn_text = install_btn.text or ""
            download_complete = True
            print(f"    [ACTION] 点击安装流程按钮「{btn_text}」")
            _tap_elem(install_btn)
            time.sleep(CLICK_RETRY_DELAY)
            if "完成" in btn_text:
                if _package_is_installed(GAME_PACKAGE):
                    installed = True
                    print("    [OK] 安装包已通过 pm path 验证")
                    break
                print("    [WARN] 点击完成后尚未检测到安装包，继续等待...")
            continue

        # ==== C) 检查安装完成后是否回到 TapTap 主页 ====
        home_tab = find_element_by_id(
            "com.taptap:id/tb_layout_home_bottom_bar", elements
        )
        if home_tab and _package_is_installed(GAME_PACKAGE):
            installed = True
            download_complete = True
            print("    [OK] 已回到 TapTap 主页且安装包存在")
            break

        if check_round % 30 == 29:
            print(f"    等待中... (已等待 {(check_round+1)*1.5:.0f}s)")

    if not download_complete:
        print("    [WARN] 下载可能未完成，继续处理安装...")
    if not installed:
        print("    [WARN] 安装可能未完成，尝试继续处理安装弹窗...")
        # 循环结束后额外尝试一次处理安装弹窗
        fallback_checkbox_clicked = installer_checkbox_clicked
        for _ in range(40):
            try:
                elems_now = get_ui_elements_safe(DEVICE_ID)
            except RuntimeError:
                time.sleep(1)
                continue
            handled = False

            continue_btn = find_with_multiple_conditions(
                res_id="android:id/button3", text="继续",
                clickable=True, elements=elems_now, device_id=DEVICE_ID
            )
            if continue_btn:
                download_complete = True
                print("    [ACTION] 发现安装请求弹窗，点击「继续」")
                _tap_elem(continue_btn)
                time.sleep(CLICK_RETRY_DELAY)
                handled = True

            allow_btn = find_with_multiple_conditions(
                res_id="android:id/button2", text="允许",
                clickable=True, elements=elems_now, device_id=DEVICE_ID
            )
            if allow_btn:
                download_complete = True
                print("    [ACTION] 发现安装权限弹窗，点击「允许」")
                _tap_elem(allow_btn)
                time.sleep(CLICK_RETRY_DELAY)
                handled = True

            verify_title = find_with_multiple_conditions(
                text="TapTap频繁安装应用", exact_match=False,
                elements=elems_now, device_id=DEVICE_ID
            )
            if verify_title:
                verify_btn = find_with_multiple_conditions(
                    text="验证", clickable=True, exact_match=False,
                    elements=elems_now, device_id=DEVICE_ID
                )
                if verify_btn:
                    download_complete = True
                    print("    [ACTION] 点击安装验证")
                    _tap_elem(verify_btn)
                    time.sleep(CLICK_RETRY_DELAY)
                    handled = True
                else:
                    download_complete = True
                    _tap_xy(720, 2501)
                    time.sleep(CLICK_RETRY_DELAY)
                    handled = True

            slider_text = find_with_multiple_conditions(
                text="拖动滑块完成拼图", exact_match=False,
                elements=elems_now, device_id=DEVICE_ID
            )
            if slider_text:
                slider_btn = None
                for e in elems_now:
                    if (not e.text and e.clickable
                            and e.class_name and "TextView" in e.class_name
                            and e.rect[1] >= slider_text.rect[1] - 200
                            and e.rect[3] <= slider_text.rect[3] + 200
                            and (e.rect[2] - e.rect[0]) < 350
                            and e.rect[2] < slider_text.rect[0] + 50):
                        slider_btn = e
                        break
                if slider_btn:
                    sx = (slider_btn.rect[0] + slider_btn.rect[2]) // 2
                    sy = (slider_btn.rect[1] + slider_btn.rect[3]) // 2
                else:
                    sx = slider_text.rect[0] + 115
                    sy = (slider_text.rect[1] + slider_text.rect[3]) // 2
                ex = sx + 700
                print(f"    [ACTION] 滑块验证，拖动 ({sx},{sy}) → ({ex},{sy})")
                _swipe_slider(sx, sy, ex, sy)
                time.sleep(CLICK_RETRY_DELAY)
                handled = True

            checkbox = (
                find_with_multiple_conditions(
                    res_id="checkbox", exact_match=False,
                    elements=elems_now, device_id=DEVICE_ID
                )
                or find_with_multiple_conditions(
                    res_id="miui.packageinstaller:id/checkbox", exact_match=False,
                    elements=elems_now, device_id=DEVICE_ID
                )
                or find_with_multiple_conditions(
                    res_id="android:id/checkbox", exact_match=False,
                    elements=elems_now, device_id=DEVICE_ID
                )
                or find_with_multiple_conditions(
                    class_name="CheckBox", checkable=True,
                    elements=elems_now, device_id=DEVICE_ID
                )
            )
            if checkbox and not fallback_checkbox_clicked and not getattr(checkbox, "checked", False):
                print("    [ACTION] 勾选「已了解此安装包未经安全检测」")
                _tap_elem(checkbox)
                fallback_checkbox_clicked = True
                time.sleep(0.3)
                handled = True
                continue

            install_btn = (
                find_with_multiple_conditions(
                    text="完成", clickable=True, exact_match=False,
                    elements=elems_now, device_id=DEVICE_ID
                )
                or find_with_multiple_conditions(
                    text="完成", exact_match=False,
                    elements=elems_now, device_id=DEVICE_ID
                )
                or find_with_multiple_conditions(
                    text="继续安装", clickable=True, exact_match=False,
                    elements=elems_now, device_id=DEVICE_ID
                )
                or find_with_multiple_conditions(
                    text="继续安装", exact_match=False,
                    elements=elems_now, device_id=DEVICE_ID
                )
                or find_with_multiple_conditions(
                    text="安装", clickable=True, exact_match=False,
                    elements=elems_now, device_id=DEVICE_ID
                )
                or find_with_multiple_conditions(
                    text="确定", clickable=True, exact_match=False,
                    elements=elems_now, device_id=DEVICE_ID
                )
            )
            if install_btn and install_btn.rect[3] > 2000:
                btn_text = install_btn.text or ""
                download_complete = True
                print(f"    [ACTION] 点击安装流程按钮「{btn_text}」")
                _tap_elem(install_btn)
                time.sleep(CLICK_RETRY_DELAY)
                if "完成" in btn_text:
                    if _package_is_installed(GAME_PACKAGE):
                        installed = True
                        print("    [OK] 安装包已通过 pm path 验证")
                        break
                    print("    [WARN] 点击完成后尚未检测到安装包")
                handled = True

            home_tab = find_element_by_id(
                "com.taptap:id/tb_layout_home_bottom_bar", elems_now
            )
            if home_tab and _package_is_installed(GAME_PACKAGE):
                installed = True
                download_complete = True
                print("    [OK] 已回到 TapTap 主页且安装包存在")
                break

            if not handled:
                time.sleep(0.75)
        if not installed:
            print("    [FAIL] 第 18 步未通过验证：安装包不存在")
            return

    if not download_complete or not _package_is_installed(GAME_PACKAGE):
        print("    [FAIL] 第 18 步未通过验证：下载或安装状态不完整")
        return
    print("    [OK] 第 18 步验证通过：下载完成且安装包已存在")

    # ---- 第 19 步：启动游戏 ----
    _log("\n[19] 启动游戏并等待...")
    log_global_ui_elements("19 启动游戏前")
    launch_result = adb_cmd(
        "shell", "monkey", "-p", GAME_PACKAGE,
        "-c", "android.intent.category.LAUNCHER", "1",
        timeout=15,
    )
    if launch_result.returncode != 0:
        _log(f"    [FAIL] 游戏启动命令失败: {launch_result.stderr.strip()}")
        return
    if not _wait_package_foreground(GAME_PACKAGE, timeout=15):
        _log("    [FAIL] 第 19 步未通过验证：游戏进程未进入前台")
        return
    _log(f"    [OK] 已验证游戏进程在前台运行: {GAME_PACKAGE}")

    _log("\n" + "=" * 60)
    _log("全部流程完成！")
    _log("=" * 60)

    monitor.stop()
    _WORKFLOW_COMPLETED = True


if __name__ == "__main__":
    try:
        main()
    finally:
        _close_log()
    if not _WORKFLOW_COMPLETED:
        sys.exit(1)
