import argparse
import ast
import ctypes as c
from ctypes import wintypes as w
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys
import threading
import webbrowser

from codepilot.capture_ocr import CaptureOCR
from codepilot.config import ConfigStore, DEFAULT
from codepilot.controller import Controller
from codepilot.credentials import Credentials
from codepilot.desktop import Desktop, KeyboardService
from codepilot.web_server import WebApp, create_server


ROOT = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
ASSETS = Path(getattr(sys, "_MEIPASS", ROOT))


def migrate_legacy_key(credentials):
    """Read the old constant without importing or running the old program."""
    legacy = ROOT / "ai_onshot.py"
    if credentials.get("cpp") or not legacy.exists():
        return
    tree = ast.parse(legacy.read_text(encoding="utf-8-sig"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "API_KEY" for t in node.targets):
            value = ast.literal_eval(node.value)
            if value:
                credentials.set("cpp", value)
            return


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--no-hooks", action="store_true", help="仅用于配置页验证，不监听键盘")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--check-ocr", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    args.data_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(args.data_dir / "app.log", maxBytes=500_000, backupCount=2, encoding="utf-8")
    logging.basicConfig(handlers=[handler], level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("codepilot")
    if args.check_ocr:
        import numpy as np
        from PIL import Image
        text = CaptureOCR().recognize(np.asarray(Image.open(args.check_ocr).convert("RGB"))[:, :, ::-1].copy())
        (args.data_dir / "ocr-check.json").write_text(json.dumps({"text": text}, ensure_ascii=False), encoding="utf-8")
        return
    kernel = c.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [c.c_void_p, w.BOOL, w.LPCWSTR]
    kernel.CreateMutexW.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    name = hashlib.sha256(str(args.data_dir.resolve()).lower().encode()).hexdigest()[:24]
    mutex = kernel.CreateMutexW(None, False, "Local\\CodePilot-" + name)
    already_running = c.get_last_error() == 183
    runtime = args.data_dir / "runtime.json"
    if already_running:
        try:
            if not args.no_browser:
                info = json.loads(runtime.read_text(encoding="utf-8"))
                webbrowser.open(info["url"])
        finally:
            kernel.CloseHandle(mutex)
        return
    desktop = Desktop()
    desktop.enable_dpi()
    credentials = Credentials("CodePilot-" + name)
    startup_error = ""
    try:
        config = ConfigStore(args.data_dir / "config.json")
    except (ValueError, KeyError, TypeError) as error:
        startup_error = f"配置文件读取失败：{error}。页面已载入初始配置，修正并保存后生效。"
        config = ConfigStore(args.data_dir / "recovered.json")
        config.path = args.data_dir / "config.json"
    try:
        migrate_legacy_key(credentials)
    except (OSError, SyntaxError, ValueError) as error:
        startup_error = f"旧 API Key 迁移失败，请在网页重新填写：{error}"
    if not config.path.exists():
        config.save(config.get())
    ocr = CaptureOCR()
    controller = Controller(config, credentials, desktop, ocr)
    if startup_error:
        controller.report_error(startup_error)
    exit_event = threading.Event()
    app = WebApp(config, credentials, controller, ocr, ASSETS / "web", exit_event.set)
    server = create_server(app)
    address = f"http://127.0.0.1:{server.server_port}/#{app.token}"
    runtime.write_text(json.dumps({"url": address}), encoding="utf-8")
    keyboard = None
    try:
        if not args.no_hooks:
            keyboard = KeyboardService(desktop, controller)
            try:
                keyboard.start(config.get())
                app.keyboard = keyboard
            except OSError as error:
                controller.report_error(f"键盘监听启动失败：{error}")
        threading.Thread(target=server.serve_forever, daemon=True, name="local-web").start()
        if not args.no_browser:
            webbrowser.open(address)
        log.info("Started local configuration server on port %s", server.server_port)
        exit_event.wait()
    except KeyboardInterrupt:
        pass
    finally:
        controller.close()
        if keyboard:
            keyboard.stop()
        server.shutdown()
        server.server_close()
        runtime.unlink(missing_ok=True)
        kernel.CloseHandle(mutex)
        log.info("Stopped")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.exception("Startup failed")
