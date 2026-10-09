"""The vLLM command line, and the Advanced overrides that reach it.

vLLM runs as a container, so what matters is the argument list: a wrong flag
shows up as a container that exits in a second with a line of C++.
"""
from pathlib import Path

import pytest

from agent import executor
from core.spec import Spec, SpecError


def cmd(**kw) -> list[str]:
    spec = Spec(model="org/m", runtime="vllm", **kw)
    spec.validate()
    return executor.build_cmd(spec, "d" * 32)


def test_a_deploy_with_no_overrides_looks_exactly_as_before():
    line = " ".join(cmd())
    for flag in ("--kv-cache-dtype", "--quantization", "--trust-remote-code",
                 "--max-num-seqs", "--cpu-offload-gb"):
        assert flag not in line


def test_a_quantized_repo_can_finally_be_asked_for():
    assert cmd(quantization="awq")[-1] == "awq"


def test_trust_remote_code_is_a_flag_with_no_value():
    """Several 2026 models ship their own modelling code and refuse to load
    without it, and it is off unless asked for: it runs the repo's Python."""
    assert "--trust-remote-code" in cmd(trust_remote_code=True)
    assert "--trust-remote-code" not in cmd()


def test_the_kv_cache_choice_is_translated_to_what_vllm_calls_it():
    """The modal says q8_0 for both runtimes; vLLM spells that fp8."""
    line = cmd(kv_cache="q8_0")
    assert line[line.index("--kv-cache-dtype") + 1] == "fp8"
    assert "q8_0" not in " ".join(line)


def test_parallel_conversations_map_to_max_num_seqs():
    assert cmd(parallel=8)[cmd(parallel=8).index("--max-num-seqs") + 1] == "8"
    # the vLLM-specific field wins when both are given
    line = cmd(parallel=8, max_num_seqs=32)
    assert line[line.index("--max-num-seqs") + 1] == "32"


def test_cpu_offload_is_only_asked_for_when_a_number_was_given():
    """0 means "do not offload", which is vLLM's own default: sending
    `--cpu-offload-gb 0` would say the same thing in a flag that can go stale."""
    line = cmd(cpu_offload_gb=8)
    assert line[line.index("--cpu-offload-gb") + 1] == "8"
    assert "--cpu-offload-gb" not in cmd() and "--cpu-offload-gb" not in cmd(cpu_offload_gb=0)


@pytest.mark.parametrize("kw,message", [
    ({"quantization": "awq2"}, "quantization"),
    ({"max_num_seqs": 0}, "max_num_seqs"),
    ({"cpu_offload_gb": 999}, "CPU offload"),
    ({"kv_cache": "nope"}, "KV cache"),
])
def test_a_value_the_server_would_choke_on_is_refused_here(kw, message):
    with pytest.raises(SpecError, match=message):
        cmd(**kw)


def test_the_secrets_still_travel_by_environment_not_argv():
    line = " ".join(cmd(api_key="sk-internal-abc", hf_token="hf_secret",
                        quantization="awq", trust_remote_code=True))
    assert "sk-internal-abc" not in line and "hf_secret" not in line


# ---- every flag we can emit exists in the image we pin ----

def _known(name: str) -> set[str]:
    path = Path(__file__).parent / f"flags_{name}.txt"
    return {line.strip() for line in path.read_text().splitlines() if line.strip()}


def test_no_flag_we_send_is_unknown_to_the_pinned_vllm():
    """`--swap-space` was real in vLLM 0.9 and gone in 0.21, and the deploy
    died with "unrecognized arguments" on a real GPU. The lists next to this
    file are `serve --help=all` of the pinned image and of the pinned llama.cpp
    image; regenerate them when either pin moves."""
    line = executor.build_cmd(
        Spec(model="org/m", runtime="vllm", api_key="k", kv_cache="q8_0", parallel=4,
             quantization="awq", trust_remote_code=True, max_num_seqs=32, cpu_offload_gb=8),
        "d" * 32)
    image = line.index("vllm/vllm-openai:v0.21.0")
    server_flags = {a for a in line[image:] if a.startswith("--")}
    assert server_flags <= _known("vllm"), server_flags - _known("vllm")


# ---- a machine that already has vLLM does not need Docker ----

