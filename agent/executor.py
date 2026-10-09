from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from core import ports
from core.spec import Spec

IMAGE = "vllm/vllm-openai:v0.21.0"
LOGS_DIR = Path(__file__).resolve().parent.parent / "sursumai-logs"


class TransportError(Exception):
    pass


def deploy_port(deploy_id: str, spec: Spec | None = None) -> int:
    """The port this deploy listens on.

    The central allocates it and puts it in the spec. The hash fallback is
    only for deploys created before allocation existed — see core.ports.
    """
    if spec is not None and spec.port:
        return spec.port
    return ports.legacy_port(deploy_id)


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
        "-p", f"{ports.bind_host()}:{port}:8000",
    ]
    # `-e NAME` with no value: docker forwards it from our environment, so the
    # secret never lands in an argument list (see runtime_env)
    if spec.hf_token:
        cmd += ["-e", "HF_TOKEN"]
    if spec.api_key:
        cmd += ["-e", "VLLM_API_KEY"]
    cmd += [
        IMAGE,
        spec.model,
        "--gpu-memory-utilization", str(spec.gpu_memory_utilization),
        "--max-model-len", str(spec.max_model_len),
        "--tensor-parallel-size", str(spec.gpus),
        "--enable-prefix-caching",
    ]
    cmd += _advanced_args(spec)
    return cmd


def _advanced_args(spec: Spec) -> list[str]:
    """The Advanced overrides, as vLLM flags. Empty unless the user set one.

    `--kv-cache-dtype` takes fp8 where llama.cpp takes q8_0; the modal offers
    one list for both runtimes and the translation happens here, so the words
    on screen never have to be a runtime's spelling."""
    args: list[str] = []
    kv = {"q8_0": "fp8", "q4_0": "fp8", "f16": "auto"}.get(spec.kv_cache)
    if kv:
        args += ["--kv-cache-dtype", kv]
    if spec.quantization:
        args += ["--quantization", spec.quantization]
    if spec.trust_remote_code:
        # several new models ship their own modelling code and simply refuse to
        # load without this
        args += ["--trust-remote-code"]
    if spec.max_num_seqs or spec.parallel:
        args += ["--max-num-seqs", str(spec.max_num_seqs or spec.parallel)]
    if spec.cpu_offload_gb:
        # vLLM 0.21 dropped --swap-space with the V1 engine; this is the knob
        # that still trades GPU memory for CPU memory, and it is what lets a
        # model that does not fit in VRAM run at all
        args += ["--cpu-offload-gb", str(spec.cpu_offload_gb)]
    # top_p and repeat_penalty are not server flags in vLLM: they are sampling
    # values sent per request, so the modal offers them on llama.cpp only
    return args


def build_native_cmd(spec: Spec, deploy_id: str) -> list[str]:
    """`vllm serve` straight on the machine. Same flags as the container gets —
    they are the server's own, not Docker's."""
    exe = vllm_here()
    if not exe:
        raise TransportError("vLLM is not installed on this machine")
    head = [sys.executable, "-m", "vllm"] if exe == "python -m vllm" else [exe]
    cmd = head + [
        "serve", spec.model,
        "--host", ports.bind_host(),
        "--port", str(deploy_port(deploy_id, spec)),
        "--gpu-memory-utilization", str(spec.gpu_memory_utilization),
        "--max-model-len", str(spec.max_model_len),
        "--tensor-parallel-size", str(spec.gpus),
        "--enable-prefix-caching",
    ]
    # the key travels in the environment here too (see runtime_env): argv ends
    # up in the deploy log and in /proc
    return cmd + _advanced_args(spec)


def _native_pid_file(deploy_id: str) -> Path:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    return LOGS_DIR / f"{deploy_id[:12]}.vllm.pid"


def _native_running(deploy_id: str) -> bool:
    pid_file = _native_pid_file(deploy_id)
    try:
        os.kill(int(pid_file.read_text().strip()), 0)
        return True
    except (OSError, ValueError):
        return False


