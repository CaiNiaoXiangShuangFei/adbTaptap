import os
import tempfile
import unittest

from adb_locator import find_adb


class AdbLocatorTests(unittest.TestCase):
    def test_finds_bundled_scrcpy_adb_without_absolute_path(self):
        project_runtime = os.path.join(os.path.dirname(os.path.dirname(__file__)), "runtime")
        os.makedirs(project_runtime, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="adb-locator-", dir=project_runtime) as project:
            scrcpy_dir = os.path.join(project, "scrcpy")
            os.makedirs(scrcpy_dir)
            adb_path = os.path.join(scrcpy_dir, "adb.exe")
            with open(adb_path, "wb") as adb_file:
                adb_file.write(b"test")
            self.assertEqual(find_adb(base_dirs=[project]), os.path.abspath(adb_path))

    def test_finds_project_local_emulator_platform_tools(self):
        project_runtime = os.path.join(os.path.dirname(os.path.dirname(__file__)), "runtime")
        os.makedirs(project_runtime, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="adb-locator-", dir=project_runtime) as project:
            tools_dir = os.path.join(project, "runtime", "android-sdk", "platform-tools")
            os.makedirs(tools_dir)
            adb_path = os.path.join(tools_dir, "adb.exe")
            with open(adb_path, "wb") as adb_file:
                adb_file.write(b"test")
            self.assertEqual(find_adb(base_dirs=[project]), os.path.abspath(adb_path))


if __name__ == "__main__":
    unittest.main()
