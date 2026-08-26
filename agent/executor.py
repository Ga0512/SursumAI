from __future__ import annotations

import hashlib
import json
import subprocess
import urllib.request
from pathlib import Path

from core.spec import Spec

IMAGE = "vllm/vllm-openai:v0.21.0"
BASE_PORT = 9000
LOGS_DIR = Path(__file__).resolve().parent.parent / "sursumai-logs"


class TransportError(Exception):
    pass


def deploy_port(deploy_id: str, spec: Spec | None = None) -> int:
    if spec is not None and spec.port:
        return spec.port
    digest = int(hashlib.sha256(deploy_id.encode()).hexdigest()[:8], 16)
    return BASE_PORT + (digest % 100)


def endpoint(deploy_id: str, spec: Spec | None = None) -> str:
    return f"http://localhost:{deploy_port(deploy_id, spec)}/v1"


def _log_file(deploy_id: str) -> Path:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    return LOGS_DIR / f"{deploy_id[:12]}.log"


def _log(deploy_id: str, line: str) -> None:
    with open(_log_file(deploy_id), "a") as f:
        f.write(line.rstrip() + "\n")


def build_cmd(spec: Spec, deploy_id: str) -> list[str]:
    port = deploy_port(deploy_id, spec)
    cmd = [
        "docker", "run", "-d", "--rm",
        "--name", f"deploy-{deploy_id[:12]}",
        "--runtime", "nvidia", "--gpus", "all",
        "--ipc", "host",
        "-p", f"{port}:8000",
    ]
    if spec.hf_token:
        cmd += ["-e", f"HF_TOKEN={spec.hf_token}"]
    cmd += [
        IMAGE,
        spec.model,
        "--gpu-memory-utilization", str(spec.gpu_memory_utilization),
        "--max-model-len", str(spec.max_model_len),
        "--tensor-parallel-size", str(spec.gpus),
        "--enable-prefix-caching",
    ]
    return cmd


def _docker_info() -> None:
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=15)
    except subprocess.TimeoutExpired:
        raise TransportError("Docker is not running or not installed") from None
    except FileNotFoundError:
        raise TransportError("Docker is not installed") from None
    if result.returncode != 0:
        raise TransportError("Docker is not running or not installed")


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


def _image_present() -> bool:
    try:
        result = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, timeout=15)
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _gpu_count() -> int:
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=index", "--format=csv,noheader"],
            capture_output=True, timeout=10,
        )
        if result.returncode != 0:
            return 0
        return len([l for l in result.stdout.decode(errors="replace").splitlines() if l.strip()])
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return 0


def _gpu_available() -> bool:
    return _gpu_count() > 0


def _hf_files(model: str) -> list[str] | None:
    """List file names of a HF model repo, or None if the repo does not exist."""
    url = f"https://huggingface.co/api/models/{model}"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.load(resp)
            return [s.get("rfilename", "") for s in data.get("siblings", [])]
    except (urllib.error.HTTPError, urllib.error.URLError, ValueError):
        return None


def _hf_check(spec: Spec) -> tuple[bool, str]:
    files = _hf_files(spec.model)
    if files is None:
        return False, f"{spec.model} not found on Hugging Face"
    if not files:
        return True, f"{spec.model} found on Hugging Face"
    gguf = [f for f in files if f.endswith(".gguf")]
    safetensors = [f for f in files if f.endswith(".safetensors")]
    if gguf and not safetensors:
        return False, (
            f"{spec.model} is a GGUF-only repo (vLLM cannot load GGUF). "
            "Use the non-GGUF repo (safetensors) or switch Runtime to llama-server."
        )
    return True, f"{spec.model} found on Hugging Face (safetensors)"


def preflight(spec: Spec) -> list[dict]:
    checks: list[dict] = []
    try:
        _docker_info()
        checks.append({"name": "docker", "ok": True, "detail": "Docker available"})
    except TransportError as e:
        checks.append({"name": "docker", "ok": False, "detail": str(e)})

    if _gpu_available():
        checks.append({"name": "gpu", "ok": True, "detail": "NVIDIA GPU available"})
    else:
        checks.append({"name": "gpu", "ok": False, "detail": "no NVIDIA GPU — vLLM requires CUDA"})

    count = _gpu_count()
    if count and spec.gpus > count:
        checks.append({
            "name": "gpu_count",
            "ok": False,
            "detail": f"{spec.gpus} GPUs requested but only {count} found — vLLM cannot start with more tensor-parallel GPUs than the machine has.",
        })

    if _image_present():
        checks.append({"name": "image", "ok": True, "detail": f"image cached ({IMAGE})"})
    else:
        checks.append({"name": "image", "ok": True, "detail": f"image will be pulled ({IMAGE})"})

    if not spec.model or "/" not in spec.model:
        checks.append({"name": "model", "ok": False, "detail": "model id must be 'org/name'"})
    else:
        ok, detail = _hf_check(spec)
        checks.append({"name": "model", "ok": ok, "detail": detail})
    return checks


def start(spec: Spec, deploy_id: str) -> str:
    _docker_info()
    log_path = _log_file(deploy_id)

    if _image_present():
        _log(deploy_id, f"=== using local image {IMAGE} ===")
    else:
        _log(deploy_id, f"=== pulling image {IMAGE} (first run may take a while) ===")
        _stream_logs(log_path, ["docker", "pull", IMAGE])
        _log(deploy_id, "=== image ready, starting container ===")

    cmd = build_cmd(spec, deploy_id)
    _log(deploy_id, ">>> " + " ".join(cmd))
    _stream_logs(log_path, cmd)

    _log(deploy_id, "=== container started, following container logs ===")
    _follow_logs(deploy_id)

    return endpoint(deploy_id, spec)


def is_running(deploy_id: str) -> bool:
    name = f"deploy-{deploy_id[:12]}"
    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", name],
            capture_output=True, timeout=10,
        )
        return result.returncode == 0 and result.stdout.strip() == b"true"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


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
    if "pulling image" in marker:
        return "preparing runtime (first run downloads it)"
    if "image ready" in marker:
        return "starting container"
    if "container started" in marker:
        return "starting inference server"
    if "model:" in marker:
        return "preparing model"
    return marker


def stop(deploy_id: str) -> None:
    name = f"deploy-{deploy_id[:12]}"
    try:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=10)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass


def logs(deploy_id: str, tail: int = 300) -> str:
    log_path = _log_file(deploy_id)
    if not log_path.exists():
        return "(no log yet)"
    content = log_path.read_text(errors="replace").replace("\r", "\n")
    lines = [l for l in content.splitlines() if l.strip()]
    return "\n".join(lines[-tail:]) + "\n"
