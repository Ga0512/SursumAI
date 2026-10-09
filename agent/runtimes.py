"""What this machine already has, before anything is downloaded.

A rented pod is rarely empty: it is a container that often ships PyTorch, and
sometimes vLLM itself. Installing on top of that wastes gigabytes and can break
what is there — reusing it costs nothing. So the rule is the same one the
llama.cpp binary already follows: look first, download only what is missing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

NEWLINE = chr(10)

# Our own isolated environment, when vLLM has to be installed. Never the
# machine's Python: a pod's image pins its own torch, and `pip install vllm`
# over it is how a working machine stops working.
VLLM_VENV = Path.home() / "sursumai" / "vllm-venv"
VLLM_VERSION = "0.21.0"          # the same version the pinned image carries
VLLM_DOWNLOAD_GB = 8             # what to warn the user about before starting


def _run(cmd: list[str], timeout: float = 30) -> tuple[int, str]:
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return done.returncode, (done.stdout or done.stderr or "").strip()
    except (subprocess.TimeoutExpired, OSError):
        return 1, ""


def _python_of(venv: Path) -> Path:
    return venv / ("Scripts" if sys.platform == "win32" else "bin") / "python"


def _package(python: str | Path, name: str) -> str:
    """The installed version of a package, as that interpreter sees it."""
    code = (f"import importlib.metadata as m; print(m.version({name!r}))")
    rc, out = _run([str(python), "-c", code], timeout=60)
    return out if rc == 0 else ""


def _torch(python: str | Path) -> dict:
    code = ("import json, torch;"
            "print(json.dumps({'version': torch.__version__,"
            " 'cuda': torch.version.cuda, 'sees_gpu': torch.cuda.is_available()}))")
    rc, out = _run([str(python), "-c", code], timeout=120)
    if rc != 0:
        return {}
    try:
        return json.loads(out.splitlines()[-1])
    except (ValueError, IndexError):
        return {}


def _driver_cuda() -> str:
    rc, out = _run(["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"])
    return out.splitlines()[0].strip() if rc == 0 and out else ""


def _free_disk_gb(path: Path) -> float:
    try:
        return shutil.disk_usage(path).free / 2 ** 30
    except OSError:
        return 0.0


def vllm_command() -> list[str]:
    """How to run vLLM here: the machine's own, ours, or nothing."""
    exe = shutil.which("vllm")
    if exe:
        return [exe]
    if _package(sys.executable, "vllm"):
        return [sys.executable, "-m", "vllm"]
    ours = _python_of(VLLM_VENV)
    if ours.exists() and _package(ours, "vllm"):
        return [str(ours), "-m", "vllm"]
    return []


def report() -> dict:
    """Everything the dashboard needs to decide what to offer and what to warn
    about. Cheap enough to answer on every capabilities call."""
    home_python = sys.executable
    ours = _python_of(VLLM_VENV)

    vllm_cmd = vllm_command()
    where = ""
    if vllm_cmd:
        where = "this machine" if str(VLLM_VENV) not in vllm_cmd[0] else "installed by SursumAI"

    torch = _torch(home_python)
    if not torch and ours.exists():
        torch = _torch(ours)

    return {
        "vllm": {
            "present": bool(vllm_cmd),
            "version": _package(vllm_cmd[0], "vllm") if vllm_cmd and Path(vllm_cmd[0]).exists() else "",
            "where": where,
            "can_install": _free_disk_gb(Path.home()) > VLLM_DOWNLOAD_GB + 2,
            "needs_gb": VLLM_DOWNLOAD_GB,
        },
        "torch": {
            "present": bool(torch),
            "version": torch.get("version", ""),
            "cuda": torch.get("cuda") or "",
            "sees_gpu": bool(torch.get("sees_gpu")),
        },
        "driver": _driver_cuda(),
        "free_disk_gb": round(_free_disk_gb(Path.home()), 1),
    }


# ---- installing it, when the machine does not have it --------------------

LOG = Path(__file__).resolve().parent.parent / "sursumai-logs" / "vllm-install.log"
MARKER = LOG.parent / "vllm-install.running"


def install_log(tail: int = 200) -> str:
    try:
        lines = LOG.read_text(errors="replace").splitlines()
    except OSError:
        return ""
    return NEWLINE.join(lines[-tail:])


def installing() -> bool:
    """True while an install is running. The marker goes away when it ends,
    however it ends."""
    return MARKER.exists()


def install() -> None:
    """Put vLLM in our own virtualenv, pinned, touching nothing of the machine's.

    A pod image pins its own torch, and `pip install vllm` over it is how a
    working machine stops working. Blocking: the caller runs it in the
    background. 8 GB and minutes, which is why nothing here starts without the
    user asking."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    MARKER.write_text("")
    uv = shutil.which("uv")
    try:
        with open(LOG, "a", encoding="utf-8") as out:
            def step(title: str, cmd: list[str]) -> int:
                out.write(f"{NEWLINE}=== {title} ==={NEWLINE}")
                out.flush()
                return subprocess.call(cmd, stdout=out, stderr=subprocess.STDOUT)

            create = ([uv, "venv", str(VLLM_VENV)] if uv
                      else [sys.executable, "-m", "venv", str(VLLM_VENV)])
            if step(f"creating the environment in {VLLM_VENV}", create) != 0:
                out.write(f"{NEWLINE}=== the environment could not be created ==={NEWLINE}")
                return

            python = str(_python_of(VLLM_VENV))
            installer = ([uv, "pip", "install", "--python", python] if uv
                         else [python, "-m", "pip", "install"])
            ok = step(f"downloading vLLM {VLLM_VERSION} and PyTorch (the long part)",
                      installer + [f"vllm=={VLLM_VERSION}"]) == 0
            out.write(f"{NEWLINE}=== {'done' if ok else 'vLLM could not be installed'} ==={NEWLINE}")
    finally:
        MARKER.unlink(missing_ok=True)
