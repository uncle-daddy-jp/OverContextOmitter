"""チャプター sidecar（Amatsukaze / join_logo_scp 出力）ベースの CM 検出。

録画を作る Amatsukaze 側で本物の join_logo_scp + 局ロゴによる CM 解析が走り、
チャプターファイルが録画と同じディレクトリに出力される前提。OCO はそれを
**読むだけ**＝自前ロゴ検出（jl_backend）のような原理的誤検出（白半透明ロゴが
明るい画面で消える）が無く、解析コストもゼロ。

- sidecar 探索: 録画は「X.hevc.ts」のような複合拡張子なので `with_suffix()` 一発では
  ズレる。候補ファイル名を優先順で総当たりする（実際の Amatsukaze 出力名は
  次の録画で確定するため広めに）。見つからなければ CM 無し扱い（安全）。
- 形式: OGM チャプター（`CHAPTER01=00:00:00.000` ＋ `CHAPTER01NAME=CM` のペア行）。
  名前に "CM"（大文字小文字無視・全角も NFKC で吸収）を含むチャプター＝CM。
  区間 = [そのチャプター開始, 次のチャプター開始)。連続する CM チャプターは 1 区間に結合。
- 最終チャプターが CM の場合: 「次チャプター無し＝動画末尾まで」。CMRange は end 必須で
  「end 無し」を表せないが、消費側（mask_lines / select_gap_frames）は動画尺内の時刻 t の
  contains(t) しか見ない → **end ≥ 実尺なら何を入れても「末尾まで」と等価**。
  なので ffprobe で実尺を引いて入れる（正確）。ffprobe 不能時のみ有限の大きめ定数
  （inf は下流の演算/直列化事故の芽になるので避ける）。
- 頑健性: パース不能・不正な行・空ファイル → logger.warning して []。
  **絶対にパイプラインを落とさない**（CM 取りこぼしは安い・落ちるのは高い）。
"""

from __future__ import annotations

import logging
import re
import subprocess
import unicodedata
from pathlib import Path

from .base import CMRange

logger = logging.getLogger("oco.cm")

# 最終チャプターが CM で実尺が引けない時の end フォールバック。
# 消費側は contains(t)（t < 実尺）しか見ないため、実尺以上でありさえすれば
# 「動画末尾まで」と完全に等価（放送録画で 6 時間を超えることはまず無い）。
_TAIL_FALLBACK_SEC = 6 * 3600.0

# OGM チャプター行: CHAPTER01=00:00:00.000（時は可変桁・ミリ秒は省略可・","区切りも許容）
_TIME_RE = re.compile(
    r"^CHAPTER(\d+)\s*=\s*(\d+):(\d{1,2}):(\d{1,2})(?:[.,](\d{1,3}))?\s*$",
    re.IGNORECASE,
)
# OGM チャプター名行: CHAPTER01NAME=CM
_NAME_RE = re.compile(r"^CHAPTER(\d+)NAME\s*=\s*(.*)$", re.IGNORECASE)


class ChapterCMProvider:
    """録画と同じディレクトリのチャプター sidecar から CM レンジを読む。"""

    def __init__(self, cfg):
        self._cfg = cfg

    def ranges(self, media_path: str | None) -> list[CMRange]:
        if not media_path:
            return []
        try:
            media = Path(media_path)
            for sidecar in _sidecar_candidates(media):
                if sidecar.is_file():
                    return self._ranges_from_sidecar(media, sidecar)
            logger.info("chapter: sidecar 無し（%s）→ CM無し扱い", media.name)
            return []
        except Exception:
            # 何が来てもパイプラインは落とさない（CM無し＝本編全量処理へフォールバック）
            logger.warning("chapter: 予期しない失敗 → CM無し扱い（media=%s）",
                           media_path, exc_info=True)
            return []

    @staticmethod
    def _ranges_from_sidecar(media: Path, sidecar: Path) -> list[CMRange]:
        # utf-8-sig ＝ BOM 付き/なし両対応。バイナリ誤爆は replace で読み切り→形式判定で弾く。
        text = sidecar.read_text(encoding="utf-8-sig", errors="replace")
        chapters = _parse_chapters(text)
        if chapters is None:
            logger.warning("chapter: %s をチャプター形式として解釈できず → CM無し扱い",
                           sidecar.name)
            return []
        ranges = _cm_ranges(chapters, media)
        logger.info("chapter: %s から CM %d区間（チャプター%d個）",
                    sidecar.name, len(ranges), len(chapters))
        return ranges


