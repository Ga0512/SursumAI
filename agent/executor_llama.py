from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import signal
import subprocess
import sys
import tarfile
import time
import urllib.request
import zipfile
from pathlib import Path

from huggingface_hub import HfApi, hf_hub_download
from huggingface_hub.errors import RepositoryNotFoundError

from core.spec import Spec

# Runtime strategy: NVIDIA GPU -> docker image (CUDA); otherwise -> native binary
# (Metal on macOS, CPU elsewhere). This is a product decision: the official
# llama.cpp releases ship no CUDA build for Linux, so docker is the only way to
# get CUDA without compiling from source; on machines without an NVIDIA GPU the
# tiny native binary is faster to bring up than a multi-GB docker image.
IMAGE = "ghcr.io/ggml-org/llama.cpp:server"
BASE_PORT = 9000
LOGS_DIR = Path(__file__).resolve().parent.parent / "sursumai-logs"
MODELS_DIR = Path(__file__).resolve().parent.parent / "llama-models"
BIN_DIR = Path(__file__).resolve().parent.parent / "llama-bin"
BIN_VERSION = "b10327"  # pinned llama.cpp release (security: no silent upgrade)
BIN_REPO = "ggml-org/llama.cpp"
BIN_API = f"https://api.github.com/repos/{BIN_REPO}/releases/tags/{BIN_VERSION}"
DEFAULT_QUANTS = ["Q4_K_M", "Q5_K_M", "Q6_K", "Q8_0", "Q4_0", "Q1_0"]

MODEL_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class TransportError(Exception):
    pass


def deploy_port(deploy_id: str) -> int:
    digest = int(hashlib.sha256(deploy_id.encode()).hexdigest()[:8], 16)
    return BASE_PORT + (digest % 100)


def endpoint(deploy_id: str) -> str:
    return f"http://localhost:{deploy_port(deploy_id)}/v1"


def _log_file(deploy_id: str) -> Path:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    return LOGS_DIR / f"{deploy_id[:12]}.log"


def _log(deploy_id: str, line: str) -> None:
    with open(_log_file(deploy_id), "a") as f:
        f.write(line.rstrip() + "\n")


def _gpu_available() -> bool:
    try:
        result = subprocess.run(["nvidia-smi"], capture_output=True, timeout=10)
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


# ---- docker helpers ----

def _docker_info() -> None:
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=15)
    except subprocess.TimeoutExpired:
        raise TransportError("Docker is not running or not installed") from None
    except FileNotFoundError:
        raise TransportError("Docker is not installed") from None
    if result.returncode != 0:
        raise TransportError("Docker is not running or not installed")


def _image_present() -> bool:
    try:
        result = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, timeout=15)
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _stream_logs(log_path: Path, cmd: list[str]) -> None:
    with open(log_path, "ab") as f:
        subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT)


def _follow_logs(deploy_id: str) -> None:
    log_path = _log_file(deploy_id)
    name = f"deploy-{deploy_id[:12]}"
    with open(log_path, "ab") as f:
        subprocess.Popen(
            ["docker", "logs", "-f", name],
            stdout=f, stderr=subprocess.STDOUT,
            start_new_session=True,
        )


def _docker_name(deploy_id: str) -> str:
    return f"deploy-{deploy_id[:12]}"


def _docker_is_running(deploy_id: str) -> bool:
    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", _docker_name(deploy_id)],
            capture_output=True, timeout=10,
        )
        return result.returncode == 0 and result.stdout.strip() == b"true"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _docker_stop(deploy_id: str) -> None:
    try:
        subprocess.run(["docker", "rm", "-f", _docker_name(deploy_id)], capture_output=True, timeout=10)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass


# ---- native binary helpers ----

def _platform_key() -> str:
    system = platform.system()
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    if system == "Darwin":
        return f"macos-{arch}"
    if system == "Linux":
        return f"ubuntu-{arch}"
    if system == "Windows":
        return f"win-cpu-{arch}"
    raise TransportError(f"unsupported platform: {system} {machine}")


def _asset_name() -> str:
    key = _platform_key()
    if key.startswith("win-"):
        return f"llama-{BIN_VERSION}-bin-{key}.zip"
    return f"llama-{BIN_VERSION}-bin-{key}.tar.gz"


