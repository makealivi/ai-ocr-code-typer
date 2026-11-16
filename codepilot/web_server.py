import json
import mimetypes
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from . import ai_client
from .config import validate


class WebApp:
    def __init__(self, config, credentials, controller, ocr, web_root, on_exit):
        self.config = config
        self.credentials = credentials
        self.controller = controller
        self.ocr = ocr
        self.web_root = web_root
        self.on_exit = on_exit
        self.keyboard = None
        self.token = secrets.token_urlsafe(32)
        self.save_lock = threading.Lock()

    def public_config(self):
        config = self.config.get()
        for p in config["profiles"]:
            p["has_key"] = bool(self.credentials.get(p["id"]))
        return config

    def save(self, body):
        value = body["config"]
        # Only known configuration fields reach disk; keys never enter config.json.
        config = {k: value[k] for k in ("pause_hotkey", "stop_hotkey")}
        fields = ("id", "hotkey", "language", "base_url", "model")
        config["profiles"] = [{k: p[k] for k in fields} for p in value["profiles"]]
        validate(config)
        keys = body.get("keys", {})
        ids = {p["id"] for p in config["profiles"]}
        if not set(keys) <= ids:
            raise ValueError("API Key 对应的方案不存在")
        with self.save_lock:
            previous = self.config.get()
            previous_keys = {p: self.credentials.get(p) for p in keys}
            if self.keyboard:
                self.keyboard.apply(config)
            try:
                for profile_id, key in keys.items():
                    if key:
                        self.credentials.set(profile_id, key.strip())
                    else:
                        self.credentials.delete(profile_id)
                self.config.save(config)
            except Exception:
                for profile_id, key in previous_keys.items():
                    if key:
                        self.credentials.set(profile_id, key)
                    else:
                        self.credentials.delete(profile_id)
                if self.keyboard:
                    self.keyboard.apply(previous)
                raise
        return self.public_config()

    def dispatch(self, path, body):
        if path == "/api/config":
            return self.save(body)
        if path == "/api/recording":
            if self.keyboard:
                self.keyboard.record_hotkey(bool(body["active"]))
            return {"ok": True}
        if path == "/api/models":
            p = body["profile"]
            key = body.get("key") or self.credentials.get(p["id"])
            return {"models": ai_client.list_models(p["base_url"], key)}
        if path == "/api/pause":
            self.controller.toggle_pause()
        elif path == "/api/stop":
            self.controller.stop_round()
        elif path == "/api/shutdown":
            threading.Timer(0.3, self.on_exit).start()
        else:
            raise ValueError("接口不存在")
        return {"ok": True}


def create_server(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send_json(self, value, status=200):
            self.send_data(json.dumps(value, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8", status)

        def send_data(self, data, content_type, status=200):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)

        def authorized(self):
            return secrets.compare_digest(self.headers.get("X-Session-Token", ""), app.token)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path.startswith("/api/"):
                if not self.authorized():
                    self.send_json({"error": "会话已失效，请重新双击启动文件打开网页"}, 403)
                    return
                try:
                    if path == "/api/config":
                        value = app.public_config()
                    elif path == "/api/status":
                        value = app.controller.snapshot()
                    else:
                        self.send_json({"error": "接口不存在"}, 404)
                        return
                    self.send_json(value)
                except Exception as error:
                    self.send_json({"error": str(error)}, 400)
                return
            assets = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}
            if path not in assets:
                self.send_data(b"Not found", "text/plain", 404)
                return
            file = app.web_root / assets[path]
            kind = mimetypes.guess_type(str(file))[0] or "application/octet-stream"
            self.send_data(file.read_bytes(), kind + "; charset=utf-8")

        def do_POST(self):
            if not self.authorized():
                self.send_json({"error": "会话已失效，请重新启动打开配置页"}, 403)
                return
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                self.send_json(app.dispatch(urlsplit(self.path).path, body))
            except (ValueError, KeyError, TypeError, OSError, RuntimeError) as error:
                self.send_json({"error": str(error)}, 400)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    return server
