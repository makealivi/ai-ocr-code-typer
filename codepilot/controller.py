import copy
import queue
import threading
import time
from collections import deque

from . import ai_client


class Controller:
    def __init__(self, config, credentials, desktop, ocr, generator=ai_client.generate):
        self.config = config
        self.credentials = credentials
        self.desktop = desktop
        self.ocr = ocr
        self.generator = generator
        self.lock = threading.RLock()
        self.round_id = 0
        self.epoch = 0
        self.phase = "idle"
        self.paused = False
        self.profile_name = ""
        self.question = ""
        self.code = ""
        self.index = 0
        self.reserved = 0
        self.error = ""
        self.history = deque(maxlen=30)
        self.outputs = queue.Queue()
        self.closed = False
        threading.Thread(target=self._output_worker, daemon=True, name="character-output").start()

    def snapshot(self):
        with self.lock:
            return {"phase": self.phase, "paused": self.paused, "profile": self.profile_name,
                    "question": self.question, "code": self.code, "index": self.index,
                    "total": len(self.code), "error": self.error, "round": self.round_id,
                    "history": list(self.history)}

    def _log(self, message):
        self.history.appendleft({"time": time.strftime("%H:%M:%S"), "message": message})

    def report_error(self, message):
        with self.lock:
            self.error = message
            self._log(message)

    def _invalidate_output(self):
        self.epoch += 1
        self.reserved = self.index
        while True:
            try:
                self.outputs.get_nowait()
            except queue.Empty:
                break

    def capture(self, profile_id, hwnd, profile=None):
        if profile is None:
            profile = next(p for p in self.config.get()["profiles"] if p["id"] == profile_id)
        profile = copy.deepcopy(profile)
        with self.lock:
            self.round_id += 1
            token = self.round_id
            self._invalidate_output()
            self.phase = "capture"
            self.paused = False
            self.profile_name = f"{profile['language']} · {profile['hotkey']}"
            self.question = self.code = self.error = ""
            self.index = self.reserved = 0
            self._log(f"开始：{self.profile_name}")
        threading.Thread(target=self._generate, args=(token, profile, hwnd),
                         daemon=True, name=f"round-{token}").start()

    def _generate(self, token, profile, hwnd):
        try:
            key = self.credentials.get(profile["id"])
            if not key:
                raise ValueError("此方案尚未保存 API Key，请填写后保存")
            image = self.ocr.capture()
            with self.lock:
                if token != self.round_id or self.closed:
                    return
                self.phase = "ocr"
            question = self.ocr.recognize(image)
            with self.lock:
                if token != self.round_id or self.closed:
                    return
                self.question = question
                self.phase = "generating"
            code = self.generator(profile, key, question)
            with self.lock:
                if token != self.round_id or self.closed:
                    return
                self.desktop.clipboard(code)
                self.code = code
                self.phase = "ready"
                self._log(f"代码就绪，共 {len(code)} 个字符")
        except Exception as error:
            with self.lock:
                if token == self.round_id and not self.closed:
                    self.phase = "error"
                    self.report_error(str(error))

    def enqueue_letter(self, hwnd):
        with self.lock:
            if self.closed or self.phase != "ready" or self.paused:
                return False
            if self.reserved < len(self.code):
                self.outputs.put((self.round_id, self.epoch, self.reserved, hwnd))
                self.reserved += 1
            # Swallow repeat/extra letters while already reserved characters drain.
            return True

    def _output_worker(self):
        while True:
            item = self.outputs.get()
            if item is None:
                return
            token, epoch, position, hwnd = item
            with self.lock:
                if token != self.round_id or epoch != self.epoch or self.phase != "ready" or self.paused:
                    continue
                if self.desktop.foreground() != hwnd:
                    self.paused = True
                    self._invalidate_output()
                    self._log("窗口已切换，输入暂停；按暂停／恢复快捷键继续")
                    continue
                try:
                    self.desktop.send_char(self.code[position])
                    self.index = position + 1
                    if self.index == len(self.code):
                        self.phase = "done"
                        self._log("本轮输入完成，已恢复正常打字")
                except Exception as error:
                    self.paused = True
                    self._invalidate_output()
                    self.report_error(str(error))

    def toggle_pause(self):
        with self.lock:
            if self.phase not in ("capture", "ocr", "generating", "ready"):
                return
            self.paused = not self.paused
            if self.paused:
                self._invalidate_output()
            self._log("已暂停输入" if self.paused else "已恢复输入")

    def stop_round(self):
        with self.lock:
            self.round_id += 1
            self._invalidate_output()
            self.phase = "idle"
            self.paused = False
            self._log("已结束本轮，恢复正常打字")

    def close(self):
        with self.lock:
            self.closed = True
            self.round_id += 1
            self._invalidate_output()
            self.outputs.put(None)
