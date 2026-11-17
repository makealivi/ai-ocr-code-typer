import copy
import ctypes
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from codepilot import ai_client
from codepilot.config import ConfigStore, DEFAULT, validate
from codepilot.controller import Controller
from codepilot.desktop import INPUT, KBDLLHOOKSTRUCT, KeyboardService, KeyRouter
from codepilot.web_server import WebApp, create_server


def until(predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("Timed out waiting for state")


class FakeCredentials:
    def __init__(self):
        self.values = {"cpp": "private-test-key"}

    def get(self, profile):
        return self.values.get(profile, "")

    def set(self, profile, key):
        self.values[profile] = key

    def delete(self, profile):
        self.values.pop(profile, None)


class FakeDesktop:
    def __init__(self):
        self.hwnd = 42
        self.sent = []
        self.copied = ""

    def foreground(self):
        return self.hwnd

    def clipboard(self, text):
        self.copied = text

    def read_clipboard(self):
        return self.copied

    def send_char(self, text):
        self.sent.append(text)


class FakeOCR:
    def capture(self):
        return "image"

    def recognize(self, image):
        return "输出两数之和\n输入 1 2\n输出 3"


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path(__file__).resolve().parents[1] / ".qa"
        temp_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temp_root)
        self.config = ConfigStore(Path(self.temp.name) / "config.json")
        self.keys = FakeCredentials()
        self.desktop = FakeDesktop()
        self.controller = Controller(self.config, self.keys, self.desktop, FakeOCR(),
                                     lambda *args: "A\n  中\t🙂")

    def tearDown(self):
        self.controller.close()
        self.temp.cleanup()

    def ready(self):
        self.controller.capture("cpp", 42)
        until(lambda: self.controller.snapshot()["phase"] == "ready")

    def test_complete_round_preserves_all_whitespace_and_unicode(self):
        self.ready()
        code = self.controller.code
        self.assertEqual(self.desktop.copied, code)
        for _ in code:
            self.assertTrue(self.controller.enqueue_letter(42))
        until(lambda: self.controller.phase == "done")
        self.assertEqual("".join(self.desktop.sent), code)
        self.assertFalse(self.controller.enqueue_letter(42))

    def block_next_character(self, error=None):
        started, release = threading.Event(), threading.Event()
        original_send = self.desktop.send_char
        def send(text):
            if not started.is_set():
                started.set()
                if not release.wait(3):
                    raise TimeoutError("Test did not release character output")
                if error is not None:
                    raise error
            original_send(text)
        self.desktop.send_char = send
        return started, release

    def test_fast_physical_keys_are_swallowed_while_character_output_is_busy(self):
        code = "array.data_[i] = array.data_[i - 1];\n" * 40
        self.controller.generator = lambda *args: code
        self.ready()
        started, release = self.block_next_character()
        self.desktop.user = SimpleNamespace(CallNextHookEx=lambda *args: 0)
        self.desktop.is_config_window = lambda hwnd: False
        keyboard = object.__new__(KeyboardService)
        keyboard.desktop = self.desktop
        keyboard.controller = self.controller
        keyboard.router = KeyRouter()
        keyboard.modifier_keys = set()
        def press(vk):
            event = KBDLLHOOKSTRUCT(vkCode=vk)
            return (keyboard._hook(0, 0x100, ctypes.addressof(event)),
                    keyboard._hook(0, 0x101, ctypes.addressof(event)))
        self.assertEqual(press(65), (1, 1))
        results, finished = [], threading.Event()
        def burst():
            for i in range(len(code) + 100):
                results.append(press(65 + i % 26))
            finished.set()
        thread = threading.Thread(target=burst, daemon=True)
        try:
            self.assertTrue(started.wait(1))
            thread.start()
            self.assertTrue(finished.wait(0.5), "Keyboard hook waited for character output")
            self.assertEqual(results, [(1, 1)] * (len(code) + 100))
        finally:
            release.set()
            if thread.ident is not None:
                thread.join(2)
        until(lambda: self.controller.phase == "done")
        self.assertEqual("".join(self.desktop.sent), code)

    def test_pause_during_send_discards_queue_and_resume_does_not_repeat_character(self):
        self.ready()
        code = self.controller.code
        started, release = self.block_next_character()
        self.controller.enqueue_letter(42)
        try:
            self.assertTrue(started.wait(1))
            self.controller.enqueue_letter(42)
            self.controller.enqueue_letter(42)
            self.controller.toggle_pause()
            self.assertFalse(self.controller.enqueue_letter(42))
        finally:
            release.set()
        until(lambda: self.controller.index == 1)
        self.assertTrue(self.controller.paused)
        self.assertEqual(self.desktop.sent, [code[0]])
        self.controller.toggle_pause()
        for _ in code[1:]:
            self.controller.enqueue_letter(42)
        until(lambda: self.controller.phase == "done")
        self.assertEqual("".join(self.desktop.sent), code)

    def test_repeated_pause_resume_before_send_finishes_preserves_next_position(self):
        self.ready()
        code = self.controller.code
        started, release = self.block_next_character()
        self.controller.enqueue_letter(42)
        try:
            self.assertTrue(started.wait(1))
            for _ in range(2):
                self.controller.toggle_pause()
                self.controller.toggle_pause()
                self.controller.enqueue_letter(42)
            for _ in code[2:]:
                self.controller.enqueue_letter(42)
        finally:
            release.set()
        until(lambda: self.controller.phase == "done")
        self.assertEqual("".join(self.desktop.sent), code)

    def test_stop_during_send_discards_remaining_characters(self):
        self.ready()
        code = self.controller.code
        started, release = self.block_next_character()
        self.controller.enqueue_letter(42)
        try:
            self.assertTrue(started.wait(1))
            for _ in code[1:]:
                self.controller.enqueue_letter(42)
            self.controller.stop_round()
            self.assertFalse(self.controller.enqueue_letter(42))
        finally:
            release.set()
        until(lambda: self.controller._inflight is None)
        self.assertEqual(self.desktop.sent, [code[0]])
        self.assertEqual(self.controller.phase, "idle")

    def test_old_send_completion_cannot_advance_new_round(self):
        self.ready()
        first_char = self.controller.code[0]
        started, release = self.block_next_character()
        self.controller.enqueue_letter(42)
        try:
            self.assertTrue(started.wait(1))
            self.controller.stop_round()
            self.controller.generator = lambda *args: "new"
            self.ready()
        finally:
            release.set()
        until(lambda: self.controller._inflight is None)
        self.assertEqual(self.controller.index, 0)
        self.assertEqual(self.controller.phase, "ready")
        for _ in "new":
            self.controller.enqueue_letter(42)
        until(lambda: self.controller.phase == "done")
        self.assertEqual("".join(self.desktop.sent), first_char + "new")

    def test_send_failure_pauses_and_retry_starts_at_unsent_character(self):
        self.ready()
        code = self.controller.code
        started, release = self.block_next_character(RuntimeError("send failed"))
        self.controller.enqueue_letter(42)
        try:
            self.assertTrue(started.wait(1))
            for _ in code[1:]:
                self.controller.enqueue_letter(42)
        finally:
            release.set()
        until(lambda: self.controller.paused)
        self.assertEqual(self.controller.index, 0)
        self.assertEqual(self.desktop.sent, [])
        self.assertEqual(self.controller.error, "send failed")
        self.controller.toggle_pause()
        for _ in code:
            self.controller.enqueue_letter(42)
        until(lambda: self.controller.phase == "done")
        self.assertEqual("".join(self.desktop.sent), code)

    def test_old_send_failure_cannot_pause_new_round(self):
        self.ready()
        started, release = self.block_next_character(RuntimeError("old send failed"))
        self.controller.enqueue_letter(42)
        try:
            self.assertTrue(started.wait(1))
            self.controller.stop_round()
            self.controller.generator = lambda *args: "new"
            self.ready()
            for _ in "new":
                self.controller.enqueue_letter(42)
        finally:
            release.set()
        until(lambda: self.controller.phase == "done")
        self.assertFalse(self.controller.paused)
        self.assertEqual(self.controller.error, "")
        self.assertEqual("".join(self.desktop.sent), "new")

    def test_pause_preserves_position_and_stop_ends_input(self):
        self.ready()
        self.controller.enqueue_letter(42)
        until(lambda: self.controller.index == 1)
        self.controller.toggle_pause()
        self.assertFalse(self.controller.enqueue_letter(42))
        self.controller.toggle_pause()
        self.controller.enqueue_letter(42)
        until(lambda: self.controller.index == 2)
        copied = self.desktop.copied
        self.controller.stop_round()
        self.assertFalse(self.controller.enqueue_letter(42))
        self.assertEqual(self.desktop.copied, copied)

    def test_pending_letters_are_discarded_on_stop(self):
        self.ready()
        with self.controller.lock:
            self.controller.enqueue_letter(42)
            self.controller.enqueue_letter(42)
            self.controller.stop_round()
        time.sleep(0.03)
        self.assertEqual(self.desktop.sent, [])

    def test_switching_foreground_pauses_without_advancing(self):
        self.ready()
        self.desktop.hwnd = 99
        self.controller.enqueue_letter(42)
        until(lambda: self.controller.paused)
        self.assertEqual(self.controller.index, 0)
        self.assertEqual(self.desktop.sent, [])

    def test_older_generation_cannot_replace_new_round(self):
        first_started, release = threading.Event(), threading.Event()
        count = 0
        def generate(*args):
            nonlocal count
            count += 1
            if count == 1:
                first_started.set()
                release.wait(2)
                return "old"
            return "new"
        self.controller.generator = generate
        self.controller.capture("cpp", 42)
        self.assertTrue(first_started.wait(2))
        self.controller.capture("cpp", 42)
        until(lambda: self.controller.code == "new")
        release.set()
        time.sleep(0.03)
        self.assertEqual(self.controller.code, "new")
        self.assertEqual(self.desktop.copied, "new")

    def test_stopping_while_generating_does_not_arm_later(self):
        started, release = threading.Event(), threading.Event()
        def generate(*args):
            started.set(); release.wait(2); return "late"
        self.controller.generator = generate
        self.controller.capture("cpp", 42)
        self.assertTrue(started.wait(2))
        self.controller.stop_round(); release.set()
        time.sleep(0.03)
        self.assertEqual(self.controller.phase, "idle")
        self.assertEqual(self.desktop.copied, "")

    def test_pause_during_generation_remains_paused_when_ready(self):
        started, release = threading.Event(), threading.Event()
        def generate(*args):
            started.set(); release.wait(2); return "code"
        self.controller.generator = generate
        self.controller.capture("cpp", 42)
        self.assertTrue(started.wait(2))
        self.controller.toggle_pause(); release.set()
        until(lambda: self.controller.phase == "ready")
        self.assertFalse(self.controller.enqueue_letter(42))

    def test_api_error_is_visible_without_arming_input(self):
        def generate(*args):
            raise ai_client.ApiError("API Key 无效")
        self.controller.generator = generate
        self.controller.capture("cpp", 42)
        until(lambda: self.controller.phase == "error")
        self.assertEqual(self.controller.error, "API Key 无效")
        self.assertFalse(self.controller.enqueue_letter(42))

    def test_profile_language_and_endpoint_are_snapshotted(self):
        captured = []
        self.controller.generator = lambda p,k,q,s: (captured.append((p,k,q,s)) or "code")
        value = self.config.get()
        value["profiles"][0].update(language="Python 3", model="my-model", base_url="http://localhost:8000")
        self.config.save(value)
        self.ready()
        self.assertEqual(captured[0][0]["language"], "Python 3")
        self.assertEqual(captured[0][0]["model"], "my-model")

    def test_config_rejects_duplicate_hotkeys(self):
        value = copy.deepcopy(DEFAULT)
        value["profiles"][0]["hotkey"] = "f8"
        with self.assertRaises(ValueError):
            validate(value)


