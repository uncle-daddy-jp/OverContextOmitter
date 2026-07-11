"""config から TranscriptSource を組み立てる。"""

from __future__ import annotations

from .base import TranscriptSource


def build_transcript_source(cfg) -> TranscriptSource:
    backend = getattr(cfg, "transcript_backend", "aribcaption")
    if backend == "aribcaption":
        from .aribcaption import AribCaptionSource

        return AribCaptionSource()
    if backend == "whisper":
        from .whisper import WhisperSource

        return WhisperSource(cfg)
    if backend == "auto":
        from .auto import AutoSource

        return AutoSource(cfg)
    raise ValueError(
        f"未知の transcript backend: {backend!r}（aribcaption | whisper | auto）"
    )
