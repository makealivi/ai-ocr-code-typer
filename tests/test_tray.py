import ctypes as c
import json
from ctypes import wintypes as w
from pathlib import Path
import queue
import tempfile
import threading
import unittest
import uuid
from unittest.mock import Mock

from codepilot.config import ConfigStore, validate
from codepilot.controller import Controller
from codepilot.tray import GUID, NOTIFYICONDATA, TrayService, WM_TRAY
from test_workflow import FakeCredentials, FakeDesktop, FakeOCR, until


ROOT = Path(__file__).resolve().parents[1]


class TrayWorkflowTests(unittest.TestCase):
    def setUp(self):
        (ROOT / ".qa").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / ".qa")
        self.desktop = FakeDesktop()
        self.controller = Controller(ConfigStore(Path(self.temp.name) / "config.json"),
                                     FakeCredentials(), self.desktop, FakeOCR(), lambda *args: "abc")

    def tearDown(self):
        self.controller.close()
        self.temp.cleanup()

    def latest(self):
        state = None
        while True:
            try:
                state = self.controller.tray_events.get_nowait()
            except queue.Empty:
                return state

    def ready(self, source="screenshot"):
        if source == "clipboard":
            self.desktop.copied = "question"
        self.controller.start_round("cpp", 42, source)
        until(lambda: self.controller.phase == "ready")
        return self.latest()

    def test_both_sources_publish_one_pending_result_and_stop_clears_it(self):
        for source in ("screenshot", "clipboard"):
            state = self.ready(source)
            self.assertEqual((state["phase"], state["pending"], state["paused"]), ("ready", True, False))
            self.controller.stop_round()
            state = self.latest()
            self.assertEqual((state["phase"], state["pending"]), ("idle", False))

    def test_first_success_clears_notice_only_after_send_returns(self):
        self.ready()
        entered, release = threading.Event(), threading.Event()
        def send(char):
            entered.set()
            if not release.wait(2):
                raise TimeoutError("character was not released")
        self.desktop.send_char = send
        self.controller.enqueue_letter(42)
        try:
            self.assertTrue(entered.wait(1))
            self.assertTrue(self.controller.notice_pending)
        finally:
            release.set()
        until(lambda: self.controller.index == 1)
        self.assertFalse(self.latest()["pending"])
        self.controller.toggle_pause()
        self.controller.toggle_pause()
        self.assertFalse(self.latest()["pending"])

    def test_send_failure_keeps_notice_and_resume_allows_first_success(self):
        self.ready()
        self.desktop.send_char = Mock(side_effect=RuntimeError("send failed"))
        self.controller.enqueue_letter(42)
        until(lambda: self.controller.paused)
        state = self.latest()
        self.assertTrue(state["pending"])
        self.assertTrue(state["paused"])
        self.desktop.send_char = Mock()
        self.controller.toggle_pause()
        self.controller.enqueue_letter(42)
        until(lambda: self.controller.index == 1)
        self.assertFalse(self.latest()["pending"])

    def test_acknowledgement_is_scoped_to_round_and_survives_pause(self):
        old = self.ready()["round"]
        self.ready()
        self.controller.acknowledge_notice(old)
        self.assertTrue(self.controller.notice_pending)
        self.controller.acknowledge_notice(self.controller.round_id)
        self.controller.toggle_pause()
        self.controller.toggle_pause()
        self.assertFalse(self.latest()["pending"])

    def test_paused_generation_keeps_notice_and_waits_for_resume(self):
        entered, release = threading.Event(), threading.Event()
        def generate(*args):
            entered.set()
            if not release.wait(2):
                raise TimeoutError("generation was not released")
            return "abc"
        self.controller.generator = generate
        self.controller.capture("cpp", 42)
        try:
            self.assertTrue(entered.wait(1))
            self.controller.toggle_pause()
        finally:
            release.set()
        until(lambda: self.controller.phase == "ready")
        state = self.latest()
        self.assertTrue(state["pending"] and state["paused"])
        self.controller.toggle_pause()
        self.assertFalse(self.latest()["paused"])

    def test_new_round_clears_notice_before_generation_finishes(self):
        self.ready()
        entered, release = threading.Event(), threading.Event()
        def generate(*args):
            entered.set()
            if not release.wait(2):
                raise TimeoutError("generation was not released")
            return "new"
        self.controller.generator = generate
        self.controller.capture("cpp", 42)
        try:
            self.assertTrue(entered.wait(1))
            self.assertFalse(self.latest()["pending"])
            self.controller.stop_round()
        finally:
            release.set()
        self.assertFalse(self.controller.notice_pending)

    def test_old_success_cannot_clear_the_new_rounds_notice(self):
        self.ready()
        entered, release = threading.Event(), threading.Event()
        def send(char):
            entered.set()
            if not release.wait(2):
                raise TimeoutError("character was not released")
        self.desktop.send_char = send
        self.controller.enqueue_letter(42)
        try:
            self.assertTrue(entered.wait(1))
            self.ready()
        finally:
            release.set()
        until(lambda: self.controller._inflight is None)
        self.assertTrue(self.controller.notice_pending)

    def test_error_has_no_ready_notice_and_close_clears_pending(self):
        self.controller.generator = Mock(side_effect=RuntimeError("generation failed"))
        self.controller.capture("cpp", 42)
        until(lambda: self.controller.phase == "error")
        self.assertFalse(self.latest()["pending"])
        self.controller.generator = lambda *args: "abc"
        self.ready()
        self.controller.close()
        state = self.latest()
        self.assertTrue(state["closed"])
        self.assertFalse(state["pending"])

    def test_tray_is_opt_in_and_legacy_configuration_defaults_to_disabled(self):
        self.assertFalse(self.controller.tray_enabled)
        value = self.controller.config.get()
        del value["show_tray_icon"]
        path = Path(self.temp.name) / "legacy.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        self.assertFalse(ConfigStore(path).get()["show_tray_icon"])

    def test_tray_setting_requires_a_boolean(self):
        value = self.controller.config.get()
        for invalid in ("false", 1, None):
            value["show_tray_icon"] = invalid
            with self.assertRaises(ValueError):
                validate(value)


