"""人格は差し替え式（コアは客観固定・主観は人格ごと）。

同梱の .txt（例: default）を使うか、外部の消費者が同じインターフェース
（persona_id → 人格テキスト、または persona_prompt で本文直渡し）で差し込む。
"""

from __future__ import annotations

import re
from pathlib import Path

_DIR = Path(__file__).parent
# persona_id は識別子（slug）限定。任意パスは受け付けない（任意ファイル読み防止）。
# 任意本文の人格は persona_prompt（本文直渡し）で渡す＝外部パスを解釈しない。
_SLUG_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def load_persona(persona_id: str) -> str:
    """persona_id（slug）を同梱の人格テキストに解決する。

    persona_id はリクエスト由来（API/MCP）なので、パスとしては絶対に解釈しない。
    カスタム人格は persona_prompt（本文）で渡すこと。
    """
    if not _SLUG_RE.match(persona_id or ""):
        raise ValueError(
            f"不正な persona_id: {persona_id!r}（英数・_-のみ）。"
            "カスタム人格は persona_prompt で本文を渡す。"
        )
    builtin = _DIR / f"{persona_id}.txt"
    if builtin.exists():
        return builtin.read_text(encoding="utf-8").strip()
    raise FileNotFoundError(
        f"人格が見つからない: {persona_id!r}（同梱: {[f.stem for f in _DIR.glob('*.txt')]}）"
    )
