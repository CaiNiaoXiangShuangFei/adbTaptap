import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import desktop_app


class FakeWindow:
    def __init__(self):
        self.on_top = False
        self.loaded_url = ""

    def get_current_url(self):
        return "http://127.0.0.1:12345/?desktop=1"

    def load_url(self, url):
        self.loaded_url = url


class DesktopAppTests(unittest.TestCase):
    def test_window_state_is_saved_inside_selected_runtime_path(self):
        project_runtime = Path(__file__).resolve().parents[1] / "runtime"
        project_runtime.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="desktop-state-", dir=project_runtime) as folder:
            state_path = Path(folder) / "window_state.json"
            with mock.patch.object(desktop_app, "STATE_PATH", state_path):
                state = desktop_app.WindowState()
                state.update(width=1280, height=760, x=100, y=80, on_top=True)
                saved = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["width"], 1280)
        self.assertEqual(saved["height"], 760)
        self.assertEqual(saved["x"], 100)
        self.assertTrue(saved["on_top"])

    def test_desktop_api_controls_window_without_browser_navigation(self):
        project_runtime = Path(__file__).resolve().parents[1] / "runtime"
        project_runtime.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="desktop-api-", dir=project_runtime) as folder:
            with mock.patch.object(desktop_app, "STATE_PATH", Path(folder) / "state.json"):
                state = desktop_app.WindowState()
                api = desktop_app.DesktopApi(state)
                window = FakeWindow()
                api.bind(window)
                result = api.set_always_on_top(True)
                api.reload()
        self.assertTrue(result["ok"])
        self.assertTrue(window.on_top)
        self.assertIn("desktop=1", window.loaded_url)


if __name__ == "__main__":
    unittest.main()
