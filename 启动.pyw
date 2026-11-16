import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_PYTHONW = ROOT / ".venv" / "Scripts" / "pythonw.exe"


def _using_project_venv():
    if not VENV_PYTHONW.exists():
        return True
    try:
        return Path(sys.executable).resolve() == VENV_PYTHONW.resolve()
    except OSError:
        return False


if __name__ == "__main__":
    # .pyw is associated with the system interpreter, which does not have mss.
    if not _using_project_venv():
        subprocess.Popen([str(VENV_PYTHONW), str(Path(__file__).resolve()), *sys.argv[1:]])
        raise SystemExit(0)
    import logging

    from main import main

    try:
        main()
    except Exception:
        logging.exception("Startup failed")