class RouterTests(unittest.TestCase):
    def test_requested_punctuation_is_swallowed_and_advances_once_per_press(self):
        router = KeyRouter()
        emitted = []
        def accept():
            emitted.append(1)
            return True
        for vk in (0xDB, 0xDD, 0xBA, 0xDE, 0xBC, 0xBE, 0xBF):
            self.assertTrue(router.route(vk, False, False, set(), accept))
            self.assertTrue(router.route(vk, False, False, set(), accept))
            self.assertTrue(router.route(vk, True, False, set(), accept))
        self.assertEqual(len(emitted), 7)

    def test_punctuation_passes_through_while_paused_or_using_ctrl(self):
        router = KeyRouter()
        self.assertFalse(router.route(0xDB, False, False, set(), lambda: False))
        router.route(0xDB, True, False, set(), lambda: False)
        self.assertFalse(router.route(0xDB, False, False, {"ctrl"}, lambda: True))

    def test_uppercase_lowercase_and_keyup_are_suppressed_once(self):
        router = KeyRouter()
        emitted = []
        def accept():
            emitted.append(1); return True
        self.assertTrue(router.route(65, False, False, set(), accept))
        self.assertTrue(router.route(65, False, False, set(), accept))
        self.assertTrue(router.route(65, True, False, set(), accept))
        self.assertTrue(router.route(65, False, False, {"shift"}, accept))
        self.assertEqual(len(emitted), 2)

    def test_injected_characters_and_ctrl_shortcuts_pass_through(self):
        router = KeyRouter()
        def forbidden():
            self.fail("Should not emit a code character")
        self.assertFalse(router.route(65, False, True, set(), forbidden))
        self.assertFalse(router.route(67, False, False, {"ctrl"}, forbidden))

    def test_pending_keyup_stays_suppressed_after_pause(self):
        router = KeyRouter()
        router.route(65, False, False, set(), lambda: True)
        self.assertTrue(router.route(65, True, False, set(), lambda: False))
        self.assertFalse(router.route(66, False, False, set(), lambda: False))

    def test_win64_input_structure_matches_windows_abi(self):
        self.assertEqual(ctypes.sizeof(INPUT), 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)


