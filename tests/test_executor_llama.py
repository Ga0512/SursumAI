"""The llama runtime: model resolution, GGUF handling, and the command lines.

Nothing here starts a process or touches the network — these are the decisions
made before anything is launched, which is where a wrong answer costs the user
a multi-gigabyte download or a server that silently never comes up.
"""

import hashlib
import json

import pytest

from agent import executor_llama as ex
from core.spec import Spec


def _spec(model="org/model", **kw):
    return Spec(model=model, runtime="llama", **kw)


# ---- model ids ----

@pytest.mark.parametrize("model", [
    "Qwen/Qwen3-8B",
    "unsloth/Qwen3-30B-A3B-GGUF",
    "org_1/model.name-v2",
])
def test_valid_model_ids_pass(model):
    assert ex._validate_model_id(model) == model


@pytest.mark.parametrize("model", [
    "",
    "noslash",
    "org/model/extra",
    "../../etc/passwd",
    "org/model;rm -rf /",
    "org/model$(whoami)",
    "org /model",
])
def test_dangerous_or_malformed_model_ids_are_rejected(model):
    with pytest.raises(ex.TransportError, match="invalid model id"):
        ex._validate_model_id(model)


# ---- picking the GGUF ----

def test_the_preferred_quant_wins_over_file_order():
    files = ["model-Q8_0.gguf", "model-Q4_K_M.gguf", "model-Q2_K.gguf"]
    assert ex._pick_gguf(files) == "model-Q4_K_M.gguf"


def test_quant_preference_is_ordered():
    assert ex._pick_gguf(["m-Q6_K.gguf", "m-Q5_K_M.gguf"]) == "m-Q5_K_M.gguf"


def test_an_unknown_quant_still_gets_picked():
    assert ex._pick_gguf(["model-IQ3_XXS.gguf"]) == "model-IQ3_XXS.gguf"


def test_the_vision_encoder_is_never_picked_as_the_model():
    files = ["mmproj-F16.gguf", "model-Q4_K_M.gguf"]
    assert ex._pick_gguf(files) == "model-Q4_K_M.gguf"


def test_a_repo_with_only_an_mmproj_has_no_model():
    assert ex._pick_gguf(["mmproj-F16.gguf"]) is None


def test_a_repo_with_no_gguf_has_no_model():
    assert ex._pick_gguf(["config.json", "model.safetensors"]) is None


def test_the_mmproj_is_found_whatever_its_case():
    assert ex._pick_mmproj(["MMPROJ-F16.GGUF"]) == "MMPROJ-F16.GGUF"


def test_no_mmproj_means_no_vision():
    assert ex._pick_mmproj(["model-Q4_K_M.gguf"]) is None


# ---- resolve_model ----

class _FakeSibling:
    def __init__(self, name):
        self.rfilename = name


class _FakeInfo:
    def __init__(self, files):
        self.siblings = [_FakeSibling(f) for f in files]
        self.sha = "abc123"


def _fake_api(files, monkeypatch):
    class _Api:
        def __init__(self, token=None):
            pass

        def model_info(self, model):
            return _FakeInfo(files)

    monkeypatch.setattr(ex, "HfApi", _Api)


def test_a_vision_repo_resolves_to_model_plus_mmproj(monkeypatch):
    _fake_api(["model-Q4_K_M.gguf", "mmproj-F16.gguf"], monkeypatch)
    resolved = ex.resolve_model(_spec())
    assert resolved["gguf"] == "model-Q4_K_M.gguf"
    assert resolved["mmproj"] == "mmproj-F16.gguf"
    assert resolved["vision"] is True


def test_a_text_repo_resolves_without_vision(monkeypatch):
    _fake_api(["model-Q4_K_M.gguf"], monkeypatch)
    assert ex.resolve_model(_spec())["vision"] is False


def test_a_repo_without_gguf_fails_with_a_readable_reason(monkeypatch):
    _fake_api(["model.safetensors"], monkeypatch)
    with pytest.raises(ex.TransportError, match="no GGUF file found"):
        ex.resolve_model(_spec())


def test_a_missing_repo_says_so_plainly(monkeypatch):
    from huggingface_hub.errors import RepositoryNotFoundError

    # built without going through __init__: the signature of this exception
    # has changed across huggingface_hub versions and is not what is under test
    missing = RepositoryNotFoundError.__new__(RepositoryNotFoundError)

    class _Api:
        def __init__(self, token=None):
            pass

        def model_info(self, model):
            raise missing

    monkeypatch.setattr(ex, "HfApi", _Api)
    with pytest.raises(ex.TransportError, match="not found on Hugging Face"):
        ex.resolve_model(_spec())