def _release_sha256() -> str:
    """Fetch the official sha256 digest for the pinned asset from the GitHub API."""
    try:
        with urllib.request.urlopen(BIN_API, timeout=30) as resp:
            data = json.load(resp)
    except Exception as e:
        raise TransportError(f"could not reach llama.cpp releases: {e}") from None
    wanted = _asset_name()
    for asset in data.get("assets", []):
        if asset.get("name") == wanted:
            digest = asset.get("digest", "")
            if digest.startswith("sha256:"):
                return digest.split(":", 1)[1]
    raise TransportError(f"release {BIN_VERSION} has no asset {wanted!r}")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _binary_path() -> Path:
    key = _platform_key()
    dirname = f"llama-{BIN_VERSION}-bin-{key}"
    if key.startswith("win-"):
        return BIN_DIR / dirname / "llama-server.exe"
    return BIN_DIR / dirname / f"llama-{BIN_VERSION}" / "llama-server"


def _ensure_binary() -> str:
    """Download, verify (sha256) and extract the pinned llama.cpp binary. Returns path."""
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    exe = _binary_path()
    if exe.exists():
        return str(exe)

    asset = _asset_name()
    archive = BIN_DIR / asset
    url = f"https://github.com/{BIN_REPO}/releases/download/{BIN_VERSION}/{asset}"
    expected = _release_sha256()

    if archive.exists():
        if _sha256(archive) == expected:
            pass
        else:
            archive.unlink()

    if not archive.exists():
        try:
            urllib.request.urlretrieve(url, archive)
        except Exception as e:
            raise TransportError(f"failed to download llama.cpp binary: {e}") from None

    actual = _sha256(archive)
    if actual != expected:
        archive.unlink()
        raise TransportError(
            f"checksum mismatch for llama.cpp binary (got {actual[:12]}…, expected {expected[:12]}…) — refusing to run"
        )

    dirname = BIN_DIR / f"llama-{BIN_VERSION}-bin-{_platform_key()}"
    dirname.mkdir(parents=True, exist_ok=True)
    if str(archive).endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dirname)
    else:
        with tarfile.open(archive, "r:gz") as tf:
            tf.extractall(dirname)

    if not exe.exists():
        raise TransportError(f"llama-server not found after extracting {asset}")
    os.chmod(exe, 0o755)
    return str(exe)


def _pid_file(deploy_id: str) -> Path:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    return LOGS_DIR / f"{deploy_id[:12]}.pid"


def _binary_is_running(deploy_id: str) -> bool:
    pid_path = _pid_file(deploy_id)
    if not pid_path.exists():
        return False
    try:
        pid = int(pid_path.read_text().strip())
        os.kill(pid, 0)
        return True
    except (ValueError, OSError, ProcessLookupError):
        return False


def _binary_stop(deploy_id: str) -> None:
    pid_path = _pid_file(deploy_id)
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            for _ in range(20):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.1)
            else:
                os.kill(pid, signal.SIGKILL)
        except (ValueError, OSError, ProcessLookupError):
            pass
        try:
            pid_path.unlink()
        except OSError:
            pass


# ---- HF resolution ----

def _validate_model_id(model: str) -> str:
    if not MODEL_ID_RE.match(model):
        raise TransportError(
            f"invalid model id '{model}': expected 'org/name' (letters, digits, '-', '_', '.')"
        )
    return model


def _pick_gguf(filenames: list[str]) -> str | None:
    gguFs = [f for f in filenames if f.endswith(".gguf") and "mmproj" not in f.lower()]
    if not gguFs:
        return None
    for q in DEFAULT_QUANTS:
        for f in gguFs:
            if q in f:
                return f
    return gguFs[0]


def _pick_mmproj(filenames: list[str]) -> str | None:
    for f in filenames:
        if f.lower().endswith(".gguf") and "mmproj" in f.lower():
            return f
    return None


def resolve_model(spec: Spec) -> dict:
    """Inspect the HF repo and return GGUF + optional mmproj (vision encoder)."""
    model = _validate_model_id(spec.model)
    api = HfApi(token=spec.hf_token or None)
    try:
        info = api.model_info(model)
    except RepositoryNotFoundError:
        raise TransportError(f"model '{model}' not found on Hugging Face")
    except Exception as e:
        raise TransportError(f"could not reach Hugging Face: {e}") from None
    files = [s.rfilename for s in info.siblings]
    gguf = _pick_gguf(files)
    mmproj = _pick_mmproj(files)
    if gguf is None:
        raise TransportError(
            f"no GGUF file found in '{model}' — llama runtime needs a GGUF (quantized) repo"
        )
    return {
        "model": model,
        "gguf": gguf,
        "mmproj": mmproj,
        "vision": mmproj is not None,
        "revision": info.sha,
    }


