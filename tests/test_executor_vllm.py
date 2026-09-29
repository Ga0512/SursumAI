"""The vLLM command line, and the Advanced overrides that reach it.

vLLM runs as a container, so what matters is the argument list: a wrong flag
shows up as a container that exits in a second with a line of C++.
"""
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
                 "--max-num-seqs", "--swap-space"):
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


def test_swap_space_of_zero_is_a_real_choice_not_an_unset_field():
    """0 turns swapping off; None leaves vLLM's own default."""
    assert cmd(swap_space=0)[cmd(swap_space=0).index("--swap-space") + 1] == "0"
    assert "--swap-space" not in cmd()


@pytest.mark.parametrize("kw,message", [
    ({"quantization": "awq2"}, "quantization"),
    ({"max_num_seqs": 0}, "max_num_seqs"),
    ({"swap_space": 999}, "swap space"),
    ({"kv_cache": "nope"}, "KV cache"),
])
def test_a_value_the_server_would_choke_on_is_refused_here(kw, message):
    with pytest.raises(SpecError, match=message):
        cmd(**kw)


def test_the_secrets_still_travel_by_environment_not_argv():
    line = " ".join(cmd(api_key="sk-internal-abc", hf_token="hf_secret",
                        quantization="awq", trust_remote_code=True))
    assert "sk-internal-abc" not in line and "hf_secret" not in line
