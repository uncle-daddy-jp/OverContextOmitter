"""ASS 字幕パーサ。

[Events] の Dialogue 行を抽出し、セリフ行（time, speaker?, text）に変換する。
ARIB 字幕→ASS では話者の色分けが `{\\1c&Hbbggrr&}` の primary color 上書きで表れる
（フリーレン s1e33 等で実証）。この色を speaker キーとして拾う。白(catch-all)で
名前明示のない行は speaker=None（＝話者不明）が誠実、という方針。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# {\...} 形式の override タグ全体
_OVERRIDE_RE = re.compile(r"\{[^}]*\}")
# primary color 上書き {\1c&Hbbggrr&}（&H... または無印）
_COLOR_RE = re.compile(r"\\1c&H([0-9A-Fa-f]+)&?")
# 改行タグ
_NEWLINE_RE = re.compile(r"\\[Nnh]")


@dataclass
class Line:
    start: float          # 秒
    end: float            # 秒
    text: str             # 純テキスト（override 除去済み）
    speaker: str | None   # 話者キー（色 or 名前）。不明は None
    style: str = ""
    kind: str = "line"    # "line"=字幕セリフ | "scene"=VLM映像説明（後挿入）

    def to_record(self) -> dict:
        return {
            "t": round(self.start, 2),
            "end": round(self.end, 2),
            "speaker": self.speaker,
            "text": self.text,
            "kind": self.kind,
        }


def _parse_time(s: str) -> float:
    """ASS 時刻 H:MM:SS.cs → 秒。"""
    s = s.strip()
    h, m, rest = s.split(":")
    return int(h) * 3600 + int(m) * 60 + float(rest)


def _extract_speaker(raw_text: str, name_field: str) -> str | None:
    """話者キーを推定。Name フィールド優先、無ければ色上書き。"""
    if name_field.strip():
        return name_field.strip()
    if m := _COLOR_RE.search(raw_text):
        color = m.group(1).upper()
        # 白(catch-all)は話者特定にならない → None
        if color in ("FFFFFF", "FFFFFFFF"):
            return None
        return f"#{color}"
    return None


def _clean_text(raw_text: str) -> str:
    """override タグを除去し改行を空白に畳む。"""
    t = _NEWLINE_RE.sub(" ", raw_text)
    t = _OVERRIDE_RE.sub("", t)
    return t.strip()


def parse_ass(content: str) -> list[Line]:
    """ASS 文字列をパースして時刻順の Line リストを返す。"""
    fmt: list[str] | None = None
    lines: list[Line] = []
    in_events = False

    for raw in content.splitlines():
        line = raw.strip()
        if line.startswith("["):
            in_events = line.lower() == "[events]"
            continue
        if not in_events:
            continue
        if line.startswith("Format:"):
            fmt = [c.strip() for c in line[len("Format:"):].split(",")]
            continue
        if not line.startswith("Dialogue:"):
            continue
        if fmt is None:
            # Format 行が無い ASS は標準フォーマットを仮定
            fmt = ["Layer", "Start", "End", "Style", "Name",
                   "MarginL", "MarginR", "MarginV", "Effect", "Text"]

        # Text は最後のフィールド（カンマを含み得る）なので最大分割数で守る
        body = line[len("Dialogue:"):]
        parts = body.split(",", len(fmt) - 1)
        if len(parts) < len(fmt):
            continue
        rec = dict(zip(fmt, parts))
        try:
            start = _parse_time(rec["Start"])
            end = _parse_time(rec["End"])
        except (KeyError, ValueError):
            continue
        raw_text = rec.get("Text", "")
        text = _clean_text(raw_text)
        if not text:
            continue
        lines.append(
            Line(
                start=start,
                end=end,
                text=text,
                speaker=_extract_speaker(raw_text, rec.get("Name", "")),
                style=rec.get("Style", ""),
            )
        )

    lines.sort(key=lambda x: x.start)
    return lines
