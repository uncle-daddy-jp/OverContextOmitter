"""台本ソースの共通インターフェース。"""

from __future__ import annotations

from typing import Protocol

from ..subtitles.ass_parser import Line


class TranscriptSource(Protocol):
    # 実際に使われた backend 名（"aribcaption" | "whisper"）。auto は lines() 実行後に確定する。
    # 後段プロンプトの ASR警告（音声認識の誤変換対策）を出し分けるために pipeline が読む。
    used_backend: str

    def lines(self, media_path: str) -> list[Line]:
        """メディアから時系列の Line リストを返す（CMマスク前・客観）。"""
        ...
