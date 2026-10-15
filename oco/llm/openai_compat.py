"""OpenAI 互換アダプタ（text / vision）。

vLLM / Ollama / LM Studio / llama.cpp（ローカル）＋ OpenAI / OpenRouter / Together
（クラウド）を base_url だけで一手にカバーする。

★思考(thinking)制御は llama.cpp + Gemma4 の仕様に従う（実機実測で確定）:
  - 効くのは `thinking_budget_tokens`（`thinking_token_budget`=vLLM名は無効・両送は無害）。
  - 思考トークンは max_tokens に含まれる → `max_tokens = 本文ぶん + budget` と加算。
  - ON/OFF は chat_template_kwargs:{enable_thinking}。skip_special_tokens=False で
    思考が reasoning_content に分離され content に漏れにくい。
  - budget はソフトリミット。小さすぎると思考が content に溢れる → 完走(-1)＋安全弁で根治。
"""

from __future__ import annotations

import re

from ..config import LLMConfig

# Gemma4 思考トークン `<|channel>thought` / `<|think|>` の保険除去（漏れ取りこぼし対策）
_CHANNEL_RE = re.compile(r"<\|?channel\|?>(?:thought)?|<channel\|>|<\|/?think\|>")


def _clean_channel(text: str) -> str:
    text = text or ""
    # 素の `</think>` が content に混じったら、その手前は思考の漏れ＝捨てる（2026-09-24 要約に英文の思考が残った）
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    return _CHANNEL_RE.sub("", text.replace("<think>", "")).strip()


class OpenAICompatLLM:
    def __init__(self, cfg: LLMConfig):
        from openai import OpenAI  # 遅延 import（anthropic だけ使う環境で不要に）

        self._cfg = cfg
        # ローカル(vLLM 等)は API キー不要だが SDK が非空を要求するので "EMPTY" を充てる
        self._client = OpenAI(base_url=cfg.base_url, api_key=cfg.api_key or "EMPTY")

    def _sampling(self) -> dict:
        cfg = self._cfg
        s: dict = {}
        if cfg.temperature is not None:
            s["temperature"] = cfg.temperature
        if cfg.frequency_penalty is not None:
            s["frequency_penalty"] = cfg.frequency_penalty
        if cfg.presence_penalty is not None:
            s["presence_penalty"] = cfg.presence_penalty
        return s

    def _create(self, messages, max_tokens, *, enable_thinking, thinking_budget=None):
        extra: dict = {"chat_template_kwargs": {"enable_thinking": enable_thinking}}
        if enable_thinking:
            extra["skip_special_tokens"] = False                 # 思考を reasoning_content へ分離
            if thinking_budget and thinking_budget > 0:
                extra["thinking_budget_tokens"] = thinking_budget  # llama.cpp 名（効く）
                extra["thinking_token_budget"] = thinking_budget   # vLLM 名（無効・両対応の保険）
        return self._client.chat.completions.create(
            model=self._cfg.model, messages=messages, max_tokens=max_tokens,
            extra_body=extra, **self._sampling(),
        )

    def complete(self, *, system: str, stable_prefix: str, volatile: str,
                 max_tokens: int | None = None) -> str:
        cfg = self._cfg
        # 安定プレフィックスを先頭、揮発テールを末尾に（自動 prefix キャッシュを効かせる）
        user = f"{stable_prefix}\n\n{volatile}" if stable_prefix else volatile
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        content_budget = max_tokens or cfg.max_tokens
        budget = cfg.thinking_budget

        if budget is None:
            # 思考制御なし（従来＝max_tokens を総量として送る）
            resp = self._client.chat.completions.create(
                model=cfg.model, messages=messages, max_tokens=content_budget, **self._sampling())
            return _clean_channel(resp.choices[0].message.content or "")

        if budget == 0:
            resp = self._create(messages, content_budget, enable_thinking=False)
            return _clean_channel(resp.choices[0].message.content or "")

        # thinking ON（完走 or ソフト上限）。max_tokens=本文ぶん + budget/headroom。
        if budget < 0:
            resp = self._create(messages, content_budget + cfg.completion_headroom,
                                 enable_thinking=True)
        else:
            resp = self._create(messages, content_budget + budget,
                                 enable_thinking=True, thinking_budget=budget)
        choice = resp.choices[0]
        text = _clean_channel(choice.message.content or "")
        # 安全弁: 思考が予算を食い切って本文が空 / length で切れた → thinking OFF で撃ち直し
        if not text or choice.finish_reason == "length":
            resp2 = self._create(messages, content_budget, enable_thinking=False)
            text = _clean_channel(resp2.choices[0].message.content or "") or text
        return text


class OpenAICompatVisionLLM:
    """OpenAI 互換の画像入力（Gemma マルチモーダル / Qwen-VL / Ollama 等）。

    フレーム説明は thinking OFF（doc §7 infer_scene_photo と同じ）＝速く本文を直出し。
    """

    def __init__(self, cfg: LLMConfig):
        from openai import OpenAI

        self._cfg = cfg
        self._client = OpenAI(base_url=cfg.base_url, api_key=cfg.api_key or "EMPTY")

    def describe(self, *, image_jpeg: bytes, prompt: str, max_tokens: int | None = None) -> str:
        import base64

        cfg = self._cfg
        b64 = base64.b64encode(image_jpeg).decode("ascii")
        url = f"data:image/jpeg;base64,{b64}"
        kwargs: dict = {
            "model": cfg.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": url}},
                    ],
                }
            ],
            "max_tokens": max_tokens or cfg.max_tokens,
            "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},  # 思考OFF＝高速
        }
        if cfg.temperature is not None:
            kwargs["temperature"] = cfg.temperature
        resp = self._client.chat.completions.create(**kwargs)
        return _clean_channel(resp.choices[0].message.content or "")