def preflight(spec: Spec) -> list[dict]:
    """Return structured checks. Never raises: each check is a dict with ok/name/detail."""
    checks: list[dict] = []
    strategy = "docker" if _gpu_available() else "binary"
    checks.append({
        "name": "strategy", "ok": True,
        "detail": f"runtime llama-server via {strategy}" +
                  ("" if strategy == "binary" else " (NVIDIA GPU detected)"),
    })

    if strategy == "docker":
        try:
            _docker_info()
            checks.append({"name": "docker", "ok": True, "detail": "Docker available"})
        except TransportError as e:
            checks.append({"name": "docker", "ok": False, "detail": str(e)})

        if _image_present():
            checks.append({"name": "image", "ok": True, "detail": f"image cached ({IMAGE})"})
        else:
            checks.append({"name": "image", "ok": True, "detail": f"image will be pulled ({IMAGE})"})
    else:
        try:
            exe = _ensure_binary()
            checks.append({
                "name": "binary", "ok": True,
                "detail": f"llama.cpp {BIN_VERSION} ({_platform_key()}) ready: {Path(exe).name}",
            })
        except TransportError as e:
            checks.append({"name": "binary", "ok": False, "detail": str(e)})

    try:
        resolved = resolve_model(spec)
        checks.append({"name": "model", "ok": True, "detail": f"{resolved['model']} found on HF"})
        checks.append({
            "name": "gguf", "ok": True,
            "detail": f"{resolved['gguf']}" + (f" + mmproj {resolved['mmproj']} (VLM)" if resolved["vision"] else " (LLM)"),
        })
        checks.append({"name": "vision", "ok": True, "detail": "VLM — mmproj detected" if resolved["vision"] else "LLM only"})
    except TransportError as e:
        checks.append({"name": "model", "ok": False, "detail": str(e)})
        checks.append({"name": "gguf", "ok": False, "detail": "n/a"})
        checks.append({"name": "vision", "ok": False, "detail": "n/a"})
    return checks


# ---- GGUF download ----

def _download_gguf(spec: Spec, resolved: dict, deploy_id: str) -> dict[str, str]:
    """Download GGUF (+ mmproj) into a per-model dir, verifying GGUF magic bytes."""
    model = resolved["model"]
    safe_repo = model.replace("/", "--")
    local_dir = MODELS_DIR / safe_repo
    local_dir.mkdir(parents=True, exist_ok=True)

    def _fetch(filename: str) -> str:
        if not filename or not re.fullmatch(r"[A-Za-z0-9._/-]+", filename):
            raise TransportError(f"unsafe file name rejected: {filename!r}")
        target = local_dir / filename.split("/")[-1]
        if target.exists() and _is_gguf(target):
            _log(deploy_id, f"=== {filename} already downloaded ===")
            return str(target)
        _log(deploy_id, f"=== downloading {model}/{filename} ===")
        try:
            path = hf_hub_download(
                repo_id=model, filename=filename,
                token=spec.hf_token or None, local_dir=str(local_dir),
            )
        except Exception as e:
            raise TransportError(f"download failed for {filename}: {e}") from None
        target = Path(path)
        if not _is_gguf(target):
            raise TransportError(f"downloaded file is not a valid GGUF: {filename}")
        return str(target)

    paths = {"gguf": _fetch(resolved["gguf"])}
    if resolved["mmproj"]:
        paths["mmproj"] = _fetch(resolved["mmproj"])
    return paths


