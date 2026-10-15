"""クラスタ→キャラ名の命名（②）と Line への反映。

命名の主手がかりは『呼びかけ』（例: シュタルクが「フリーレン！」と言う＝直後/直前に
喋るクラスタがフリーレン）。これは会話の時系列に現れるので、クラスタごとに束ねず
『話者ラベル付きの時系列台本』をそのまま LLM に読ませる。
"""

from __future__ import annotations

import logging
import re
from collections import OrderedDict

from .. import prompts
from ..subtitles.ass_parser import Line

logger = logging.getLogger("oco.speakers")


def attribute_speakers(llm, lines: list[Line], cast_text: str | None,
                       asr: bool = False) -> dict:
    """話者をセリフ行に付与。クラスタが2つ以上＝クラスタ命名、無ければ行単位推定。

    lines を破壊的に更新。返り値は {mode, changed, ...} のサマリ。
    asr=True（音声認識由来の台本）ならプロンプトに誤変換の警告を入れる。
    """
    n_clusters = len({ln.speaker for ln in lines if ln.kind == "line" and ln.speaker})
    if n_clusters >= 2:
        mapping = name_speakers(llm, lines, cast_text, asr=asr)  # ②クラスタ命名(色/diarization)
        if mapping:
            apply_names(lines, mapping)
            return {"mode": "cluster", "changed": True, "named": mapping}
        return {"mode": "cluster", "changed": False}
    named = _attribute_per_line(llm, lines, cast_text, asr)      # 字幕なし: テキストだけ行単位
    return {"mode": "per_line", "changed": named > 0, "named_lines": named}


def _attribute_per_line(llm, lines: list[Line], cast_text: str | None,
                        asr: bool = False) -> int:
    """各行の話者をテキストだけで推定し Line.speaker に入れる。命名できた行数を返す。"""
    idxs = [i for i, ln in enumerate(lines) if ln.kind == "line" and ln.text]
    if len(idxs) < 2:
        return 0
    rows = []
    for n, i in enumerate(idxs, 1):
        ln = lines[i]
        mm, ss = divmod(int(ln.start), 60)
        rows.append(f"{n}. [{mm:02d}:{ss:02d}] {ln.text}")
    sys_, pre_, vol_ = prompts.build_line_attribution("\n".join(rows), cast_text, asr=asr)
    # 出力は全行ぶん（行番号=名前）。本文予算を行数に応じて広げる。
    out = llm.complete(system=sys_, stable_prefix=pre_, volatile=vol_,
                       max_tokens=min(8000, len(idxs) * 12 + 600))
    named = 0
    for raw in out.splitlines():
        m = re.match(r"\s*(\d+)\s*[=:.、]\s*(.+)", raw)
        if not m:
            continue
        n = int(m.group(1))
        # 名前だけ取る（モデルが付けがちな注釈「(Context: ...)」「（〜）」を捨てる）
        name = re.split(r"[\s(（]", m.group(2).strip())[0].strip("。.,、")
        # 長すぎ＝セリフを名前化した誤り、疑問符＝同上 を弾く（名前は短い）
        if len(name) > 12 or name.endswith(("?", "？")):
            continue
        if 1 <= n <= len(idxs) and name and name.lower() != "unknown":
            lines[idxs[n - 1]].speaker = name
            named += 1
    logger.info("話者推定(行単位): %d 行中 %d 行に命名", len(idxs), named)
    return named


def _label(i: int) -> str:
    # 話者A, 話者B, ... 話者Z, 話者AA ...
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return "話者" + s


def name_speakers(llm, lines: list[Line], cast_text: str | None,
                  max_lines: int = 400, asr: bool = False) -> dict[str, str]:
    """話者クラスタ→キャラ名のマッピングを返す（確信あるものだけ・不明は除外）。"""
    # 出現順に安定したクラスタ→ラベル(話者A..)を割り当て
    order: "OrderedDict[str, int]" = OrderedDict()
    counts: dict[str, int] = {}
    for ln in lines:
        if ln.kind == "line" and ln.speaker:
            if ln.speaker not in order:
                order[ln.speaker] = len(order)
            counts[ln.speaker] = counts.get(ln.speaker, 0) + 1
    if len(order) < 2:
        return {}  # クラスタが1つ以下なら同定の意味がない

    label = {cid: _label(i) for cid, i in order.items()}
    rev = {v: k for k, v in label.items()}

    # 話者ラベル付きの時系列台本（呼びかけが見えるようそのまま順に）
    rows = []
    for ln in lines:
        if ln.kind == "line" and ln.speaker in label:
            mm, ss = divmod(int(ln.start), 60)
            rows.append(f"[{mm:02d}:{ss:02d}] ({label[ln.speaker]}) {ln.text}")
    summary = "、".join(f"{label[c]}={counts[c]}発話" for c in order)
    block = f"（話者の発話数: {summary}）\n" + "\n".join(rows[:max_lines])

    sys_, pre_, vol_ = prompts.build_speaker_naming(block, cast_text, asr=asr)
    out = llm.complete(system=sys_, stable_prefix=pre_, volatile=vol_)

    mapping: dict[str, str] = {}
    for raw in out.splitlines():
        if "=" not in raw:
            continue
        lab, name = raw.split("=", 1)
        lab, name = lab.strip(), name.strip().strip("。.,、")
        if lab in rev and name and name.lower() != "unknown":
            mapping[rev[lab]] = name
    logger.info("話者同定: %d クラスタ中 %d 命名 %s",
                len(order), len(mapping), {label[k]: v for k, v in mapping.items()})
    return mapping


def apply_names(lines: list[Line], mapping: dict[str, str]) -> list[Line]:
    """マッピングに従い Line.speaker をキャラ名へ。未命名クラスタはそのまま。"""
    for ln in lines:
        if ln.speaker in mapping:
            ln.speaker = mapping[ln.speaker]
    return lines