def test_an_unreachable_hub_is_reported_separately(monkeypatch):
    class _Api:
        def __init__(self, token=None):
            pass

        def model_info(self, model):
            raise OSError("network down")

    monkeypatch.setattr(ex, "HfApi", _Api)
    with pytest.raises(ex.TransportError, match="could not reach Hugging Face"):
        ex.resolve_model(_spec())


# ---- GGUF validation ----

def test_a_real_gguf_is_accepted(tmp_path):
    path = tmp_path / "m.gguf"
    path.write_bytes(b"GGUF" + b"\x00" * 32)
    assert ex._is_gguf(path)


def test_an_html_error_page_is_not_a_gguf(tmp_path):
    """A failed CDN download often lands as HTML with a .gguf name."""
    path = tmp_path / "m.gguf"
    path.write_bytes(b"<!DOCTYPE html><html>404</html>")
    assert not ex._is_gguf(path)


def test_a_missing_file_is_not_a_gguf(tmp_path):
    assert not ex._is_gguf(tmp_path / "nope.gguf")


def test_sha256_matches_hashlib(tmp_path):
    path = tmp_path / "blob"
    path.write_bytes(b"some bytes" * 10_000)  # bigger than one read chunk
    assert ex._sha256(path) == hashlib.sha256(path.read_bytes()).hexdigest()


# ---- release assets ----

def _on(monkeypatch, system, machine="x86_64", strategy="cpu"):
    """Pretend we are on another OS — these tests must give the same answer
    whatever machine CI happens to run on."""
    monkeypatch.setattr(ex.platform, "system", lambda: system)
    monkeypatch.setattr(ex.platform, "machine", lambda: machine)
    monkeypatch.setattr(ex, "_runtime_strategy", lambda: strategy)


@pytest.mark.parametrize("system,machine,strategy,expected", [
    ("Linux", "x86_64", "cpu", "ubuntu-x64"),
    ("Linux", "aarch64", "cpu", "ubuntu-arm64"),
    ("Linux", "x86_64", "cuda", "ubuntu-cuda-x64"),
    ("Linux", "x86_64", "vulkan", "ubuntu-vulkan-x64"),
    ("Darwin", "arm64", "cpu", "macos-arm64"),
    ("Darwin", "x86_64", "cpu", "macos-x64"),
    ("Windows", "AMD64", "cpu", "win-cpu-x64"),
])
def test_platform_key(monkeypatch, system, machine, strategy, expected):
    _on(monkeypatch, system, machine, strategy)
    assert ex._platform_key() == expected


def test_an_unsupported_platform_is_refused(monkeypatch):
    _on(monkeypatch, "Plan9")
    with pytest.raises(ex.TransportError, match="unsupported platform"):
        ex._platform_key()


def test_the_cuda_build_comes_from_the_pinned_fork(monkeypatch):
    _on(monkeypatch, "Linux", "x86_64", "cuda")
    assert ex._bin_repo() == ex.BIN_CUDA_REPO
    assert ex._bin_version() == ex.BIN_CUDA_TAG
    assert ex._asset_name() == ex.BIN_CUDA_ASSET


def test_every_other_build_comes_from_the_official_repo(monkeypatch):
    _on(monkeypatch, "Linux", "x86_64", "cpu")
    assert ex._bin_repo() == ex.BIN_REPO
    assert ex._bin_version() == ex.BIN_VERSION


def test_the_asset_name_carries_the_pinned_version(monkeypatch):
    monkeypatch.setattr(ex, "_runtime_strategy", lambda: "cpu")
    monkeypatch.setattr(ex, "_platform_key", lambda: "ubuntu-x64")
    assert ex._asset_name() == f"llama-{ex.BIN_VERSION}-bin-ubuntu-x64.tar.gz"


def test_windows_assets_are_zips(monkeypatch):
    monkeypatch.setattr(ex, "_runtime_strategy", lambda: "cpu")
    monkeypatch.setattr(ex, "_platform_key", lambda: "win-cpu-x64")
    assert ex._asset_name().endswith(".zip")


def test_the_release_digest_is_read_from_the_api(monkeypatch):
    monkeypatch.setattr(ex, "_asset_name", lambda: "wanted.tar.gz")
    monkeypatch.setattr(ex, "_bin_repo", lambda: "org/repo")
    monkeypatch.setattr(ex, "_bin_version", lambda: "b1")
    payload = {"assets": [
        {"name": "other.tar.gz", "digest": "sha256:" + "0" * 64},
        {"name": "wanted.tar.gz", "digest": "sha256:" + "a" * 64},
    ]}
    _fake_urlopen(monkeypatch, payload)
    assert ex._release_sha256() == "a" * 64


