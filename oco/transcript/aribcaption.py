"""ARIB字幕から台本を得る（既定）。

.ass はそのまま解析。.ts は ffmpeg(libaribcaption) で .ass 抽出してから解析。
近年アニメ/ドラマは字幕放送が多く、字幕onlyで内容理解＋話者割当は実用十分（実証済）。
字幕が無いメディアは whisper バックエンド（将来）に委ねる。
"""

from __future__ import annotations

from pathlib import Path

from ..subtitles.ass_parser import Line, parse_ass
from ..subtitles.extract import extract_ass


class AribCaptionSource:
    def lines(self, media_path: str) -> list[Line]:
        p = Path(media_path)
        suffix = p.suffix.lower()
        if suffix == ".ass":
            return parse_ass(p.read_text(encoding="utf-8", errors="replace"))
        if suffix == ".ts":
            sibling = p.with_suffix(".ass")
            if sibling.exists():
                return parse_ass(sibling.read_text(encoding="utf-8", errors="replace"))
            # 録画マウントは読み取り専用のことがある（NAS CIFS ro 等）→ 入力TSの隣には書けない。
            # .ass は一時ファイルへ抽出して使い捨てる（解析後の lines は SQLite に保存されるので
            # .ass を残す必要は無い）。
            import os
            import tempfile

            fd, tmp = tempfile.mkstemp(suffix=".ass")
            os.close(fd)
            try:
                extract_ass(p, tmp)
                return parse_ass(Path(tmp).read_text(encoding="utf-8", errors="replace"))
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        raise ValueError(f"aribcaption: 未対応のメディア拡張子: {media_path!r}")
