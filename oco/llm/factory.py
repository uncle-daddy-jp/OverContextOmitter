"""config から TextLLM を組み立てる。プロバイダ名だけで切り替わる。"""

from __future__ import annotations

from ..config import LLMConfig
from .base import TextLLM, VisionLLM


def build_text_llm(cfg: LLMConfig) -> TextLLM:
    if cfg.provider == "openai_compat":
        from .openai_compat import OpenAICompatLLM

        return OpenAICompatLLM(cfg)
    if cfg.provider == "anthropic":
        from .anthropic_native import AnthropicLLM

        return AnthropicLLM(cfg)
    raise ValueError(
        f"未知の LLM provider: {cfg.provider!r}（openai_compat | anthropic）"
    )


def build_vision_llm(cfg: LLMConfig) -> VisionLLM:
    if cfg.provider == "openai_compat":
        from .openai_compat import OpenAICompatVisionLLM

        return OpenAICompatVisionLLM(cfg)
    raise ValueError(
        f"vision 未対応の provider: {cfg.provider!r}（今は openai_compat のみ）"
    )