def _is_gguf(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"GGUF"
    except OSError:
        return False


# ---- commands ----

def _docker_build_cmd(spec: Spec, deploy_id: str, paths: dict[str, str]) -> list[str]:
    port = deploy_port(deploy_id)
    model_dir = Path(paths["gguf"]).parent
    cmd = [
        "docker", "run", "-d", "--rm",
        "--name", _docker_name(deploy_id),
        "-p", f"{port}:8080",
        "-v", f"{model_dir}:/models:ro",
    ]
    if spec.gpus and _gpu_available():
        cmd += ["--runtime", "nvidia", "--gpus", "all"]
    cmd += [
        IMAGE,
        "--model", f"/models/{Path(paths['gguf']).name}",
        "--host", "0.0.0.0",
        "--port", "8080",
        "--ctx-size", str(max(spec.max_model_len, 128)),
        "-n", str(spec.max_tokens),
        "-t", str(max(2, min(spec.gpus * 4, 16))),
        "--metrics",
    ]
    if paths.get("mmproj"):
        cmd += ["--mmproj", f"/models/{Path(paths['mmproj']).name}"]
    return cmd


def _binary_build_cmd(spec: Spec, deploy_id: str, paths: dict[str, str], exe: str) -> list[str]:
    port = deploy_port(deploy_id)
    cmd = [
        exe,
        "--model", paths["gguf"],
        "--host", "0.0.0.0",
        "--port", str(port),
        "--ctx-size", str(max(spec.max_model_len, 128)),
        "-n", str(spec.max_tokens),
        "-t", str(max(2, min(spec.gpus * 4, 16))),
        "--metrics",
    ]
    if paths.get("mmproj"):
        cmd += ["--mmproj", paths["mmproj"]]
    return cmd


def _binary_env(exe: str) -> dict[str, str]:
    """Make sure llama.cpp finds its bundled shared libs (Linux)."""
    env = dict(os.environ)
    lib_dir = Path(exe).resolve().parent
    if lib_dir.exists():
        env["LD_LIBRARY_PATH"] = str(lib_dir) + os.pathsep + env.get("LD_LIBRARY_PATH", "")
    return env


# ---- lifecycle ----

def start(spec: Spec, deploy_id: str) -> str:
    log_path = _log_file(deploy_id)
    resolved = resolve_model(spec)
    _log(deploy_id, f"=== model: {resolved['model']} ({'VLM' if resolved['vision'] else 'LLM'}) ===")
    paths = _download_gguf(spec, resolved, deploy_id)

    if _gpu_available():
        _docker_info()
        if _image_present():
            _log(deploy_id, f"=== using local image {IMAGE} ===")
        else:
            _log(deploy_id, f"=== pulling image {IMAGE} (first run may take a while) ===")
            _stream_logs(log_path, ["docker", "pull", IMAGE])
            _log(deploy_id, "=== image ready, starting container ===")
        cmd = _docker_build_cmd(spec, deploy_id, paths)
        _log(deploy_id, ">>> " + " ".join(cmd))
        _stream_logs(log_path, cmd)
        _log(deploy_id, "=== container started, following container logs ===")
        _follow_logs(deploy_id)
    else:
        _log(deploy_id, "=== no NVIDIA GPU — using native llama.cpp binary ===")
        exe = _ensure_binary()
        cmd = _binary_build_cmd(spec, deploy_id, paths, exe)
        _log(deploy_id, ">>> " + " ".join(cmd))
        proc = subprocess.Popen(
            cmd,
            stdout=open(log_path, "ab"),
            stderr=subprocess.STDOUT,
            env=_binary_env(exe),
            start_new_session=True,
        )
        _pid_file(deploy_id).write_text(str(proc.pid))
        _log(deploy_id, f"=== llama-server started (pid {proc.pid}), logging to {log_path.name} ===")

    return endpoint(deploy_id)


def is_running(deploy_id: str) -> bool:
    return _binary_is_running(deploy_id) or _docker_is_running(deploy_id)


def stage(deploy_id: str) -> str:
    """Last human-readable '=== ... ===' marker from the deploy log (progress)."""
    log_path = _log_file(deploy_id)
    if not log_path.exists():
        return "starting"
    for line in reversed(log_path.read_text(errors="replace").splitlines()):
        line = line.strip()
        if line.startswith("==="):
            return _friendly_stage(line.strip("= ").lower())
    return "starting"


def _friendly_stage(marker: str) -> str:
    if "already downloaded" in marker:
        return "model already downloaded"
    if "downloading" in marker:
        return "downloading model"
    if "pulling image" in marker:
        return "preparing runtime (first run downloads it)"
    if "image ready" in marker:
        return "starting container"
    if "container started" in marker:
        return "starting inference server"
    if "llama-server started" in marker:
        return "starting inference server"
    if "no nvidia gpu" in marker:
        return "running on CPU (no NVIDIA GPU)"
    if "model:" in marker:
        return "preparing model"
    return marker


def stop(deploy_id: str) -> None:
    _binary_stop(deploy_id)
    _docker_stop(deploy_id)


def logs(deploy_id: str, tail: int = 300) -> str:
    log_path = _log_file(deploy_id)
    if not log_path.exists():
        return "(no log yet)"
    content = log_path.read_text(errors="replace").replace("\r", "\n")
    lines = [l for l in content.splitlines() if l.strip()]
    return "\n".join(lines[-tail:]) + "\n"