def _sidecar_candidates(media: Path) -> list[Path]:
    """sidecar 候補パスを優先順で。複合拡張子「X.hevc.ts」を段階的に剥がして生成。

    例: X.hevc.ts → X.hevc.ts.chapter / X.hevc.chapter / X.hevc.chapter.txt / X.hevc.chapters.txt /
        X.hevc.ts.chapters.txt / X.chapter / X.hevc.txt
    """
    b0 = media                  # X.hevc.ts（フルネーム）
    b1 = b0.with_suffix("")     # X.hevc（末尾 1 拡張子を剥がす）
    b2 = b1.with_suffix("")     # X（さらに 1 つ剥がす）
    names = [
        b0.name + ".chapter",
        b1.name + ".chapter",
        b1.name + ".chapter.txt",               # Amatsukaze の実出力名（2026-09-24 これが無く 7/19 以降 CM 全素通り）
        b1.name + ".chapters.txt",
        b0.name + ".chapters.txt",
        b2.name + ".chapter",
        b1.name + ".txt",
    ]
    out: list[Path] = []
    seen: set[str] = set()
    for n in names:
        if n not in seen and n != media.name:  # 拡張子が浅い時の重複/自分自身を除外
            seen.add(n)
            out.append(media.parent / n)
    return out


def _parse_chapters(text: str) -> list[tuple[float, str]] | None:
    """OGM チャプター本文 → [(開始秒, 名前)] を時刻順で。

    時刻行が 1 つも無い（空ファイル・別形式・バイナリ誤爆）なら None。
    不正な行は個別に無視する（1 行の汚れで全部を捨てない）。
    """
    times: dict[int, float] = {}
    names: dict[int, str] = {}
    for raw in text.splitlines():          # splitlines ＝ CRLF/LF どちらも吸収
        line = raw.strip()
        if not line:
            continue
        m = _NAME_RE.match(line)
        if m:
            names[int(m.group(1))] = m.group(2).strip()
            continue
        m = _TIME_RE.match(line)
        if m:
            hh, mm, ss = int(m.group(2)), int(m.group(3)), int(m.group(4))
            ms = int((m.group(5) or "0").ljust(3, "0"))  # ".5"→500ms
            times[int(m.group(1))] = hh * 3600 + mm * 60 + ss + ms / 1000.0
            continue
        # CHAPTER 行以外（コメント等）は黙って無視
    if not times:
        return None
    chapters = [(times[k], names.get(k, "")) for k in sorted(times)]
    chapters.sort(key=lambda c: c[0])      # 番号と時刻が食い違う汚れにも保険
    return chapters


def _is_cm_name(name: str) -> bool:
    """チャプター名が CM を指すか（"CM"/"cm 3"/"ＣＭ" 等・大文字小文字無視）。"""
    return "cm" in unicodedata.normalize("NFKC", name).lower()


def _cm_ranges(chapters: list[tuple[float, str]], media: Path) -> list[CMRange]:
    """CM チャプターの連続 run → [run 先頭の開始, 直後チャプターの開始) の CMRange 列。"""
    out: list[CMRange] = []
    i, n = 0, len(chapters)
    while i < n:
        start, name = chapters[i]
        if not _is_cm_name(name):
            i += 1
            continue
        j = i + 1
        while j < n and _is_cm_name(chapters[j][1]):
            j += 1                          # 連続 CM チャプターは 1 区間に畳む
        if j < n:
            end = chapters[j][0]
        else:                               # 最終チャプターが CM ＝ 動画末尾まで
            end = _end_of_video(media, start)
        if end > start:
            out.append(CMRange(start, end))
        i = j
    return out


def _end_of_video(media: Path, cm_start: float) -> float:
    """末尾 CM の end。実尺（ffprobe）＞ フォールバック定数の順（モジュール先頭コメント参照）。"""
    dur = _probe_duration(media)
    if dur is not None and dur > cm_start:
        return dur
    return cm_start + _TAIL_FALLBACK_SEC


def _probe_duration(media: Path) -> float | None:
    """ffprobe でコンテナ実尺（秒）。取れなければ None（呼び出し側でフォールバック）。"""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(media)],
            capture_output=True, timeout=30,
        )
        return float(proc.stdout.decode("ascii", "ignore").strip()) or None
    except Exception:
        return None