class HTTPTests(unittest.TestCase):
    def setUp(self):
        WorkflowTests.setUp(self)
        self.app = WebApp(self.config,self.keys,self.controller,FakeOCR(),Path(__file__).resolve().parents[1]/"web",lambda:None)
        self.server = create_server(self.app)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown(); self.server.server_close()
        WorkflowTests.tearDown(self)

    def req(self, path, value=None):
        data = None if value is None else json.dumps(value).encode()
        req = urllib.request.Request(self.url+path,data=data,headers={"X-Session-Token":self.app.token,"Content-Type":"application/json"})
        with urllib.request.urlopen(req) as response:
            return json.load(response)

    def test_config_never_exposes_keys_or_writes_them_to_json(self):
        config = self.req("/api/config")
        self.assertTrue(config["profiles"][0]["has_key"])
        self.assertNotIn("private-test-key", json.dumps(config))
        self.req("/api/config", {"config":config,"keys":{"cpp":"new-secret"}})
        self.assertEqual(self.keys.get("cpp"), "new-secret")
        self.assertNotIn("new-secret", self.config.path.read_text())

    def test_missing_session_token_cannot_modify_state(self):
        req=urllib.request.Request(self.url+"/api/stop",data=b"{}")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req)
        self.assertEqual(caught.exception.code,403)

    def test_clear_key_is_explicit_and_blank_field_preserves_it(self):
        self.req("/api/config", {"config":self.app.public_config(),"keys":{}})
        self.assertEqual(self.keys.get("cpp"),"private-test-key")
        self.req("/api/config", {"config":self.app.public_config(),"keys":{"cpp":""}})
        self.assertEqual(self.keys.get("cpp"),"")

    def test_registration_error_leaves_saved_config_unchanged(self):
        self.config.save(self.config.get())
        old = self.config.path.read_text()
        class Keyboard:
            def apply(self, config):
                raise ValueError("快捷键已被占用")
        self.app.keyboard=Keyboard()
        value=self.app.public_config();value["pause_hotkey"]="F10"
        with self.assertRaises(urllib.error.HTTPError):
            self.req("/api/config", {"config":value,"keys":{}})
        self.assertEqual(self.config.path.read_text(),old)

    def test_clipboard_hotkey_survives_http_save_and_reload(self):
        config = self.req("/api/config")
        self.assertEqual(config["profiles"][0]["clipboard_hotkey"], "F2")
        for hotkey in ("F6", ""):
            config["profiles"][0]["clipboard_hotkey"] = hotkey
            response = self.req("/api/config", {"config": config, "keys": {}})
            self.assertEqual(response["profiles"][0]["clipboard_hotkey"], hotkey)
            self.assertEqual(self.req("/api/config")["profiles"][0]["clipboard_hotkey"], hotkey)
            self.assertEqual(ConfigStore(self.config.path).get()["profiles"][0]["clipboard_hotkey"], hotkey)


