from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class SpecError(Exception):
    pass


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

    def validate(self) -> None:
        if not self.model or not isinstance(self.model, str):
            raise SpecError("model is required")
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
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Spec":
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in data.items() if k in allowed})
