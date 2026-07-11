"""台本ソースの共通インターフェース。"""

from __future__ import annotations

from typing import Protocol

from ..subtitles.ass_parser import Line


class TranscriptSource(Protocol):
    def lines(self, media_path: str) -> list[Line]:
        """メディアから時系列の Line リストを返す（CMマスク前・客観）。"""
        ...
