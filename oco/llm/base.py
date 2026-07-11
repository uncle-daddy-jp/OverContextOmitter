"""LLM アダプタの共通インターフェース。

モジュールが要求するのは text(chat) 1能力だけ（vision=look() は phase2）。

呼び出し形は「安定プレフィックス + 揮発テール」の2部構成に固定する：
  - stable_prefix … 毎話ほぼ不変（過去の全あらすじ＋感想）。prompt caching の対象。
  - volatile      … 今回ぶん（今回の台本＋指示）。必ず末尾＝プレフィックスを壊さない。

この分割により、話数が増えてもキャッシュが効いてコストが線形に伸びない。
（Anthropic=cache_control / ローカル=自動 prefix キャッシュ）
"""

from __future__ import annotations

from typing import Protocol


class TextLLM(Protocol):
    def complete(
        self,
        *,
        system: str,
        stable_prefix: str,
        volatile: str,
        max_tokens: int | None = None,
    ) -> str:
        """1ターン補完。system + stable_prefix + volatile を渡し本文テキストを返す。"""
        ...


class VisionLLM(Protocol):
    def describe(self, *, image_jpeg: bytes, prompt: str, max_tokens: int | None = None) -> str:
        """1枚の画像（JPEG bytes）について prompt に答える（look() / 場面説明）。"""
        ...