def test_a_release_without_our_asset_is_an_error(monkeypatch):
    monkeypatch.setattr(ex, "_asset_name", lambda: "wanted.tar.gz")
    monkeypatch.setattr(ex, "_bin_repo", lambda: "org/repo")
    monkeypatch.setattr(ex, "_bin_version", lambda: "b1")
    _fake_urlopen(monkeypatch, {"assets": []})
    with pytest.raises(ex.TransportError, match="has no asset"):
        ex._release_sha256()


def _fake_urlopen(monkeypatch, payload):
    class _Resp:
        def read(self):
            return json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(ex.urllib.request, "urlopen", lambda *a, **kw: _Resp())


# ---- command lines ----

@pytest.fixture
def paths(tmp_path):
    gguf = tmp_path / "model-Q4_K_M.gguf"
    gguf.write_bytes(b"GGUF")
    return {"gguf": str(gguf)}


def test_the_binary_command_binds_the_allocated_port(paths):
    cmd = ex._binary_build_cmd(_spec(port=9042), "id", paths, "llama-server")
    assert cmd[cmd.index("--port") + 1] == "9042"


def test_the_docker_command_maps_the_allocated_port(paths):
    cmd = ex._docker_build_cmd(_spec(port=9042), "id", paths)
    assert "9042:8080" in cmd


def test_metrics_are_always_enabled(paths):
    """The dashboard has nothing to show without them."""
    assert "--metrics" in ex._binary_build_cmd(_spec(), "id", paths, "llama-server")
    assert "--metrics" in ex._docker_build_cmd(_spec(), "id", paths)


def test_the_context_size_never_drops_below_the_floor(paths):
    cmd = ex._binary_build_cmd(_spec(max_model_len=1), "id", paths, "llama-server")
    assert int(cmd[cmd.index("--ctx-size") + 1]) >= 128


def test_a_vision_model_passes_its_mmproj(paths, tmp_path):
    mmproj = tmp_path / "mmproj-F16.gguf"
    mmproj.write_bytes(b"GGUF")
    with_vision = {**paths, "mmproj": str(mmproj)}
    cmd = ex._binary_build_cmd(_spec(), "id", with_vision, "llama-server")
    assert cmd[cmd.index("--mmproj") + 1] == str(mmproj)


def test_a_text_model_passes_no_mmproj(paths):
    assert "--mmproj" not in ex._binary_build_cmd(_spec(), "id", paths, "llama-server")


def test_gpu_layers_are_offloaded_only_when_the_gpu_can_be_used(paths, monkeypatch):
    monkeypatch.setattr(ex, "_runtime_strategy", lambda: "cuda")
    assert "-ngl" in ex._binary_build_cmd(_spec(), "id", paths, "llama-server")
    monkeypatch.setattr(ex, "_runtime_strategy", lambda: "cpu")
    assert "-ngl" not in ex._binary_build_cmd(_spec(), "id", paths, "llama-server")


def test_the_model_directory_is_mounted_read_only(paths):
    cmd = ex._docker_build_cmd(_spec(), "id", paths)
    assert any(arg.endswith(":/models:ro") for arg in cmd)


def test_container_names_are_derived_from_the_deploy_id():
    assert ex._docker_name("deadbeefcafe1234") == "deploy-deadbeefcafe"


def test_the_api_key_reaches_both_runtimes(paths):
    spec = _spec(api_key="sk-sursum-secret")
    for cmd in (ex._binary_build_cmd(spec, "id", paths, "llama-server"),
                ex._docker_build_cmd(spec, "id", paths)):
        assert cmd[cmd.index("--api-key") + 1] == "sk-sursum-secret"


def test_no_api_key_means_no_flag(paths):
    assert "--api-key" not in ex._binary_build_cmd(_spec(), "id", paths, "llama-server")


# ---- logs ----

def test_logs_reports_plainly_when_there_is_nothing_yet(monkeypatch, tmp_path):
    monkeypatch.setattr(ex, "LOGS_DIR", tmp_path)
    assert ex.logs("never-started") == "(no log yet)"


def test_logs_returns_the_tail(monkeypatch, tmp_path):
    monkeypatch.setattr(ex, "LOGS_DIR", tmp_path)
    (tmp_path / "abc.log").write_text("\n".join(f"line {i}" for i in range(100)))
    out = ex.logs("abc", tail=5)
    assert out.strip().splitlines() == [f"line {i}" for i in range(95, 100)]