def _start_native(spec: Spec, deploy_id: str) -> None:
    cmd = build_native_cmd(spec, deploy_id)
    _log(deploy_id, "=== vLLM is installed on this machine, running it directly ===")
    _log(deploy_id, ">>> " + " ".join(cmd))
    log_path = _log_file(deploy_id)
    with open(log_path, "ab") as logf:
        proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT,
                                env=runtime_env(spec), start_new_session=True)
    _native_pid_file(deploy_id).write_text(str(proc.pid))
    _log(deploy_id, "=== inference server starting ===")


def _stop_native(deploy_id: str) -> None:
    pid_file = _native_pid_file(deploy_id)
    try:
        pid = int(pid_file.read_text().strip())
    except (OSError, ValueError):
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(pid), sig)
        except OSError:
            break
        for _ in range(30):
            if not _native_running(deploy_id):
                break
            time.sleep(0.1)
        if not _native_running(deploy_id):
            break
    pid_file.unlink(missing_ok=True)


def runtime_env(spec: Spec) -> dict[str, str]:
    """Environment for the docker client: carries the secrets the container
    inherits through the bare `-e NAME` flags in build_cmd."""
    env = dict(os.environ)
    if spec.hf_token:
        env["HF_TOKEN"] = spec.hf_token
    if spec.api_key:
        env["VLLM_API_KEY"] = spec.api_key
    return env


def vllm_here() -> str:
    """The `vllm` command on this machine, or "" — a RunPod pod IS a container,
    so Docker inside it is not an option, but the image often ships vLLM itself.
    Using what the machine already has beats refusing to run."""
    exe = shutil.which("vllm")
    if exe:
        return exe
    try:
        out = subprocess.run([sys.executable, "-c", "import vllm, sys; print(sys.executable)"],
                             capture_output=True, text=True, timeout=30)
        if out.returncode == 0:
            return "python -m vllm"
    except (subprocess.TimeoutExpired, OSError):
        pass
    return ""


def _docker_here() -> bool:
    try:
        _docker_info()
        return True
    except TransportError:
        return False


def _runtime_strategy() -> str:
    """docker when Docker is usable, native when the machine already has vLLM
    installed, and nothing otherwise."""
    if _docker_here():
        return "docker"
    if vllm_here():
        return "native"
    return "none"


def _docker_info() -> None:
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=15)
    except subprocess.TimeoutExpired:
        raise TransportError("Docker is not running or not installed") from None
    except FileNotFoundError:
        raise TransportError("Docker is not installed") from None
    if result.returncode != 0:
        raise TransportError("Docker is not running or not installed")


def _stream_logs(log_path: Path, cmd: list[str],
                 env: dict[str, str] | None = None) -> None:
    with open(log_path, "ab") as f:
        subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, env=env)


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
    strategy = _runtime_strategy()
    if strategy == "docker":
        checks.append({"name": "runtime", "ok": True, "detail": "Docker available"})
    elif strategy == "native":
        checks.append({"name": "runtime", "ok": True,
                       "detail": "vLLM is installed on this machine — running it without Docker"})
    else:
        checks.append({"name": "runtime", "ok": False,
                       "detail": "this machine has neither Docker nor vLLM installed — "
                                 "install one, or deploy with llama.cpp instead"})

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

    if strategy == "docker":
        where = "cached" if _image_present() else "will be pulled"
        checks.append({"name": "image", "ok": True, "detail": f"image {where} ({IMAGE})"})

    if not spec.model or "/" not in spec.model:
        checks.append({"name": "model", "ok": False, "detail": "model id must be 'org/name'"})
    else:
        ok, detail = _hf_check(spec)
        checks.append({"name": "model", "ok": ok, "detail": detail})
    return checks


def start(spec: Spec, deploy_id: str) -> str:
    if _runtime_strategy() == "native":
        _start_native(spec, deploy_id)
        return endpoint(deploy_id, spec)

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
    _stream_logs(log_path, cmd, env=runtime_env(spec))

    _log(deploy_id, "=== container started, following container logs ===")
    _follow_logs(deploy_id)

    return endpoint(deploy_id, spec)


def is_running(deploy_id: str) -> bool:
    if _native_running(deploy_id):
        return True
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
    if "installed on this machine" in marker:
        return "starting inference server"
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
    _stop_native(deploy_id)
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
