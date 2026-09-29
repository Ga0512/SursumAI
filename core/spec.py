from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class SpecError(Exception):
    pass


# What the runtimes accept, kept here so the modal, the API and the executors
# never disagree about it.
KV_CACHE_TYPES = ("f16", "q8_0", "q4_0")
VLLM_QUANTIZATIONS = ("awq", "gptq", "fp8", "bitsandbytes", "gguf")


@dataclass
class Spec:
    model: str
    runtime: str = "vllm"  # vllm | llama
    target: str = "local"  # local | aws
    gpus: int = 1
    nodes: int = 1
    gpu_memory_utilization: float = 0.50
    max_model_len: int = 300
    max_tokens: int = 2048
    temperature: float = 0.0
    hf_token: str = ""
    api_key: str = ""  # bearer required by the deploy's own endpoint
    port: int | None = None  # 9000-9099; None = auto (hash do deploy_id)

    # --- Advanced. None everywhere means "let the runtime decide", which is
    # what almost every deploy uses; a value here is a deliberate override.
    name: str = ""                     # what the user calls it, "Qwen 27B fast"
    kv_cache: str = ""                 # "", f16, q8_0, q4_0 — halves context memory
    parallel: int | None = None        # how many conversations at once
    top_p: float | None = None
    repeat_penalty: float | None = None
    # llama.cpp only
    gpu_layers: str = ""               # "" = auto (splits GPU/CPU), or a number
    threads: int | None = None
    flash_attn: bool | None = None
    # vLLM only
    quantization: str = ""             # awq, gptq, fp8, …
    trust_remote_code: bool = False
    max_num_seqs: int | None = None
    swap_space: int | None = None      # GiB of CPU memory for swapped-out blocks

    def validate(self) -> None:
        if not self.model or not isinstance(self.model, str):
            raise SpecError("model is required")
        if self.runtime == "vllm" and ":" in self.model:
            raise SpecError("choosing a quantization (':Q4_K_M') is for GGUF models on llama.cpp")
        if self.runtime not in ("vllm", "llama"):
            raise SpecError("runtime must be 'vllm' or 'llama'")
        if self.target not in ("local", "aws"):
            raise SpecError("target must be 'local' or 'aws'")
        if self.gpus < 1:
            raise SpecError("gpus must be >= 1")
        if self.nodes < 1:
            raise SpecError("nodes must be >= 1")
        if not 0 < self.gpu_memory_utilization <= 1:
            raise SpecError("gpu_memory_utilization must be between 0 and 1")
        if self.max_model_len < 1:
            raise SpecError("max_model_len must be >= 1")
        if self.max_tokens < 1:
            raise SpecError("max_tokens must be >= 1")
        if not 0 <= self.temperature <= 2:
            raise SpecError("temperature must be between 0 and 2")
        if self.api_key and not isinstance(self.api_key, str):
            raise SpecError("api_key must be a string")
        if self.port is not None:
            if not 9000 <= self.port <= 9099:
                raise SpecError("port must be between 9000 and 9099")
        self._validate_advanced()

    def _validate_advanced(self) -> None:
        """The overrides. Each one is refused here rather than on the machine:
        a bad value has to read as a sentence in the modal, not as a container
        that exits in a second with a line of C++."""
        if len(self.name) > 60:
            raise SpecError("the name can have at most 60 characters")
        if self.kv_cache and self.kv_cache not in KV_CACHE_TYPES:
            raise SpecError(f"KV cache must be one of: {', '.join(KV_CACHE_TYPES)}")
        if self.parallel is not None and not 1 <= self.parallel <= 256:
            raise SpecError("parallel conversations must be between 1 and 256")
        if self.top_p is not None and not 0 < self.top_p <= 1:
            raise SpecError("top_p must be between 0 and 1")
        if self.repeat_penalty is not None and not 0 < self.repeat_penalty <= 5:
            raise SpecError("repeat penalty must be between 0 and 5")
        if self.runtime == "llama":
            if self.gpu_layers and self.gpu_layers != "auto" and not self.gpu_layers.isdigit():
                raise SpecError("GPU layers must be a number, or 'auto'")
            if self.threads is not None and not 1 <= self.threads <= 256:
                raise SpecError("threads must be between 1 and 256")
        elif self.runtime == "vllm":
            if self.quantization and self.quantization not in VLLM_QUANTIZATIONS:
                raise SpecError(f"quantization must be one of: {', '.join(VLLM_QUANTIZATIONS)}")
            if self.max_num_seqs is not None and not 1 <= self.max_num_seqs <= 1024:
                raise SpecError("max_num_seqs must be between 1 and 1024")
            if self.swap_space is not None and not 0 <= self.swap_space <= 128:
                raise SpecError("swap space must be between 0 and 128 GiB")

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "runtime": self.runtime,
            "target": self.target,
            "gpus": self.gpus,
            "nodes": self.nodes,
            "gpu_memory_utilization": self.gpu_memory_utilization,
            "max_model_len": self.max_model_len,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "hf_token": self.hf_token,
            "api_key": self.api_key,
            "port": self.port,
            "name": self.name,
            "kv_cache": self.kv_cache,
            "parallel": self.parallel,
            "top_p": self.top_p,
            "repeat_penalty": self.repeat_penalty,
            "gpu_layers": self.gpu_layers,
            "threads": self.threads,
            "flash_attn": self.flash_attn,
            "quantization": self.quantization,
            "trust_remote_code": self.trust_remote_code,
            "max_num_seqs": self.max_num_seqs,
            "swap_space": self.swap_space,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Spec":
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in data.items() if k in allowed})
