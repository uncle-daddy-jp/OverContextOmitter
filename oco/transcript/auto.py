"""自動判別ソース: 字幕テキストが取れれば aribcaption、取れなければ whisper。

字幕付き（フリーレン等）は速い字幕抽出、字幕なし（ダーティペア等）は ASR、を
利用者が config を切り替えずに自動で使い分ける。

※「字幕ストリームはあるが中身が空」= 放送に字幕テキストが乗っていない録画がある
  （例: 一部の TOKYO MX アニメ。管理データのみの 20byte パケットが並び、libaribcaption は
  タイミングだけの空 Dialogue を吐く）。ストリームの"有無"でなく"抽出できたテキスト行数"で
  分岐しないと、この録画で 0 行のままハードエラーになる（→ ASR に落とす）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..subtitles.ass_parser import Line

logger = logging.getLogger("oco.transcript.auto")


class AutoSource:
    def __init__(self, cfg):
        self._cfg = cfg

    def lines(self, media_path: str) -> list[Line]:
        from .aribcaption import AribCaptionSource

        p = Path(media_path)
        if p.suffix.lower() == ".ass":
            return AribCaptionSource().lines(media_path)

        # 動画: まず字幕ストリームの有無を見て、あれば ARIB 抽出を試す。
        from ..subtitles.extract import probe_subtitle_streams

        info = probe_subtitle_streams(media_path)
        if "Subtitle:" in info:
            try:
                lines = AribCaptionSource().lines(media_path)
            except Exception:
                logger.warning("ARIB字幕の抽出に失敗→whisper(ASR)にフォールバック: %s",
                               media_path, exc_info=True)
                lines = []
            if lines:
                return lines
            # 字幕ストリームはあるが本文テキストが空（管理データのみの録画）→ ASR に落とす。
            logger.info("字幕ストリームはあるが本文0行→whisper(ASR)にフォールバック: %s", media_path)

        from .whisper import WhisperSource

        return WhisperSource(self._cfg).lines(media_path)
