"""Local release smoke check. No screen capture or AI generation."""
import json
from pathlib import Path
import subprocess
import time
import urllib.request

import pefile


ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "release" / "启动" / "启动.exe"
assert pefile.PE(str(EXE)).OPTIONAL_HEADER.Subsystem == 2, "Executable must use Windows GUI subsystem"
process = subprocess.Popen([str(EXE), "--no-browser"], creationflags=0x08000000)
runtime = EXE.parent / "data" / "runtime.json"
deadline = time.monotonic() + 20
while not runtime.exists():
    if process.poll() is not None or time.monotonic() > deadline:
        raise AssertionError("Release did not start; inspect release/启动/data/app.log")
    time.sleep(0.1)
address = json.loads(runtime.read_text(encoding="utf-8"))["url"]
base, token = address.split("/#")


def request(path, body=None):
    raw = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + "/api/" + path, data=raw,
          headers={"X-Session-Token": token, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as response:
        return json.load(response)


try:
    config = request("config")
    assert config["profiles"][0]["model"] == "deepseek-flash"
    assert config["profiles"][0]["hotkey"] == "F1"
    assert config["pause_hotkey"] == "F8" and config["stop_hotkey"] == "F9"
    assert "startup_url" not in config and "name" not in config["profiles"][0]
    assert config["profiles"][0]["has_key"], "Existing key migration must survive packaging"
    assert not request("status")["error"], request("status")["error"]
    assert not {"capture", "monitor", "region"}.intersection(config["profiles"][0])
    with urllib.request.urlopen(base + "/", timeout=5) as response:
        html = response.read().decode("utf-8")
    assert 'id="capture"' not in html and 'id="monitor"' not in html
    assert 'id="startup-url"' not in html and 'id="name"' not in html
    request("recording", {"active": True})
    request("recording", {"active": False})
    request("config", {"config": config, "keys": {}})
    duplicate = subprocess.run([str(EXE), "--no-browser"], timeout=10, creationflags=0x08000000)
    assert duplicate.returncode == 0
    assert json.loads(runtime.read_text(encoding="utf-8"))["url"] == address
finally:
    request("shutdown", {})
    process.wait(timeout=10)
assert process.returncode == 0
ocr_data = ROOT / ".qa" / "package-ocr"
ocr_process = subprocess.run([str(EXE), "--data-dir", str(ocr_data), "--check-ocr",
                              str(ROOT / ".qa" / "ocr-fixture.png")],
                             timeout=45, creationflags=0x08000000)
assert ocr_process.returncode == 0
ocr = json.loads((ocr_data / "ocr-check.json").read_text(encoding="utf-8"))
assert "两数之和" in ocr["text"]
report = {"gui_executable": True, "startup": True, "hotkeys": True,
          "credentials_migrated": True, "single_instance": True,
          "configuration": True, "shutdown": True, "bundled_ocr": True}
(ROOT / ".qa" / "package-check.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print("Release startup, GUI subsystem, hotkeys, credentials, single instance, configuration, shutdown and bundled OCR passed.")