class APITests(unittest.TestCase):
    def test_real_http_request_contains_language_and_untruncated_ocr(self):
        received = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                payload=json.dumps({"choices":[{"finish_reason":"stop","message":{"content":"```python\nprint(3)\n```"}}]}).encode()
                self.send_response(200);self.end_headers();self.wfile.write(payload)
        server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            p=copy.deepcopy(DEFAULT["profiles"][0]);p.update(language="Python 3",base_url=f"http://127.0.0.1:{server.server_port}")
            question="题目" * 3000
            self.assertEqual(ai_client.generate(p,"fake",question),"print(3)")
            self.assertEqual(received[0]["messages"][1]["content"],question)
            self.assertIn("Python 3",received[0]["messages"][0]["content"])
            prompt = received[0]["messages"][0]["content"]
            self.assertIn("函数题只实现题目要求的函数", prompt)
            self.assertIn("完整编程题返回完整可运行程序", prompt)
            next_question = "函数接口定义：int sum(int a, int b);"
            ai_client.generate(p, "fake", next_question)
            self.assertEqual(len(received[1]["messages"]), 2)
            self.assertEqual(received[1]["messages"][1], {"role":"user","content":next_question})
        finally:
            server.shutdown();server.server_close()

    def test_empty_code_cannot_arm_input(self):
        with self.assertRaises(ai_client.ApiError):
            ai_client.clean_code("  ")


if __name__ == "__main__":
    unittest.main()
