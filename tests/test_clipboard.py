import copy
import ctypes
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from codepilot import ai_client
from codepilot.config import ConfigStore, DEFAULT, validate
from codepilot.controller import Controller
from codepilot.desktop import Desktop, KeyboardService
from codepilot.web_server import WebApp
from test_workflow import FakeCredentials, FakeDesktop, FakeOCR, until


def temporary_directory():
    root = Path(__file__).resolve().parents[1] / ".qa"
    root.mkdir(exist_ok=True)
    return tempfile.TemporaryDirectory(dir=root)


class ClipboardWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_directory()
        self.config = ConfigStore(Path(self.temp.name) / "config.json")
        self.desktop = FakeDesktop()
        self.ocr = Mock(spec=FakeOCR)
        self.generated = []
        self.controller = Controller(self.config, FakeCredentials(), self.desktop, self.ocr,
            lambda p, k, q, s: self.generated.append((p, k, q, s)) or "return a + b;\n")

    def tearDown(self):
        self.controller.close()
        self.temp.cleanup()

    def test_clipboard_skips_ocr_preserves_text_and_uses_existing_typing(self):
        question = "函数接口：int sum(int a, int b);\r\n  样例：1 2 → 3\t🙂\r末行\n"
        self.desktop.copied = question
        self.desktop.read_clipboard = Mock(wraps=self.desktop.read_clipboard)
        self.controller.use_clipboard("cpp", 42)
        until(lambda: self.controller.phase == "ready")
        status = self.controller.snapshot()
        expected = question.replace("\r\n", "\n").replace("\r", "\n")
        self.assertEqual(status["source"], "clipboard")
        self.assertIn("F2 · 剪贴板", status["profile"])
        self.assertEqual(status["question"], expected)
        self.assertEqual(self.generated[0][2:], (expected, "clipboard"))
        self.ocr.capture.assert_not_called()
        self.ocr.recognize.assert_not_called()
        self.assertEqual(self.desktop.copied, status["code"])
        for _ in status["code"]:
            self.assertTrue(self.controller.enqueue_letter(42))
        until(lambda: self.controller.phase == "done")
        self.assertEqual("".join(self.desktop.sent), status["code"])
        self.desktop.read_clipboard.assert_called_once_with()
        self.assertEqual(len(self.generated), 1)

    def block_reader(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        def read():
            started.set()
            release.wait(3)
            finished.set()
            return "旧题目"
        self.desktop.read_clipboard = read
        self.controller.use_clipboard("cpp", 42)
        self.assertTrue(started.wait(1))
        self.addCleanup(release.set)
        return release, finished

    def test_stop_while_reading_prevents_generation_and_clipboard_overwrite(self):
        self.desktop.copied = "手动复制的题目"
        release, finished = self.block_reader()
        self.assertEqual(self.controller.snapshot()["phase"], "reading_clipboard")
        self.controller.stop_round()
        release.set()
        self.assertTrue(finished.wait(1))
        self.assertEqual(self.generated, [])
        self.assertEqual(self.controller.phase, "idle")
        self.assertEqual(self.desktop.copied, "手动复制的题目")

    def test_pause_during_reading_remains_paused_when_ready(self):
        release, _ = self.block_reader()
        self.controller.toggle_pause()
        release.set()
        until(lambda: self.controller.phase == "ready")
        self.assertTrue(self.controller.paused)
        self.assertFalse(self.controller.enqueue_letter(42))
        self.controller.toggle_pause()
        self.assertTrue(self.controller.enqueue_letter(42))

    def test_screenshot_replaces_pending_clipboard_round(self):
        release, finished = self.block_reader()
        self.ocr.capture.return_value = "image"
        self.ocr.recognize.return_value = "截图题目"
        self.controller.capture("cpp", 42)
        until(lambda: self.controller.phase == "ready")
        release.set()
        self.assertTrue(finished.wait(1))
        self.assertEqual(self.controller.question, "截图题目")
        self.assertEqual(len(self.generated), 1)
        self.assertEqual(self.generated[0][3], "screenshot")

    def test_clipboard_replaces_pending_screenshot_generation(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        def generate(p, k, q, source):
            if source == "screenshot":
                started.set()
                release.wait(3)
                finished.set()
                return "old"
            return "new"
        self.controller.generator = generate
        self.ocr.capture.return_value = "image"
        self.ocr.recognize.return_value = "截图题目"
        self.controller.capture("cpp", 42)
        self.assertTrue(started.wait(1))
        try:
            self.desktop.copied = "手动复制的题目"
            self.controller.use_clipboard("cpp", 42)
            until(lambda: self.controller.code == "new")
        finally:
            release.set()
        self.assertTrue(finished.wait(1))
        self.assertEqual(self.desktop.copied, "new")
        self.assertEqual(self.controller.question, "手动复制的题目")

    def test_clipboard_and_profile_are_snapshotted_for_generation(self):
        started, release = threading.Event(), threading.Event()
        def generate(p, k, q, source):
            self.generated.append((p, k, q, source))
            started.set()
            release.wait(3)
            return "code"
        self.controller.generator = generate
        self.desktop.copied = "原题目\n  原格式"
        self.controller.use_clipboard("cpp", 42)
        self.assertTrue(started.wait(1))
        try:
            self.desktop.copied = "后来复制的内容"
            value = self.config.get()
            value["profiles"][0]["language"] = "Python 3"
            self.config.save(value)
        finally:
            release.set()
        until(lambda: self.controller.phase == "ready")
        self.assertEqual(self.generated[0][0]["language"], "C++17")
        self.assertEqual(self.generated[0][2], "原题目\n  原格式")

    def test_empty_or_unreadable_clipboard_never_arms_typing(self):
        for result in ("", " \r\n\t", RuntimeError("剪贴板正被占用")):
            with self.subTest(result=result):
                self.desktop.read_clipboard = Mock(return_value=result,
                    side_effect=result if isinstance(result, Exception) else None)
                self.controller.use_clipboard("cpp", 42)
                until(lambda: self.controller.phase == "error")
                self.assertTrue(self.controller.error)
                self.assertEqual(self.generated, [])
                self.assertFalse(self.controller.enqueue_letter(42))

    def test_closed_controller_does_not_start_clipboard_read(self):
        self.desktop.read_clipboard = Mock()
        self.controller.close()
        token = self.controller.round_id
        self.controller.use_clipboard("cpp", 42)
        self.assertEqual(self.controller.round_id, token)
        self.desktop.read_clipboard.assert_not_called()


class ClipboardConfigTests(unittest.TestCase):
    def legacy_store(self, folder, hotkeys=("F1",)):
        value = copy.deepcopy(DEFAULT)
        original = value["profiles"][0]
        del original["clipboard_hotkey"]
        value["profiles"] = [dict(original, id=f"p{i}", hotkey=key) for i, key in enumerate(hotkeys)]
        path = Path(folder) / "config.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return ConfigStore(path)

    def test_old_config_gets_default_f2_and_web_save_preserves_it(self):
        with temporary_directory() as folder:
            store = self.legacy_store(folder)
            self.assertEqual(store.get()["profiles"][0]["clipboard_hotkey"], "F2")
            app = WebApp(store, FakeCredentials(), Mock(), FakeOCR(), Path("web"), lambda: None)
            app.keyboard = Mock()
            value = app.public_config()
            for hotkey in ("F6", ""):
                value["profiles"][0]["clipboard_hotkey"] = hotkey
                result = app.save({"config": value, "keys": {}})
                self.assertEqual(result["profiles"][0]["clipboard_hotkey"], hotkey)
                self.assertEqual(ConfigStore(store.path).get()["profiles"][0]["clipboard_hotkey"], hotkey)
                app.keyboard.apply.assert_called_with(store.get())

    def test_migration_assigns_unique_keys_without_replacing_existing_bindings(self):
        with temporary_directory() as folder:
            store = self.legacy_store(folder, ("F2", "F4"))
            profiles = store.get()["profiles"]
            self.assertEqual([p["hotkey"] for p in profiles], ["F2", "F4"])
            self.assertEqual([p["clipboard_hotkey"] for p in profiles], ["F3", "F5"])

    def test_migration_preserves_explicit_disabled_and_custom_clipboard_keys(self):
        with temporary_directory() as folder:
            path = Path(folder) / "config.json"
            for hotkey in ("", "Ctrl+Alt+2"):
                value = copy.deepcopy(DEFAULT)
                value["profiles"][0]["clipboard_hotkey"] = hotkey
                path.write_text(json.dumps(value), encoding="utf-8")
                self.assertEqual(ConfigStore(path).get()["profiles"][0]["clipboard_hotkey"], hotkey)

    def test_migration_skips_reserved_f12_and_preserves_existing_hotkeys(self):
        with temporary_directory() as folder:
            hotkeys = tuple(f"F{i}" for i in range(1, 6))
            value = self.legacy_store(folder, hotkeys).get()
            self.assertEqual([p["hotkey"] for p in value["profiles"]], list(hotkeys))
            self.assertEqual([p["clipboard_hotkey"] for p in value["profiles"]],
                             ["F6", "F7", "F10", "F11", "F13"])
            self.assertEqual((value["pause_hotkey"], value["stop_hotkey"]), ("F8", "F9"))
            validate(value)

    def test_migration_disables_clipboard_when_function_keys_are_exhausted(self):
        with temporary_directory() as folder:
            hotkeys = tuple(f"F{i}" for i in range(1, 25) if i not in (8, 9, 12))
            value = self.legacy_store(folder, hotkeys).get()
            self.assertTrue(all(p["clipboard_hotkey"] == "" for p in value["profiles"]))
            validate(value)

    def test_conflicts_include_screenshot_clipboard_pause_and_stop(self):
        for hotkey in ("f1", "F8", "F9"):
            with self.subTest(hotkey=hotkey):
                value = copy.deepcopy(DEFAULT)
                value["profiles"][0]["clipboard_hotkey"] = hotkey
                with self.assertRaises(ValueError):
                    validate(value)
        value = copy.deepcopy(DEFAULT)
        value["profiles"].append(dict(value["profiles"][0], id="python", hotkey="F3"))
        with self.assertRaises(ValueError):
            validate(value)


class ClipboardDesktopTests(unittest.TestCase):
    def setUp(self):
        self.desktop = object.__new__(Desktop)
        self.buffer = ctypes.create_unicode_buffer("题目\r\n  样例🙂\t")
        self.desktop.user = SimpleNamespace(OpenClipboard=Mock(return_value=True), CloseClipboard=Mock(),
            IsClipboardFormatAvailable=Mock(return_value=True), GetClipboardData=Mock(return_value=123),
            EmptyClipboard=Mock(), SetClipboardData=Mock(), SendInput=Mock())
        self.desktop.kernel = SimpleNamespace(GlobalLock=Mock(return_value=ctypes.addressof(self.buffer)),
            GlobalUnlock=Mock(), GlobalFree=Mock())

    def test_reads_unicode_without_typing_or_modifying_clipboard(self):
        self.assertEqual(self.desktop.read_clipboard(), "题目\r\n  样例🙂\t")
        self.desktop.user.GetClipboardData.assert_called_once_with(13)
        self.desktop.kernel.GlobalUnlock.assert_called_once_with(123)
        self.desktop.user.CloseClipboard.assert_called_once_with()
        for operation in (self.desktop.user.SendInput, self.desktop.user.EmptyClipboard,
                          self.desktop.user.SetClipboardData, self.desktop.kernel.GlobalFree):
            operation.assert_not_called()

    def test_image_only_clipboard_reports_copy_instruction_and_closes(self):
        self.desktop.user.IsClipboardFormatAvailable.return_value = False
        with self.assertRaisesRegex(ValueError, "手动选中题目并复制"):
            self.desktop.read_clipboard()
        self.desktop.user.CloseClipboard.assert_called_once_with()
        self.desktop.kernel.GlobalLock.assert_not_called()

    def test_clipboard_open_retries_and_busy_failure_does_not_close_unopened_clipboard(self):
        with patch("codepilot.desktop.time.sleep"):
            self.desktop.user.OpenClipboard.side_effect = [False, False, True]
            self.assertEqual(self.desktop.read_clipboard(), self.buffer.value)
            self.desktop.user.OpenClipboard.reset_mock(side_effect=True)
            self.desktop.user.CloseClipboard.reset_mock()
            self.desktop.user.OpenClipboard.return_value = False
            with self.assertRaisesRegex(RuntimeError, "剪贴板正被占用"):
                self.desktop.read_clipboard()
            self.assertEqual(self.desktop.user.OpenClipboard.call_count, 10)
            self.desktop.user.CloseClipboard.assert_not_called()

    def test_failed_lock_still_closes_clipboard(self):
        self.desktop.kernel.GlobalLock.return_value = None
        with self.assertRaises(OSError):
            self.desktop.read_clipboard()
        self.desktop.user.CloseClipboard.assert_called_once_with()
        self.desktop.kernel.GlobalUnlock.assert_not_called()


class ClipboardHotkeyTests(unittest.TestCase):
    def test_registers_and_dispatches_clipboard_with_profile_snapshot(self):
        keyboard = object.__new__(KeyboardService)
        keyboard.desktop = SimpleNamespace(user=SimpleNamespace(RegisterHotKey=Mock(return_value=True)),
                                           foreground=lambda: 42)
        keyboard.controller = Mock()
        keyboard.actions = {}
        value = copy.deepcopy(DEFAULT)
        keyboard._register(value)
        self.assertEqual(len(keyboard.actions), 4)
        value["profiles"][0]["language"] = "Python 3"
        for number, (action, profile) in keyboard.actions.items():
            keyboard._dispatch_hotkey(number)
            if action == "clipboard":
                self.assertEqual(profile["language"], "C++17")
                keyboard.controller.use_clipboard.assert_called_once_with("cpp", 42, profile=profile)
        keyboard.controller.capture.assert_called_once()
        keyboard.controller.toggle_pause.assert_called_once()
        keyboard.controller.stop_round.assert_called_once()

    def test_disabled_clipboard_key_does_not_register(self):
        keyboard = object.__new__(KeyboardService)
        keyboard.desktop = SimpleNamespace(user=SimpleNamespace(RegisterHotKey=Mock(return_value=True)))
        keyboard.actions = {}
        value = copy.deepcopy(DEFAULT)
        value["profiles"][0]["clipboard_hotkey"] = ""
        keyboard._register(value)
        self.assertEqual(len(keyboard.actions), 3)
        self.assertNotIn("clipboard", [action for action, _ in keyboard.actions.values()])


class ClipboardPromptTests(unittest.TestCase):
    def test_manual_copy_prompt_preserves_original_text_without_ocr_correction(self):
        response = {"choices": [{"finish_reason": "stop", "message": {"content": "int sum();"}}]}
        question = "函数接口：int sum(int a, int b);\n约束：a < b"
        with patch("codepilot.ai_client.request", return_value=response) as request:
            ai_client.generate(DEFAULT["profiles"][0], "key", question, "clipboard")
            payload = request.call_args.args[3]
            self.assertEqual(payload["messages"][1]["content"], question)
            self.assertIn("手动复制", payload["messages"][0]["content"])
            self.assertNotIn("OCR", payload["messages"][0]["content"])
            ai_client.generate(DEFAULT["profiles"][0], "key", question)
            self.assertIn("OCR", request.call_args.args[3]["messages"][0]["content"])


if __name__ == "__main__":
    unittest.main()
