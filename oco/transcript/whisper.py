"""音声書き起こし（ASR/Whisper）バックエンド ── 字幕なしメディア対応。

faster-whisper（CTranslate2）で動画の音声から Line（time, text）を起こす。
- 話者は当面 None（話者不明が誠実。話者分離=声紋等は別課題・後段）。
- CMマスク（cm.ranges→mask_lines）・人物相関・映像scene は現行のまま乗る。
- ローカルにGPUが無ければ CPU(int8)。model/device は config で可変
  （tiny/base/small/medium/large-v3、cpu/cuda）。大きいほど高精度・遅い。

字幕付きメディアは aribcaption バックオン（速い）。字幕なしだけ ASR。

★放送 TS(.ts) の音声は faster-whisper(PyAV) が直接デコードできないことがある
  （duration 00:00 と誤検出→0行になる。TOKYO MX 等の多重プログラム TS で発生）。
  なので ffmpeg で 16kHz mono WAV に前抽出してから faster-whisper に渡す＝
  .ts/.mp4/.mkv どれでも確実にデコードできる（.wav 経由は実証済）。
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile

from ..subtitles.ass_parser import Line

logger = logging.getLogger("oco.whisper")


class WhisperSource:
    def __init__(self, cfg):
        self._cfg = cfg
        self._model = None

    def _model_obj(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            logger.info(
                "whisper モデル読込: %s (device=%s compute=%s)",
                self._cfg.whisper_model, self._cfg.whisper_device, self._cfg.whisper_compute_type,
            )
            self._model = WhisperModel(
                self._cfg.whisper_model,
                device=self._cfg.whisper_device,
                compute_type=self._cfg.whisper_compute_type,
            )
        return self._model

    @staticmethod
    def _extract_audio(media_path: str) -> tuple[str, bool]:
        """動画から 16kHz mono WAV を temp に前抽出する。戻り: (音声パス, 一時ファイルか)。

        放送 TS を faster-whisper(PyAV) に直接渡すと音声 0 秒になる問題を回避する。
        抽出に失敗したら元パスを返し、faster-whisper の直接デコードに委ねる（誠実な劣化）。
        """
        fd, wav = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        cmd = [
            "ffmpeg", "-hide_banner", "-y",
            "-i", str(media_path),
            "-vn", "-ac", "1", "-ar", "16000", "-f", "wav",
            wav,
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True)
            if os.path.getsize(wav) > 1024:  # 空/ほぼ空の wav は失敗扱い
                return wav, True
            logger.warning("whisper 音声抽出が空: %s", media_path)
        except Exception:  # noqa: BLE001 — ffmpeg 失敗は直接デコードにフォールバック
            logger.warning("whisper 音声抽出に失敗→元メディアを直接デコードに委ねる: %s",
                           media_path, exc_info=True)
        try:
            os.remove(wav)
        except OSError:
            pass
        return str(media_path), False

    def lines(self, media_path: str) -> list[Line]:
        model = self._model_obj()
        # 放送TS対策：まず ffmpeg で 16kHz mono WAV を前抽出してから渡す。
        audio_path, is_temp = self._extract_audio(media_path)
        try:
            # vad_filter で無音/BGMだけの区間を飛ばし、セリフに絞る。
            segments, info = model.transcribe(
                audio_path,
                language=(self._cfg.whisper_language or None),
                vad_filter=True,
                beam_size=5,
            )
            logger.info("whisper 検出言語=%s p=%.2f", getattr(info, "language", "?"),
                        getattr(info, "language_probability", 0.0))
            out: list[Line] = []
            for s in segments:
                text = (s.text or "").strip()
                if text:
                    out.append(Line(start=float(s.start), end=float(s.end), text=text, speaker=None))
            logger.info("whisper 書き起こし %d 行", len(out))
            return out
        finally:
            if is_temp:
                try:
                    os.remove(audio_path)
                except OSError:
                    pass