def test_a_pod_with_vllm_installed_runs_it_without_docker(monkeypatch):
    """A RunPod pod IS a container: Docker inside it is not an option, and its
    image usually ships vLLM. Refusing to deploy there was the app knowing
    better than the machine."""
    monkeypatch.setattr(executor, "_docker_here", lambda: False)
    monkeypatch.setattr(executor, "vllm_here", lambda: "/usr/local/bin/vllm")
    assert executor._runtime_strategy() == "native"

    line = executor.build_native_cmd(
        Spec(model="org/m", runtime="vllm", api_key="k", max_model_len=8192), "d" * 32)
    assert line[0] == "/usr/local/bin/vllm" and line[1] == "serve" and line[2] == "org/m"
    assert "--port" in line and line[line.index("--max-model-len") + 1] == "8192"
    assert "docker" not in " ".join(line)


def test_docker_wins_when_both_are_there(monkeypatch):
    """The image has its CUDA and PyTorch already matched; a local install is
    the fallback, not the preference."""
    monkeypatch.setattr(executor, "_docker_here", lambda: True)
    monkeypatch.setattr(executor, "vllm_here", lambda: "/usr/local/bin/vllm")
    assert executor._runtime_strategy() == "docker"


def test_with_neither_the_preflight_says_so_in_one_sentence(monkeypatch):
    monkeypatch.setattr(executor, "_docker_here", lambda: False)
    monkeypatch.setattr(executor, "vllm_here", lambda: "")
    monkeypatch.setattr(executor, "_gpu_available", lambda: True)
    monkeypatch.setattr(executor, "_gpu_count", lambda: 1)
    monkeypatch.setattr(executor, "_hf_check", lambda spec: (True, "found"))

    checks = {c["name"]: c for c in executor.preflight(Spec(model="org/m", runtime="vllm"))}
    assert checks["runtime"]["ok"] is False
    assert "neither Docker nor vLLM" in checks["runtime"]["detail"]
    assert "image" not in checks          # nothing is going to be pulled


def test_the_native_server_still_gets_its_key_by_environment(monkeypatch):
    monkeypatch.setattr(executor, "vllm_here", lambda: "/usr/local/bin/vllm")
    spec = Spec(model="org/m", runtime="vllm", api_key="sk-internal-abc", hf_token="hf_secret")
    line = " ".join(executor.build_native_cmd(spec, "d" * 32))
    assert "sk-internal-abc" not in line and "hf_secret" not in line
    env = executor.runtime_env(spec)
    assert env["VLLM_API_KEY"] == "sk-internal-abc" and env["HF_TOKEN"] == "hf_secret"


# ---- look before downloading ----

def test_the_machine_s_own_vllm_is_used_before_ours(monkeypatch):
    """A pod image that ships vLLM should never trigger an 8 GB download."""
    from agent import runtimes

    monkeypatch.setattr(runtimes.shutil, "which", lambda name: "/usr/local/bin/vllm"
                        if name == "vllm" else None)
    assert runtimes.vllm_command() == ["/usr/local/bin/vllm"]


def test_our_own_environment_is_the_last_resort(monkeypatch, tmp_path):
    from agent import runtimes

    venv = tmp_path / "vllm-venv"
    python = venv / ("Scripts" if runtimes.sys.platform == "win32" else "bin") / "python"
    python.parent.mkdir(parents=True)
    python.write_text("")
    monkeypatch.setattr(runtimes, "VLLM_VENV", venv)
    monkeypatch.setattr(runtimes.shutil, "which", lambda name: None)
    monkeypatch.setattr(runtimes, "_package",
                        lambda interpreter, name: "0.21.0" if str(interpreter) == str(python) else "")

    assert runtimes.vllm_command() == [str(python), "-m", "vllm"]


def test_nothing_installed_is_an_empty_answer_not_an_error(monkeypatch, tmp_path):
    from agent import runtimes

    monkeypatch.setattr(runtimes, "VLLM_VENV", tmp_path / "nope")
    monkeypatch.setattr(runtimes.shutil, "which", lambda name: None)
    monkeypatch.setattr(runtimes, "_package", lambda interpreter, name: "")
    assert runtimes.vllm_command() == []


def test_the_report_says_what_is_there_without_installing_anything(monkeypatch, tmp_path):
    from agent import runtimes

    monkeypatch.setattr(runtimes, "VLLM_VENV", tmp_path / "nope")
    monkeypatch.setattr(runtimes.shutil, "which", lambda name: None)
    monkeypatch.setattr(runtimes, "_package", lambda interpreter, name: "")
    monkeypatch.setattr(runtimes, "_torch", lambda interpreter: {})
    monkeypatch.setattr(runtimes, "_driver_cuda", lambda: "580.00")

    r = runtimes.report()
    assert r["vllm"]["present"] is False and r["torch"]["present"] is False
    assert r["driver"] == "580.00" and r["vllm"]["needs_gb"] > 0
