import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from adb_manager import emulator_manager


class EmulatorManagerTests(unittest.TestCase):
    def setUp(self):
        project_runtime = os.path.join(os.path.dirname(os.path.dirname(__file__)), "runtime")
        os.makedirs(project_runtime, exist_ok=True)
        self.project_dir = tempfile.mkdtemp(prefix="emulator-test-", dir=project_runtime)
        emulator_manager.configure(os.path.join(self.project_dir, "adb.exe"), self.project_dir)

    def tearDown(self):
        shutil.rmtree(self.project_dir, ignore_errors=True)

    def test_runtime_environment_is_project_local(self):
        env = emulator_manager.runtime_env()
        for key in (
            "ANDROID_HOME", "ANDROID_SDK_ROOT", "ANDROID_USER_HOME",
            "ANDROID_AVD_HOME", "ANDROID_SDK_HOME", "JAVA_HOME", "HOME",
            "USERPROFILE", "TEMP", "TMP", "APPDATA", "LOCALAPPDATA",
            "GRADLE_USER_HOME",
        ):
            value = os.path.abspath(env[key])
            self.assertEqual(os.path.commonpath([self.project_dir, value]), self.project_dir, key)

    def test_profile_labels_cannot_inject_avd_config(self):
        for path in (
            emulator_manager._exe("emulator", "emulator.exe"),
            emulator_manager._exe("cmdline-tools", "latest", "bin", "avdmanager.bat"),
            emulator_manager._exe("system-images", "android-35", "google_apis", "x86_64", "package.xml"),
            emulator_manager._exe("system-images", "android-35", "google_apis", "x86_64", "system.img"),
        ):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "ab").close()

        def fake_run(command, **_kwargs):
            avd_path = command[command.index("--path") + 1]
            os.makedirs(avd_path, exist_ok=True)
            open(os.path.join(avd_path, "config.ini"), "a", encoding="utf-8").close()
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch.object(emulator_manager, "_run", side_effect=fake_run):
            result = emulator_manager.create_profile({
                "name": "Test Phone ../ 01",
                "brand_label": "Brand\nhw.keyboard=no",
                "model_label": "Model\nfoo=bar",
                "width": 1080,
                "height": 2400,
                "dpi": 420,
                "memory": 3072,
            })

        self.assertTrue(result["ok"], result)
        profile = result["profile"]
        self.assertNotIn("\n", profile["brand_label"])
        self.assertNotIn("=", profile["model_label"])
        config_path = os.path.join(emulator_manager.AVD_HOME, profile["name"] + ".avd", "config.ini")
        with open(config_path, encoding="utf-8") as config_file:
            config = config_file.read()
        self.assertIn("hw.lcd.width=1080", config)
        self.assertIn("hw.ramSize=3072", config)
        self.assertNotIn("\nfoo=bar", config)

        with mock.patch.object(emulator_manager, "_running_avds", return_value={}):
            updated = emulator_manager.update_profile({
                "name": profile["name"], "brand_label": "Samsung",
                "model_label": "Galaxy Test", "width": 720, "height": 1600,
                "dpi": 320, "memory": 2048,
            })
        self.assertTrue(updated["ok"], updated)
        with open(config_path, encoding="utf-8") as config_file:
            updated_config = config_file.read()
        self.assertIn("hw.device.manufacturer=Samsung", updated_config)
        self.assertIn("hw.device.name=Galaxy Test", updated_config)
        self.assertIn("hw.lcd.width=720", updated_config)


if __name__ == "__main__":
    unittest.main()
