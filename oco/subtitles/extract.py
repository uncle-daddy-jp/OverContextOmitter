"""TS から ARIB 字幕を .ass に抽出する ffmpeg ラッパー。

ffmpeg 6.1+ は libaribcaption デコーダを本体内蔵（Docker に同梱済）。
近年アニメは字幕放送が多く、字幕onlyで内容理解＋話者割当は実用十分（実証済）。
字幕が無い番組は別途 Whisper 等が要る（ここでは扱わない）。

※実 TS での字幕ストリーム index / 文字コードは録画環境で揺れる。
  まずは `ffmpeg -i <ts>` で字幕ストリームを確認 → このコマンドを実機で詰める。
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def probe_subtitle_streams(ts_path: str | Path) -> str:
    """ffmpeg にストリーム一覧を出させる（字幕 index 確認用）。"""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(ts_path)],
        capture_output=True,
        text=True,
        errors="replace",  # ffmpeg stderr は非UTF-8(放送メタ)を含み得る
    )
    # ffmpeg は入力のみ指定だと「出力が無い」で非0終了するが stderr に情報が出る
    return proc.stderr


def extract_ass(
    ts_path: str | Path,
    out_path: str | Path,
    *,
    stream: str = "0:s:0",
) -> Path:
    """TS の字幕ストリームを .ass として書き出す。

    libaribcaption は ASS スタイル（色＝話者キー）付きで出力できる。
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-i", str(ts_path),
        "-map", stream,
        "-c:s", "ass",            # libaribcaption デコーダ→ASS（色＝話者キー）に変換
        str(out_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True, errors="replace")
    return out_path
