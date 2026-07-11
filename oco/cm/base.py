"""CM 検出のインターフェースとマスク処理（モジュール所有）。

TS は無改変（物理カットしない）。CM レンジを検出して、テキスト段では
そのレンジに入るセリフ行を落とす（時刻でスキップ）。

検出エンジンはプラガブル（既定 JL。後で comskip 等に差替可）。誤検出は安い
（本編数秒落ち / CM数秒残り程度）。テキスト先行なので取りこぼし CM が混じっても
LLM が低信号として流す。完璧な CM 除去は不要。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import CMConfig
    from ..subtitles.ass_parser import Line


@dataclass
class CMRange:
    start: float  # 秒
    end: float    # 秒

    def contains(self, t: float) -> bool:
        return self.start <= t < self.end


class CMProvider(Protocol):
    def ranges(self, media_path: str) -> list[CMRange]:
        """media（TS等）から CM レンジ表を返す。"""
        ...


class NullCMProvider:
    """CM 検出をしない（既定）。まず字幕だけで回すとき用。"""

    def ranges(self, media_path: str) -> list[CMRange]:
        return []


def mask_lines(lines: list["Line"], ranges: list[CMRange]) -> list["Line"]:
    """CM レンジに開始時刻が入るセリフ行を落とす。"""
    if not ranges:
        return lines
    return [ln for ln in lines if not any(r.contains(ln.start) for r in ranges)]


def build_cm_provider(cfg: "CMConfig") -> CMProvider:
    if cfg.backend == "none":
        return NullCMProvider()
    if cfg.backend == "jl":
        from .jl_backend import JLCMProvider

        return JLCMProvider(cfg)
    if cfg.backend == "chapter":
        from .jl_backend import ChapterCMProvider

        return ChapterCMProvider(cfg)
    raise ValueError(f"未知の CM backend: {cfg.backend!r}（none | jl | chapter）")
