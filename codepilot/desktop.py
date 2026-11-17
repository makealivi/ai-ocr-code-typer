import ctypes as c
import copy
from ctypes import wintypes as w
import queue
import threading
import time

from .config import TRIGGER_KEYS, parse_hotkey


ULONG_PTR = c.c_size_t
LRESULT = c.c_ssize_t
HOOKPROC = c.WINFUNCTYPE(LRESULT, c.c_int, w.WPARAM, w.LPARAM)
TAG = 0x4350494C


class KEYBDINPUT(c.Structure):
    _fields_ = [("wVk", w.WORD), ("wScan", w.WORD), ("dwFlags", w.DWORD),
                ("time", w.DWORD), ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(c.Structure):
    _fields_ = [("dx", w.LONG), ("dy", w.LONG), ("mouseData", w.DWORD),
                ("dwFlags", w.DWORD), ("time", w.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(c.Structure):
    _fields_ = [("uMsg", w.DWORD), ("wParamL", w.WORD), ("wParamH", w.WORD)]


class INPUTUNION(c.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(c.Structure):
    _anonymous_ = ("data",)
    _fields_ = [("type", w.DWORD), ("data", INPUTUNION)]


class KBDLLHOOKSTRUCT(c.Structure):
    _fields_ = [("vkCode", w.DWORD), ("scanCode", w.DWORD), ("flags", w.DWORD),
                ("time", w.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KeyRouter:
    """Pure event routing; no I/O in the low-level hook."""

    def __init__(self):
        self.down = set()
        self.suppressed = set()

    def route(self, vk, keyup, injected, modifiers, accept):
        if injected:
            return False
        if keyup:
            self.down.discard(vk)
            suppressed = vk in self.suppressed
            self.suppressed.discard(vk)
            return suppressed
        repeat = vk in self.down
        self.down.add(vk)
        if vk in self.suppressed:
            return True
        if not repeat and vk in TRIGGER_KEYS and not modifiers.intersection({"ctrl", "alt", "win"}):
            if accept():
                self.suppressed.add(vk)
                return True
        return False


class Desktop:
    def __init__(self):
        self.user = c.WinDLL("user32", use_last_error=True)
        self.kernel = c.WinDLL("kernel32", use_last_error=True)
        self.user.GetForegroundWindow.restype = w.HWND
        self.user.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, c.c_int]
        self.user.GetWindowRect.argtypes = [w.HWND, c.POINTER(w.RECT)]
        self.user.GetWindowRect.restype = w.BOOL
        self.user.SendInput.argtypes = [w.UINT, c.POINTER(INPUT), c.c_int]
        self.user.SendInput.restype = w.UINT
        self.user.GetAsyncKeyState.argtypes = [c.c_int]
        self.user.GetAsyncKeyState.restype = c.c_short
        self.user.OpenClipboard.argtypes = [w.HWND]
        self.user.OpenClipboard.restype = w.BOOL
        self.user.CloseClipboard.argtypes = []
        self.user.CloseClipboard.restype = w.BOOL
        self.user.GetClipboardData.argtypes = [w.UINT]
        self.user.GetClipboardData.restype = w.HANDLE
        self.user.IsClipboardFormatAvailable.argtypes = [w.UINT]
        self.user.IsClipboardFormatAvailable.restype = w.BOOL
        self.user.SetClipboardData.argtypes = [w.UINT, w.HANDLE]
        self.user.SetClipboardData.restype = w.HANDLE
        self.kernel.GlobalAlloc.argtypes = [w.UINT, c.c_size_t]
        self.kernel.GlobalAlloc.restype = w.HGLOBAL
        self.kernel.GlobalLock.argtypes = [w.HGLOBAL]
        self.kernel.GlobalLock.restype = c.c_void_p
        self.kernel.GlobalUnlock.argtypes = [w.HGLOBAL]
        self.kernel.GlobalFree.argtypes = [w.HGLOBAL]

    def enable_dpi(self):
        self.user.SetProcessDpiAwarenessContext.argtypes = [c.c_void_p]
        self.user.SetProcessDpiAwarenessContext(c.c_void_p(-4))

    def foreground(self):
        return self.user.GetForegroundWindow()

    def is_config_window(self, hwnd):
        title = c.create_unicode_buffer(1024)
        self.user.GetWindowTextW(hwnd, title, len(title))
        return "CodePilot · 配置与运行" in title.value

    def window_rect(self, hwnd):
        rect = w.RECT()
        if not self.user.GetWindowRect(hwnd, c.byref(rect)):
            raise c.WinError(c.get_last_error())
        if rect.right <= rect.left or rect.bottom <= rect.top:
            raise ValueError("当前窗口没有可截图的区域")
        return {"left": rect.left, "top": rect.top,
                "width": rect.right - rect.left, "height": rect.bottom - rect.top}

    def clipboard(self, text):
        raw = (text + "\0").encode("utf-16-le")
        handle = self.kernel.GlobalAlloc(0x0002, len(raw))
        if not handle:
            raise c.WinError(c.get_last_error())
        transferred = False
        try:
            ptr = self.kernel.GlobalLock(handle)
            if not ptr:
                raise c.WinError(c.get_last_error())
            c.memmove(ptr, raw, len(raw))
            self.kernel.GlobalUnlock(handle)
            self._open_clipboard()
            try:
                if not self.user.EmptyClipboard():
                    raise c.WinError(c.get_last_error())
                if not self.user.SetClipboardData(13, handle):
                    raise c.WinError(c.get_last_error())
                transferred = True
            finally:
                self.user.CloseClipboard()
        finally:
            if not transferred:
                self.kernel.GlobalFree(handle)

    def _open_clipboard(self):
        for attempt in range(10):
            if self.user.OpenClipboard(None):
                return
            time.sleep(0.02)
        raise RuntimeError("剪贴板正被占用，请稍后重新生成")

    def read_clipboard(self):
        self._open_clipboard()
        try:
            if not self.user.IsClipboardFormatAvailable(13):
                raise ValueError("剪贴板中没有文字，请先手动选中题目并复制")
            handle = self.user.GetClipboardData(13)
            if not handle:
                raise c.WinError(c.get_last_error())
            ptr = self.kernel.GlobalLock(handle)
            if not ptr:
                raise c.WinError(c.get_last_error())
            try:
                return c.wstring_at(ptr)
            finally:
                self.kernel.GlobalUnlock(handle)
        finally:
            self.user.CloseClipboard()

    @staticmethod
    def key(vk=0, scan=0, flags=0):
        return INPUT(type=1, ki=KEYBDINPUT(vk, scan, flags, 0, TAG))

    def send_char(self, char):
        # Shift is a trigger modifier, not a modifier for the generated code.
        shifts = [vk for vk in (0xA0, 0xA1) if self.user.GetAsyncKeyState(vk) & 0x8000]
        events = [self.key(vk=vk, flags=2) for vk in shifts]
        if char == "\n":
            events += [self.key(vk=0x0D), self.key(vk=0x0D, flags=2)]
        else:
            raw = char.encode("utf-16-le")
            for i in range(0, len(raw), 2):
                unit = int.from_bytes(raw[i:i + 2], "little")
                events += [self.key(scan=unit, flags=4), self.key(scan=unit, flags=6)]
        events += [self.key(vk=vk) for vk in shifts]
        array = (INPUT * len(events))(*events)
        if self.user.SendInput(len(events), array, c.sizeof(INPUT)) != len(events):
            raise RuntimeError("字符发送失败，请让程序与编辑器使用相同权限级别")


class KeyboardService:
    def __init__(self, desktop, controller):
        self.desktop = desktop
        self.controller = controller
        self.router = KeyRouter()
        self.commands = queue.Queue()
        self.started = threading.Event()
        self.thread_id = None
        self.error = None
        self.bindings = []
        self.actions = {}
        self.modifier_keys = set()
        self.recording_until = 0
        self.proc = HOOKPROC(self._hook)
        u, k = self.desktop.user, self.desktop.kernel
        u.SetWindowsHookExW.argtypes = [c.c_int, HOOKPROC, w.HINSTANCE, w.DWORD]
        u.SetWindowsHookExW.restype = w.HANDLE
        u.CallNextHookEx.argtypes = [w.HANDLE, c.c_int, w.WPARAM, w.LPARAM]
        u.CallNextHookEx.restype = LRESULT
        u.UnhookWindowsHookEx.argtypes = [w.HANDLE]
        u.RegisterHotKey.argtypes = [w.HWND, c.c_int, w.UINT, w.UINT]
        u.RegisterHotKey.restype = w.BOOL
        u.UnregisterHotKey.argtypes = [w.HWND, c.c_int]
        u.PostThreadMessageW.argtypes = [w.DWORD, w.UINT, w.WPARAM, w.LPARAM]
        u.GetMessageW.argtypes = [c.POINTER(w.MSG), w.HWND, w.UINT, w.UINT]
        u.GetMessageW.restype = w.BOOL
        u.SetTimer.argtypes = [w.HWND, ULONG_PTR, w.UINT, c.c_void_p]
        u.SetTimer.restype = ULONG_PTR
        u.KillTimer.argtypes = [w.HWND, ULONG_PTR]
        k.GetModuleHandleW.argtypes = [w.LPCWSTR]
        k.GetModuleHandleW.restype = w.HMODULE

    def start(self, config):
        threading.Thread(target=self._run, args=(config,), daemon=True, name="keyboard-hook").start()
        self.started.wait()
        if self.error:
            raise self.error

    def _register(self, config):
        candidates = [(config["pause_hotkey"], ("pause", None)),
                      (config["stop_hotkey"], ("stop", None))]
        candidates += [(p["hotkey"], ("capture", copy.deepcopy(p))) for p in config["profiles"]]
        candidates += [(p["clipboard_hotkey"], ("clipboard", copy.deepcopy(p)))
                       for p in config["profiles"] if p["clipboard_hotkey"]]
        installed = []
        try:
            for number, (text, action) in enumerate(candidates, 1):
                mods, vk = parse_hotkey(text)
                flags = 0x4000 | sum({"alt": 1, "ctrl": 2, "shift": 4, "win": 8}[m] for m in mods)
                if not self.desktop.user.RegisterHotKey(None, number, flags, vk):
                    raise ValueError(f"快捷键 {text} 注册失败，可能已被其他程序使用，请换一个组合")
                installed.append(number)
                self.actions[number] = action
        except Exception:
            for number in installed:
                self.desktop.user.UnregisterHotKey(None, number)
            self.actions.clear()
            raise
        self.bindings = candidates

    def _unregister(self):
        for number in list(self.actions):
            self.desktop.user.UnregisterHotKey(None, number)
        self.actions.clear()

    def _dispatch_hotkey(self, number):
        if number not in self.actions:
            return
        action, profile = self.actions[number]
        if action == "capture":
            self.controller.capture(profile["id"], self.desktop.foreground(), profile=profile)
        elif action == "clipboard":
            self.controller.use_clipboard(profile["id"], self.desktop.foreground(), profile=profile)
        elif action == "pause":
            self.controller.toggle_pause()
        elif action == "stop":
            self.controller.stop_round()

    def apply(self, config):
        self._command("config", config)

    def record_hotkey(self, active):
        self._command("recording", active)

    def _command(self, kind, value):
        done = threading.Event()
        result = []
        self.commands.put((kind, value, done, result))
        if not self.desktop.user.PostThreadMessageW(self.thread_id, 0x8001, 0, 0):
            raise RuntimeError("键盘监听线程未运行，请重新启动程序")
        done.wait()
        if result:
            raise result[0]

    def _run(self, config):
        self.thread_id = self.desktop.kernel.GetCurrentThreadId()
        hook = None
        timer = None
        current = config
        try:
            message = w.MSG()
            self.desktop.user.PeekMessageW(c.byref(message), None, 0, 0, 0)
            hook = self.desktop.user.SetWindowsHookExW(13, self.proc,
                       self.desktop.kernel.GetModuleHandleW(None), 0)
            if not hook:
                raise c.WinError(c.get_last_error())
            # A registration conflict is shown on the page; the loop remains alive for edits.
            try:
                self._register(current)
            except ValueError as error:
                self.controller.report_error(str(error))
            self.started.set()
            timer = self.desktop.user.SetTimer(None, 0, 1000, None)
            while self.desktop.user.GetMessageW(c.byref(message), None, 0, 0) > 0:
                if message.message == 0x0312:
                    self._dispatch_hotkey(message.wParam)
                elif message.message == 0x8001:
                    kind, new, done, result = self.commands.get()
                    self._unregister()
                    try:
                        if kind == "recording":
                            self.recording_until = time.monotonic() + 4 if new else 0
                            if not self.recording_until:
                                self._register(current)
                        else:
                            current_before = current
                            self._register(new)
                            current = new
                            if self.recording_until:
                                self._unregister()
                    except Exception as error:
                        result.append(error)
                        if kind == "config":
                            current = current_before
                        try:
                            if not self.recording_until:
                                self._register(current)
                        except ValueError as rollback_error:
                            self.controller.report_error(str(rollback_error))
                    finally:
                        done.set()
                elif message.message == 0x0113 and self.recording_until and time.monotonic() >= self.recording_until:
                    self.recording_until = 0
                    try:
                        self._register(current)
                    except ValueError as error:
                        self.controller.report_error(str(error))
                elif message.message == 0x8002:
                    break
        except Exception as error:
            self.error = error
            self.controller.report_error(str(error))
            self.started.set()
        finally:
            if timer:
                self.desktop.user.KillTimer(None, timer)
            self._unregister()
            if hook:
                self.desktop.user.UnhookWindowsHookEx(hook)

    def _hook(self, code, message, pointer):
        u = self.desktop.user
        if code < 0:
            return u.CallNextHookEx(None, code, message, pointer)
        event = c.cast(pointer, c.POINTER(KBDLLHOOKSTRUCT)).contents
        # Never acquire the controller lock for our own injected input.
        if event.flags & 0x10 or event.dwExtraInfo == TAG:
            return u.CallNextHookEx(None, code, message, pointer)
        try:
            vk = event.vkCode
            keyup = message in (0x101, 0x105)
            if vk in (0x11, 0xA2, 0xA3, 0x12, 0xA4, 0xA5, 0x5B, 0x5C):
                if keyup:
                    self.modifier_keys.discard(vk)
                else:
                    self.modifier_keys.add(vk)
            modifiers = set()
            if self.modifier_keys.intersection({0x11, 0xA2, 0xA3}):
                modifiers.add("ctrl")
            if self.modifier_keys.intersection({0x12, 0xA4, 0xA5}):
                modifiers.add("alt")
            if self.modifier_keys.intersection({0x5B, 0x5C}):
                modifiers.add("win")
            hwnd = self.desktop.foreground()
            suppressed = self.router.route(vk, keyup, False, modifiers,
                lambda: not self.desktop.is_config_window(hwnd) and self.controller.enqueue_letter(hwnd))
            if suppressed:
                return 1
        except Exception as error:
            self.controller.report_error(str(error))
        return u.CallNextHookEx(None, code, message, pointer)

    def stop(self):
        if self.thread_id:
            self.desktop.user.PostThreadMessageW(self.thread_id, 0x8002, 0, 0)
