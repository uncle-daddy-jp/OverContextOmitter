"""保存物＝1話ごと・ただのファイル（グラフも索引も持たない）。

store/<series_id>/
  ep29/
    lines.jsonl              # 全セリフ（t, end, speaker?, text）← recall の素・客観
    story.txt                # ① あらすじ                       ← 客観（人格非依存）
    <persona_id>/
      impression.txt         # ② 感想                           ← 人格
      outlook.txt            # ④ 前回まで踏まえた感想+予想+期待  ← 人格

客観（lines/story）はペルソナ非依存で1組。主観（impression/outlook）は persona_id ごと。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .subtitles.ass_parser import Line


def _ep_dirname(ep: int) -> str:
    return f"ep{ep:02d}"


@dataclass
class EpisodeArtifacts:
    ep: int
    story: str | None = None
    impression: str | None = None
    outlook: str | None = None


class SeriesStore:
    def __init__(self, store_dir: str | Path, series_id: str):
        self.root = Path(store_dir) / series_id
        self.series_id = series_id

    # ── パス ─────────────────────────────────────────────
    def _ep_dir(self, ep: int) -> Path:
        return self.root / _ep_dirname(ep)

    def _persona_dir(self, ep: int, persona: str) -> Path:
        return self._ep_dir(ep) / persona

    # 人格プロンプト provenance（files 版は保持しない＝SQLite 版とIFを揃えるためのno-op）
    def get_or_create_persona_prompt(self, persona_id: str, prompt_text: str) -> None:
        return None

    # ── 書き込み ─────────────────────────────────────────
    def write_lines(self, ep: int, lines: list[Line]) -> None:
        d = self._ep_dir(ep)
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "lines.jsonl", "w", encoding="utf-8") as f:
            for ln in lines:
                f.write(json.dumps(ln.to_record(), ensure_ascii=False) + "\n")

    def write_transcript(self, ep: int, text: str) -> None:
        d = self._ep_dir(ep)
        d.mkdir(parents=True, exist_ok=True)
        (d / "transcript.txt").write_text(text.strip() + "\n", encoding="utf-8")

    def write_cast(self, ep: int, text: str) -> None:
        d = self._ep_dir(ep)
        d.mkdir(parents=True, exist_ok=True)
        (d / "cast.txt").write_text(text.strip() + "\n", encoding="utf-8")

    def read_latest_cast(self, before_ep: int) -> str | None:
        for ep in sorted(self.processed_episodes(), reverse=True):
            if ep < before_ep:
                p = self._ep_dir(ep) / "cast.txt"
                if p.exists():
                    return p.read_text(encoding="utf-8").strip()
        return None

    # model/n_lines/n_masked/episode_title は SQLite 版とIFを揃えるため受けるが files 版は無視
    def write_story(self, ep: int, text: str, model: str | None = None,
                    n_lines: int | None = None, n_masked: int | None = None,
                    episode_title: str | None = None) -> None:
        d = self._ep_dir(ep)
        d.mkdir(parents=True, exist_ok=True)
        (d / "story.txt").write_text(text.strip() + "\n", encoding="utf-8")

    def write_understanding(self, ep: int, persona: str, text: str,
                            persona_prompt_id=None, model=None) -> None:
        d = self._persona_dir(ep, persona)
        d.mkdir(parents=True, exist_ok=True)
        (d / "understanding.txt").write_text(text.strip() + "\n", encoding="utf-8")

    def write_impression(self, ep: int, persona: str, text: str,
                         persona_prompt_id=None, model=None) -> None:
        d = self._persona_dir(ep, persona)
        d.mkdir(parents=True, exist_ok=True)
        (d / "impression.txt").write_text(text.strip() + "\n", encoding="utf-8")

    def write_outlook(self, ep: int, persona: str, text: str,
                      persona_prompt_id=None, model=None) -> None:
        d = self._persona_dir(ep, persona)
        d.mkdir(parents=True, exist_ok=True)
        (d / "outlook.txt").write_text(text.strip() + "\n", encoding="utf-8")

    # ── 読み込み ─────────────────────────────────────────
    def read_lines(self, ep: int) -> list[dict]:
        p = self._ep_dir(ep) / "lines.jsonl"
        if not p.exists():
            return []
        return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line]

    def read_transcript(self, ep: int) -> str | None:
        p = self._ep_dir(ep) / "transcript.txt"
        return p.read_text(encoding="utf-8").strip() if p.exists() else None

    def read_cast(self, ep: int) -> str | None:
        p = self._ep_dir(ep) / "cast.txt"
        return p.read_text(encoding="utf-8").strip() if p.exists() else None

    def read_story(self, ep: int) -> str | None:
        p = self._ep_dir(ep) / "story.txt"
        return p.read_text(encoding="utf-8").strip() if p.exists() else None

    def read_understanding(self, ep: int, persona: str) -> str | None:
        p = self._persona_dir(ep, persona) / "understanding.txt"
        return p.read_text(encoding="utf-8").strip() if p.exists() else None

    def read_impression(self, ep: int, persona: str) -> str | None:
        p = self._persona_dir(ep, persona) / "impression.txt"
        return p.read_text(encoding="utf-8").strip() if p.exists() else None

    def read_outlook(self, ep: int, persona: str) -> str | None:
        p = self._persona_dir(ep, persona) / "outlook.txt"
        return p.read_text(encoding="utf-8").strip() if p.exists() else None

    def processed_episodes(self) -> list[int]:
        """story.txt を持つ話数（昇順）。"""
        if not self.root.exists():
            return []
        eps = []
        for d in self.root.iterdir():
            m = re.fullmatch(r"ep(\d+)", d.name)
            if m and (d / "story.txt").exists():
                eps.append(int(m.group(1)))
        return sorted(eps)

    # ── recall（テキスト引き・ただの文字列一致） ─────────
    def recall(self, keyword: str, persona: str | None = None) -> list[dict]:
        """全話の lines / story / impression を横断キーワード検索。

        埋め込み・グラフなし、ただの部分一致。ヒット箇所を {ep, type, excerpt} で返す。
        """
        hits: list[dict] = []
        if not self.root.exists():
            return hits
        for ep in sorted(
            int(re.fullmatch(r"ep(\d+)", d.name).group(1))
            for d in self.root.iterdir()
            if re.fullmatch(r"ep(\d+)", d.name)
        ):
            for rec in self.read_lines(ep):
                if keyword in rec.get("text", ""):
                    hits.append({"ep": ep, "type": "line", "excerpt": rec["text"]})
            if (story := self.read_story(ep)) and keyword in story:
                hits.append({"ep": ep, "type": "story", "excerpt": _snippet(story, keyword)})
            if persona and (imp := self.read_impression(ep, persona)) and keyword in imp:
                hits.append({"ep": ep, "type": "impression", "excerpt": _snippet(imp, keyword)})
        return hits


def _snippet(text: str, keyword: str, span: int = 30) -> str:
    i = text.find(keyword)
    if i < 0:
        return text[:span]
    a = max(0, i - span)
    b = min(len(text), i + len(keyword) + span)
    return ("…" if a > 0 else "") + text[a:b] + ("…" if b < len(text) else "")
