"""Spec is the contract between the central and the agent — everything the
agent will turn into a docker/llama-server command line goes through here."""

import pytest

from core.spec import Spec, SpecError


def test_defaults_are_valid():
    Spec(model="Qwen/Qwen3-8B").validate()


def test_model_is_required():
    with pytest.raises(SpecError):
        Spec(model="").validate()


@pytest.mark.parametrize("runtime", ["vllm", "llama"])
def test_supported_runtimes(runtime):
    Spec(model="org/m", runtime=runtime).validate()


def test_unknown_runtime_is_rejected():
    with pytest.raises(SpecError):
        Spec(model="org/m", runtime="ollama").validate()


def test_unknown_target_is_rejected():
    with pytest.raises(SpecError):
        Spec(model="org/m", target="gcp").validate()


@pytest.mark.parametrize("kwargs", [
    {"gpus": 0},
    {"nodes": 0},
    {"gpu_memory_utilization": 0},
    {"gpu_memory_utilization": 1.5},
    {"max_model_len": 0},
    {"max_tokens": 0},
    {"temperature": -0.1},
    {"temperature": 2.1},
])
def test_out_of_range_values_are_rejected(kwargs):
    with pytest.raises(SpecError):
        Spec(model="org/m", **kwargs).validate()


@pytest.mark.parametrize("port", [8999, 9100, 8001])
def test_port_must_be_in_the_deploy_range(port):
    with pytest.raises(SpecError):
        Spec(model="org/m", port=port).validate()


@pytest.mark.parametrize("port", [9000, 9050, 9099])
def test_ports_inside_the_deploy_range_are_accepted(port):
    Spec(model="org/m", port=port).validate()


def test_roundtrip_through_dict_preserves_every_field():
    spec = Spec(model="org/m", runtime="llama", gpus=2, max_model_len=4096,
                temperature=0.7, hf_token="hf_x", api_key="sk-sursum-abc", port=9001)
    assert Spec.from_dict(spec.to_dict()) == spec


def test_from_dict_ignores_unknown_keys():
    """The agent must not blow up when a newer central sends extra fields."""
    spec = Spec.from_dict({"model": "org/m", "something_new": True})
    assert spec.model == "org/m"


def test_api_key_travels_to_the_agent():
    assert Spec(model="org/m", api_key="sk-sursum-abc").to_dict()["api_key"] == "sk-sursum-abc"
