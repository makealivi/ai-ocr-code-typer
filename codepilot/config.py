import copy
import json
import re
import threading
from pathlib import Path
from urllib.parse import urlparse


DEFAULT = {
    "show_tray_icon": False,
    "pause_hotkey": "F8",
    "stop_hotkey": "F9",
    "profiles": [{
        "id": "cpp", "hotkey": "F1", "clipboard_hotkey": "F2",
        "language": "C++17", "base_url": "https://api.deepseek.com",
        "model": "deepseek-flash",
    }],
}

MODIFIERS = {"ctrl": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B}
TRIGGER_KEYS = frozenset(range(65, 91)) | {0xDB, 0xDD, 0xBA, 0xDE, 0xBC, 0xBE, 0xBF}
KEYS = {"space": 0x20, "pause": 0x13, "insert": 0x2D,
        "delete": 0x2E, "home": 0x24, "end": 0x23,
        "pageup": 0x21, "pagedown": 0x22}


def parse_hotkey(text):
    parts = [part.strip().lower() for part in text.split("+")]
    if not parts or any(not p for p in parts):
        raise ValueError("快捷键不能为空，例如 Ctrl+Alt+1 或 F8")
    if len(set(parts)) != len(parts) or any(p not in MODIFIERS for p in parts[:-1]):
        raise ValueError(f"快捷键格式不正确：{text}")
    key = parts[-1]
    if re.fullmatch(r"f([1-9]|1[0-9]|2[0-4])", key):
        vk = 0x70 + int(key[1:]) - 1
    elif len(key) == 1 and key.isascii() and key.isalnum():
        vk = ord(key.upper())
    elif key in KEYS:
        vk = KEYS[key]
    else:
        raise ValueError(f"不支持的快捷键：{text}")
    mods = frozenset(parts[:-1])
    if vk in TRIGGER_KEYS and mods <= {"shift"}:
        raise ValueError("字母和 [];',./ 留作逐字输入，请为操作快捷键添加 Ctrl 或 Alt")
    return mods, vk


def validate(config):
    if not isinstance(config, dict):
        raise ValueError("配置必须是一个对象")
    if not isinstance(config["show_tray_icon"], bool):
        raise ValueError("显示微信样式托盘图标必须为开或关")
    profiles = config["profiles"]
    if not profiles:
        raise ValueError("请至少保留一个方案")
    bindings = [parse_hotkey(config[k]) for k in ("pause_hotkey", "stop_hotkey")]
    ids = set()
    for p in profiles:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", p["id"]) or p["id"] in ids:
            raise ValueError("方案 ID 必须唯一，且仅包含字母、数字、下划线和短横线")
        ids.add(p["id"])
        for key in ("language", "model"):
            if not isinstance(p[key], str) or not p[key].strip():
                raise ValueError(f"方案的 {key} 不能为空")
        url = urlparse(p["base_url"])
        if url.scheme not in ("https", "http") or not url.netloc or url.username or url.password:
            raise ValueError("API 地址应为 http:// 或 https:// 开头的服务地址")
        bindings.append(parse_hotkey(p["hotkey"]))
        if p["clipboard_hotkey"]:
            bindings.append(parse_hotkey(p["clipboard_hotkey"]))
    if len(bindings) != len(set(bindings)):
        raise ValueError("截图、剪贴板、暂停和结束快捷键不能重复")


class ConfigStore:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.value = copy.deepcopy(DEFAULT)
        if self.path.exists():
            self.value = json.loads(self.path.read_text(encoding="utf-8"))
            self.value.setdefault("show_tray_icon", False)
            self.value.pop("startup_url", None)
            used = {parse_hotkey(self.value[k]) for k in ("pause_hotkey", "stop_hotkey")}
            for profile in self.value["profiles"]:
                used.add(parse_hotkey(profile["hotkey"]))
                if profile.get("clipboard_hotkey"):
                    used.add(parse_hotkey(profile["clipboard_hotkey"]))
            for profile in self.value["profiles"]:
                if "clipboard_hotkey" not in profile:
                    profile["clipboard_hotkey"] = next(
                        (f"F{number}" for number in range(2, 25)
                         if number != 12 and parse_hotkey(f"F{number}") not in used), "")
                    if profile["clipboard_hotkey"]:
                        used.add(parse_hotkey(profile["clipboard_hotkey"]))
                for field in ("capture", "monitor", "region", "name"):
                    profile.pop(field, None)
            validate(self.value)

    def get(self):
        with self.lock:
            return copy.deepcopy(self.value)

    def save(self, value):
        validate(value)
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.path)
            self.value = copy.deepcopy(value)
