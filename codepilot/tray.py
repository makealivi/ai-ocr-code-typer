"""Native Windows notification-area icon, owned by its own message thread."""
import ctypes as c
from ctypes import wintypes as w
import logging
import queue
import threading
import uuid


LRESULT = c.c_ssize_t
UINT_PTR = c.c_size_t
WNDPROC = c.WINFUNCTYPE(LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
WM_CLOSE, WM_DESTROY, WM_TIMER = 0x0010, 0x0002, 0x0113
WM_TRAY = 0x8003
NIM_ADD, NIM_MODIFY, NIM_DELETE, NIM_SETFOCUS, NIM_SETVERSION = range(5)
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_GUID, NIF_SHOWTIP = 1, 2, 4, 32, 128
STATE_TIMER, BLINK_TIMER = 1, 2


class GUID(c.Structure):
    _fields_ = [("Data1", w.DWORD), ("Data2", w.WORD), ("Data3", w.WORD),
                ("Data4", c.c_ubyte * 8)]


class VERSION(c.Union):
    _fields_ = [("uTimeout", w.UINT), ("uVersion", w.UINT)]


class NOTIFYICONDATA(c.Structure):
    _anonymous_ = ("version",)
    _fields_ = [("cbSize", w.DWORD), ("hWnd", w.HWND), ("uID", w.UINT),
                ("uFlags", w.UINT), ("uCallbackMessage", w.UINT), ("hIcon", w.HICON),
                ("szTip", w.WCHAR * 128), ("dwState", w.DWORD), ("dwStateMask", w.DWORD),
                ("szInfo", w.WCHAR * 256), ("version", VERSION),
                ("szInfoTitle", w.WCHAR * 64), ("dwInfoFlags", w.DWORD),
                ("guidItem", GUID), ("hBalloonIcon", w.HICON)]


class WNDCLASS(c.Structure):
    _fields_ = [("style", w.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", c.c_int),
                ("cbWndExtra", c.c_int), ("hInstance", w.HINSTANCE), ("hIcon", w.HICON),
                ("hCursor", w.HANDLE), ("hbrBackground", w.HBRUSH),
                ("lpszMenuName", w.LPCWSTR), ("lpszClassName", w.LPCWSTR)]


PHASE_LABELS = {"idle": "等待获取题目", "capture": "正在截图", "ocr": "正在识别文字",
                "reading_clipboard": "正在读取剪贴板", "generating": "正在生成代码",
                "ready": "可以逐字输入", "done": "本轮输入完成", "error": "本轮未完成，请查看配置页"}


class TrayService:
    def __init__(self, controller, assets, identity, open_config, on_exit):
        self.controller = controller
        self.assets = assets
        self.open_config = open_config
        self.on_exit = on_exit
        self.guid = GUID.from_buffer_copy(uuid.uuid5(uuid.NAMESPACE_URL, "codepilot/tray/" + identity).bytes_le)
        self.state = {"round": 0, "phase": "idle", "paused": False, "pending": False,
                      "closed": False, "enabled": False}
        self.blinking = False
        self.bright = True
        self.icons = {}
        self.hwnd = None
        self.nid = None
        self.error = None
        self.started = threading.Event()
        self.thread = None
        self.proc = WNDPROC(self._window_proc)
        self.user = c.WinDLL("user32", use_last_error=True)
        self.shell = c.WinDLL("shell32", use_last_error=True)
        self.kernel = c.WinDLL("kernel32", use_last_error=True)
        self._bind_api()

    def _bind_api(self):
        signatures = {
            "RegisterClassW": ([c.POINTER(WNDCLASS)], w.ATOM),
            "UnregisterClassW": ([w.LPCWSTR, w.HINSTANCE], w.BOOL),
            "CreateWindowExW": ([w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, c.c_int, c.c_int,
                                  c.c_int, c.c_int, w.HWND, w.HMENU, w.HINSTANCE, c.c_void_p], w.HWND),
            "DefWindowProcW": ([w.HWND, w.UINT, w.WPARAM, w.LPARAM], LRESULT),
            "DestroyWindow": ([w.HWND], w.BOOL),
            "GetMessageW": ([c.POINTER(w.MSG), w.HWND, w.UINT, w.UINT], c.c_int),
            "TranslateMessage": ([c.POINTER(w.MSG)], w.BOOL),
            "DispatchMessageW": ([c.POINTER(w.MSG)], LRESULT),
            "PostMessageW": ([w.HWND, w.UINT, w.WPARAM, w.LPARAM], w.BOOL),
            "PostQuitMessage": ([c.c_int], None),
            "RegisterWindowMessageW": ([w.LPCWSTR], w.UINT),
            "SetTimer": ([w.HWND, UINT_PTR, w.UINT, c.c_void_p], UINT_PTR),
            "KillTimer": ([w.HWND, UINT_PTR], w.BOOL),
            "GetDpiForSystem": ([], w.UINT),
            "GetSystemMetricsForDpi": ([c.c_int, w.UINT], c.c_int),
            "LoadImageW": ([w.HINSTANCE, w.LPCWSTR, w.UINT, c.c_int, c.c_int, w.UINT], w.HANDLE),
            "DestroyIcon": ([w.HICON], w.BOOL),
            "CreatePopupMenu": ([], w.HMENU),
            "AppendMenuW": ([w.HMENU, w.UINT, UINT_PTR, w.LPCWSTR], w.BOOL),
            "TrackPopupMenu": ([w.HMENU, w.UINT, c.c_int, c.c_int, c.c_int, w.HWND, c.c_void_p], w.UINT),
            "DestroyMenu": ([w.HMENU], w.BOOL),
            "GetCursorPos": ([c.POINTER(w.POINT)], w.BOOL),
            "SetForegroundWindow": ([w.HWND], w.BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.user, name)
            function.argtypes, function.restype = args, result
        self.shell.Shell_NotifyIconW.argtypes = [w.DWORD, c.POINTER(NOTIFYICONDATA)]
        self.shell.Shell_NotifyIconW.restype = w.BOOL
        self.kernel.GetModuleHandleW.argtypes = [w.LPCWSTR]
        self.kernel.GetModuleHandleW.restype = w.HMODULE

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True, name="notification-tray")
        self.thread.start()
        self.started.wait()
        if self.error:
            raise self.error

    def stop(self):
        if self.hwnd:
            self.user.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
        if self.thread:
            self.thread.join()

    def _load_icons(self):
        dpi = self.user.GetDpiForSystem()
        width = self.user.GetSystemMetricsForDpi(49, dpi)
        height = self.user.GetSystemMetricsForDpi(50, dpi)
        for name in ("normal", "dim"):
            icon = self.user.LoadImageW(None, str(self.assets / (name + ".ico")), 1, width, height, 0x10)
            if not icon:
                raise c.WinError(c.get_last_error())
            self.icons[name] = icon

    def _notify(self, command):
        if not self.shell.Shell_NotifyIconW(command, c.byref(self.nid)):
            raise OSError("Windows 托盘图标更新失败")

    def _add_icon(self):
        self.nid = NOTIFYICONDATA(cbSize=c.sizeof(NOTIFYICONDATA), hWnd=self.hwnd, uID=1,
                                  uFlags=NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_GUID | NIF_SHOWTIP,
                                  uCallbackMessage=WM_TRAY, guidItem=self.guid)
        self._set_icon_data()
        self._notify(NIM_ADD)
        self.nid.uVersion = 4
        self._notify(NIM_SETVERSION)

    def _set_icon_data(self):
        name = "dim" if self.blinking and not self.bright else "normal"
        self.nid.hIcon = self.icons[name]
        label = PHASE_LABELS[self.state["phase"]]
        if self.state["paused"]:
            label = "输入已暂停 · " + label
        self.nid.szTip = "CodePilot · " + label

    def _render(self):
        self._set_icon_data()
        self._notify(NIM_MODIFY)

    def _consume_state(self):
        latest = None
        while True:
            try:
                latest = self.controller.tray_events.get_nowait()
            except queue.Empty:
                break
        if latest is None:
            return
        self.state = latest
        blinking = (latest["enabled"] and latest["phase"] == "ready" and latest["pending"]
                    and not latest["paused"] and not latest["closed"])
        if blinking != self.blinking:
            self.blinking = blinking
            self.bright = True
            if blinking:
                if not self.user.SetTimer(self.hwnd, BLINK_TIMER, 500, None):
                    raise c.WinError(c.get_last_error())
            else:
                self.user.KillTimer(self.hwnd, BLINK_TIMER)
        if latest["enabled"] and not latest["closed"]:
            if self.nid is None:
                self._add_icon()
            else:
                self._render()
        elif self.nid is not None:
            self._notify(NIM_DELETE)
            self.nid = None

    def _menu(self, hwnd):
        # Use current state for actions even if a generation completed just before the click.
        self._consume_state()
        if self.nid is None:
            return
        menu = self.user.CreatePopupMenu()
        if not menu:
            raise c.WinError(c.get_last_error())
        try:
            items = [(1, "打开配置"), (2, "恢复输入" if self.state["paused"] else "暂停输入"),
                     (3, "结束本轮"), (0, None), (4, "退出程序")]
            for command, label in items:
                flags = 0x800 if label is None else 0
                if command in (2, 3) and self.state["phase"] in ("idle", "done", "error"):
                    flags |= 1
                if not self.user.AppendMenuW(menu, flags, command, label):
                    raise c.WinError(c.get_last_error())
            point = w.POINT()
            if not self.user.GetCursorPos(c.byref(point)):
                raise c.WinError(c.get_last_error())
            self.user.SetForegroundWindow(hwnd)
            command = self.user.TrackPopupMenu(menu, 0x100 | 0x02, point.x, point.y, 0, hwnd, None)
            self.user.PostMessageW(hwnd, 0, 0, 0)
        finally:
            self.user.DestroyMenu(menu)
        if command == 1:
            self.open_config()
        elif command == 2:
            self.controller.toggle_pause()
        elif command == 3:
            self.controller.stop_round()
        elif command == 4:
            self.on_exit()
        if self.nid is not None:
            self.shell.Shell_NotifyIconW(NIM_SETFOCUS, c.byref(self.nid))
        self._consume_state()

    def _window_proc(self, hwnd, message, wp, lp):
        try:
            if message == WM_TIMER:
                self._consume_state()
                if wp == BLINK_TIMER and self.blinking:
                    self.bright = not self.bright
                    self._render()
                return 0
            if message == WM_TRAY:
                if self.nid is None:
                    return 0
                event = lp & 0xFFFF
                if event in (0x400, 0x401):  # NIN_SELECT / NIN_KEYSELECT (version 4)
                    self.controller.acknowledge_notice(self.state["round"])
                    self._consume_state()
                elif event == 0x007B:  # WM_CONTEXTMENU
                    self._menu(hwnd)
                return 0
            if message == self.taskbar_created:
                if self.nid is not None:
                    self._add_icon()
                self._consume_state()
                return 0
            if message in (0x001A, 0x007E) and self.nid:  # Taskbar display/scale changes.
                previous, self.icons = self.icons, {}
                try:
                    self._load_icons()
                    self._render()
                finally:
                    for icon in previous.values():
                        self.user.DestroyIcon(icon)
                return 0
            if message == WM_CLOSE:
                self.user.DestroyWindow(hwnd)
                return 0
            if message == WM_DESTROY:
                self.hwnd = None
                self.user.PostQuitMessage(0)
                return 0
        except Exception as error:
            self.error = error
            logging.exception("Notification tray failed")
            self.controller.report_error(f"托盘提示失败：{error}")
            self.user.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            return 0
        return self.user.DefWindowProcW(hwnd, message, wp, lp)

    def _run(self):
        instance = self.kernel.GetModuleHandleW(None)
        class_name = "CodePilotTray-" + uuid.uuid4().hex
        registered = False
        try:
            self.taskbar_created = self.user.RegisterWindowMessageW("TaskbarCreated")
            if not self.taskbar_created:
                raise c.WinError(c.get_last_error())
            wc = WNDCLASS(lpfnWndProc=self.proc, hInstance=instance, lpszClassName=class_name)
            if not self.user.RegisterClassW(c.byref(wc)):
                raise c.WinError(c.get_last_error())
            registered = True
            # A hidden top-level window receives the Explorer TaskbarCreated broadcast.
            self.hwnd = self.user.CreateWindowExW(0, class_name, "CodePilot 通知", 0,
                                                   0, 0, 0, 0, None, None, instance, None)
            if not self.hwnd:
                raise c.WinError(c.get_last_error())
            self._load_icons()
            self._consume_state()
            if not self.user.SetTimer(self.hwnd, STATE_TIMER, 100, None):
                raise c.WinError(c.get_last_error())
            self.started.set()
            message = w.MSG()
            while True:
                result = self.user.GetMessageW(c.byref(message), None, 0, 0)
                if result == -1:
                    raise c.WinError(c.get_last_error())
                if result == 0:
                    break
                self.user.TranslateMessage(c.byref(message))
                self.user.DispatchMessageW(c.byref(message))
        except Exception as error:
            self.error = error
            logging.exception("Notification tray stopped")
        finally:
            if self.nid:
                self.shell.Shell_NotifyIconW(NIM_DELETE, c.byref(self.nid))
            if self.hwnd:
                self.user.DestroyWindow(self.hwnd)
            for icon in self.icons.values():
                self.user.DestroyIcon(icon)
            if registered:
                self.user.UnregisterClassW(class_name, instance)
            self.started.set()
