"""台本ソース（差し替え式）。

メディアから「セリフ行（Line）」を得る経路を抜き差し可能にする。
- aribcaption … ARIB字幕(.ass)から（実装済・既定）
- whisper     … 字幕なしメディアの音声書き起こし（ASR・将来）

LLMプロバイダ/CMバックエンドと同じ「プラガブルbackend」パターン。
"""

from .base import TranscriptSource
from .factory import build_transcript_source

__all__ = ["TranscriptSource", "build_transcript_source"]
