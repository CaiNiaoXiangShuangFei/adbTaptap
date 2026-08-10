"""跨平台定位 Android Debug Bridge 可执行文件。"""

import os
import shutil
from collections.abc import Iterable


def _resolve_candidate(value: str | None) -> str | None:
    if not value:
        return None

    value = os.path.expandvars(os.path.expanduser(value.strip().strip('"')))
    if not value:
        return None

    if os.path.isdir(value):
        for name in ("adb.exe", "adb"):
            path = os.path.join(value, name)
            if os.path.isfile(path):
                return os.path.abspath(path)
        return None

    if os.path.isfile(value):
        return os.path.abspath(value)

    found = shutil.which(value)
    return os.path.abspath(found) if found else None


def find_adb(
    explicit: str | None = None,
    base_dirs: Iterable[str] = (),
) -> str | None:
    """按明确配置、PATH、项目目录和 Android SDK 环境依次查找 adb。"""
    if explicit is not None:
        return _resolve_candidate(explicit)

    configured = os.environ.get("ADB_PATH")
    if configured:
        found = _resolve_candidate(configured)
        if found:
            return found

    found = _resolve_candidate("adb")
    if found:
        return found

    roots: list[str] = []
    for base_dir in base_dirs:
        if base_dir:
            roots.append(os.path.abspath(base_dir))

    for sdk_var in ("ANDROID_SDK_ROOT", "ANDROID_HOME"):
        sdk_root = os.environ.get(sdk_var)
        if sdk_root:
            roots.append(os.path.abspath(os.path.expanduser(os.path.expandvars(sdk_root))))

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        roots.append(os.path.join(local_app_data, "Android", "Sdk"))

    user_home = os.path.expanduser("~")
    roots.extend([
        os.path.join(user_home, "Android", "Sdk"),
        os.path.join(user_home, "Library", "Android", "sdk"),
    ])

    seen: set[str] = set()
    for root in roots:
        normalized = os.path.normcase(os.path.normpath(root))
        if normalized in seen:
            continue
        seen.add(normalized)
        for candidate in (
            os.path.join(root, "platform-tools"),
            os.path.join(root, "scrcpy"),
            os.path.join(root, "runtime", "android-sdk", "platform-tools"),
            os.path.join(root, "android-sdk", "platform-tools"),
            os.path.join(root, "tools", "platform-tools"),
            root,
        ):
            found = _resolve_candidate(candidate)
            if found:
                return found

    return None