class NOTIFYICONIDENTIFIER(c.Structure):
    _fields_ = [("cbSize", w.DWORD), ("hWnd", w.HWND), ("uID", w.UINT), ("guidItem", GUID)]


class NativeTrayTests(unittest.TestCase):
    setUp = TrayWorkflowTests.setUp
    tearDown = TrayWorkflowTests.tearDown

    def test_native_icon_blinks_pauses_acknowledges_and_is_removed(self):
        tray = TrayService(self.controller, ROOT / "assets" / "tray", uuid.uuid4().hex, Mock(), Mock())
        tray.start()
        try:
            self.assertEqual(c.sizeof(NOTIFYICONDATA), 976 if c.sizeof(c.c_void_p) == 8 else 956)
            identifier = NOTIFYICONIDENTIFIER(cbSize=c.sizeof(NOTIFYICONIDENTIFIER), guidItem=tray.guid)
            tray.shell.Shell_NotifyIconGetRect.argtypes = [c.POINTER(NOTIFYICONIDENTIFIER), c.POINTER(w.RECT)]
            tray.shell.Shell_NotifyIconGetRect.restype = c.c_long
            rect = w.RECT()
            self.assertIsNone(tray.nid)
            self.assertNotEqual(tray.shell.Shell_NotifyIconGetRect(c.byref(identifier), c.byref(rect)), 0)
            self.controller.set_tray_enabled(True)
            until(lambda: tray.shell.Shell_NotifyIconGetRect(c.byref(identifier), c.byref(rect)) == 0)
            self.assertEqual(tray.shell.Shell_NotifyIconGetRect(c.byref(identifier), c.byref(rect)), 0)
            self.controller.capture("cpp", 42)
            until(lambda: tray.blinking)
            self.assertEqual(tray.nid.hIcon, tray.icons["normal"])
            until(lambda: tray.nid.hIcon == tray.icons["dim"])
            self.controller.set_tray_enabled(False)
            until(lambda: tray.nid is None)
            self.assertFalse(tray.blinking)
            self.assertTrue(self.controller.notice_pending)
            self.assertNotEqual(tray.shell.Shell_NotifyIconGetRect(c.byref(identifier), c.byref(rect)), 0)
            self.controller.set_tray_enabled(True)
            until(lambda: tray.nid is not None and tray.blinking)
            self.controller.toggle_pause()
            until(lambda: not tray.blinking)
            self.assertTrue(tray.state["pending"])
            self.assertEqual(tray.nid.hIcon, tray.icons["normal"])
            self.controller.toggle_pause()
            until(lambda: tray.blinking)
            tray.user.PostMessageW(tray.hwnd, WM_TRAY, 0, 0x400 | (1 << 16))
            until(lambda: not self.controller.notice_pending)
            until(lambda: tray.nid.hIcon == tray.icons["normal"])
            self.assertFalse(tray.blinking)
            self.assertIsNone(tray.error)
        finally:
            tray.stop()
        self.assertFalse(tray.thread.is_alive())
        self.assertNotEqual(tray.shell.Shell_NotifyIconGetRect(c.byref(identifier), c.byref(rect)), 0)


if __name__ == "__main__":
    unittest.main()
