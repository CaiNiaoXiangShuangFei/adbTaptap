"""设备信息报告示例插件。"""

import argparse
import subprocess


def adb_text(adb_path: str, serial: str, command: str) -> str:
    result = subprocess.run(
        [adb_path, "-s", serial, "shell", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stdout + result.stderr).strip() or f"ADB 退出码 {result.returncode}")
    return result.stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", required=True)
    parser.add_argument("--adb", required=True)
    args = parser.parse_args()
    print(f"设备: {args.device}")
    print("型号:", adb_text(args.adb, args.device, "getprop ro.product.model"))
    print("Android:", adb_text(args.adb, args.device, "getprop ro.build.version.release"))
    print("分辨率:", adb_text(args.adb, args.device, "wm size"))
    battery = adb_text(args.adb, args.device, "dumpsys battery")
    level = next((line.split(":", 1)[1].strip() for line in battery.splitlines() if line.strip().startswith("level:")), "未知")
    print("电量:", level + ("%" if level.isdigit() else ""))


if __name__ == "__main__":
    main()
