"""Anthropic ネイティブアダプタ（Claude）。

OpenAI 互換 shim は使わない（prompt caching / tool use / 品質のため）。
- 安定プレフィックス（system と stable_prefix）に cache_control: ephemeral を付け、
  毎話のベタ貼りプレフィックスをまるごとキャッシュ対象にする。
- sampling param（frequency/presence penalty）や budget_tokens は送らない
  （Claude 4.x は 400 になる）。暴走抑制はプロンプト接地 + adaptive thinking で担保。
"""

from __future__ import annotations

from ..config import LLMConfig


class AnthropicLLM:
    def __init__(self, cfg: LLMConfig):
        import anthropic  # 遅延 import

        self._cfg = cfg
        self._client = anthropic.Anthropic(api_key=cfg.api_key)

    def complete(
        self,
        *,
        system: str,
        stable_prefix: str,
        volatile: str,
        max_tokens: int | None = None,
    ) -> str:
        cfg = self._cfg
        # system は安定＝キャッシュ対象
        system_blocks = [
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
        ]
        # user content: [安定プレフィックス(キャッシュ), 揮発テール]
        content: list[dict] = []
        if stable_prefix:
            content.append(
                {
                    "type": "text",
                    "text": stable_prefix,
                    "cache_control": {"type": "ephemeral"},
                }
            )
        content.append({"type": "text", "text": volatile})

        resp = self._client.messages.create(
            model=cfg.model,
            max_tokens=max_tokens or cfg.max_tokens,
            system=system_blocks,
            messages=[{"role": "user", "content": content}],
        )
        return "".join(b.text for b in resp.content if b.type == "text").strip()
