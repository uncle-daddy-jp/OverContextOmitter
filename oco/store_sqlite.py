"""SQLite バックエンドの Store。

SeriesStore（フラットファイル）と同名・同シグネチャの read/write/recall/
processed_episodes を満たしつつ、②understanding・整形済みトランスクリプト・
人格プロンプト provenance を足す。pipeline は同名メソッド依存なので非破壊で差替可。

操作ごとに短命接続を開閉する（WAL+busy_timeout でスレッド安全に同時アクセス）。
"""

from __future__ import annotations

import hashlib

from .db import init_db, transaction
from .store import _snippet  # スニペット整形は1実装を共有
from .subtitles.ass_parser import Line


class SqliteStore:
    def __init__(self, db_path: str, series_id: str, title: str | None = None):
        self.db_path = db_path
        self.series_id = series_id
        self.title = title or series_id
        init_db(db_path)

    # ── 内部: 親行の存在を保証（FK） ──────────────────────
    def _ensure_series(self, conn) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO oco_series (series_id, title) VALUES (?, ?)",
            (self.series_id, self.title),
        )
        row = conn.execute(
            "SELECT 1 FROM oco_series WHERE series_id=?", (self.series_id,)
        ).fetchone()
        if row is None:
            raise ValueError(
                f"作品名(title) {self.title!r} が別の series_id に既に使われている。"
                "service で title→series_id を解決すること。"
            )

    def _ensure_episode(self, conn, ep: int) -> None:
        self._ensure_series(conn)
        conn.execute(
            "INSERT OR IGNORE INTO oco_episodes (series_id, episode_number) VALUES (?, ?)",
            (self.series_id, ep),
        )

    # ── 人格プロンプト provenance（get-or-create） ─────────
    def get_or_create_persona_prompt(self, persona_id: str, prompt_text: str) -> int | None:
        if not prompt_text:
            return None
        sha = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
        with transaction(self.db_path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO oco_persona_prompts (persona_id, prompt_sha256, prompt_text) "
                "VALUES (?, ?, ?)",
                (persona_id, sha, prompt_text),
            )
            row = conn.execute(
                "SELECT persona_prompt_id FROM oco_persona_prompts WHERE persona_id=? AND prompt_sha256=?",
                (persona_id, sha),
            ).fetchone()
        return row["persona_prompt_id"] if row else None

    # ── 書き込み ─────────────────────────────────────────
    def write_lines(self, ep: int, lines: list[Line]) -> None:
        with transaction(self.db_path) as conn:
            self._ensure_episode(conn, ep)
            conn.execute(
                "DELETE FROM oco_lines WHERE series_id=? AND episode_number=?",
                (self.series_id, ep),
            )
            conn.executemany(
                "INSERT INTO oco_lines "
                "(series_id, episode_number, line_seq, line_kind, line_start_sec, line_end_sec, line_speaker, line_text) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    # 時刻は 2桁丸め（to_record と一致させ backend 間差を無くす）
                    (self.series_id, ep, seq, ln.kind, round(ln.start, 2), round(ln.end, 2),
                     ln.speaker, ln.text)
                    for seq, ln in enumerate(lines)
                ],
            )

    def write_transcript(self, ep: int, text: str) -> None:
        with transaction(self.db_path) as conn:
            self._ensure_episode(conn, ep)
            conn.execute(
                "UPDATE oco_episodes SET episode_transcript_text=?, updated_at=datetime('now') "
                "WHERE series_id=? AND episode_number=?",
                (text, self.series_id, ep),
            )

    def write_cast(self, ep: int, text: str) -> None:
        with transaction(self.db_path) as conn:
            self._ensure_episode(conn, ep)
            conn.execute(
                "UPDATE oco_episodes SET cast_roster_text=?, updated_at=datetime('now') "
                "WHERE series_id=? AND episode_number=?",
                (text, self.series_id, ep),
            )

    def read_latest_cast(self, before_ep: int) -> str | None:
        """before_ep より前で最後に確定した人物相関（carry-forward の引き継ぎ元）。"""
        with transaction(self.db_path) as conn:
            row = conn.execute(
                "SELECT cast_roster_text AS v FROM oco_episodes "
                "WHERE series_id=? AND episode_number<? AND cast_roster_text IS NOT NULL "
                "ORDER BY episode_number DESC LIMIT 1",
                (self.series_id, before_ep),
            ).fetchone()
        return row["v"] if row else None

    def write_story(
        self,
        ep: int,
        text: str,
        model: str | None = None,
        n_lines: int | None = None,
        n_masked: int | None = None,
        episode_title: str | None = None,
    ) -> None:
        with transaction(self.db_path) as conn:
            self._ensure_series(conn)
            conn.execute(
                "INSERT INTO oco_episodes "
                "(series_id, episode_number, episode_title, story_objective_text, story_model, n_lines, n_masked) "
                "VALUES (:sid, :ep, :etitle, :body, :model, :n_lines, :n_masked) "
                "ON CONFLICT (series_id, episode_number) DO UPDATE SET "
                "  story_objective_text=excluded.story_objective_text, story_model=excluded.story_model, "
                "  n_lines=excluded.n_lines, n_masked=excluded.n_masked, "
                "  episode_title=COALESCE(excluded.episode_title, oco_episodes.episode_title), "
                "  updated_at=datetime('now')",
                {
                    "sid": self.series_id, "ep": ep, "etitle": episode_title,
                    "body": text, "model": model, "n_lines": n_lines, "n_masked": n_masked,
                },
            )

    def _write_view(self, ep: int, persona: str, column: str, text: str,
                    persona_prompt_id: int | None, model: str | None) -> None:
        # understanding/impression/outlook の部分更新（他列は COALESCE で保持）
        with transaction(self.db_path) as conn:
            self._ensure_episode(conn, ep)
            conn.execute(
                f"INSERT INTO oco_persona_views "
                f"(series_id, episode_number, persona_id, {column}, persona_prompt_id, view_model) "
                f"VALUES (:sid, :ep, :pid, :body, :ppid, :model) "
                f"ON CONFLICT (series_id, episode_number, persona_id) DO UPDATE SET "
                f"  {column}=excluded.{column}, "
                f"  persona_prompt_id=COALESCE(excluded.persona_prompt_id, oco_persona_views.persona_prompt_id), "
                f"  view_model=COALESCE(excluded.view_model, oco_persona_views.view_model), "
                f"  updated_at=datetime('now')",
                {"sid": self.series_id, "ep": ep, "pid": persona,
                 "body": text, "ppid": persona_prompt_id, "model": model},
            )

    def write_understanding(self, ep, persona, text, persona_prompt_id=None, model=None):
        self._write_view(ep, persona, "story_understanding_text", text, persona_prompt_id, model)

    def write_impression(self, ep, persona, text, persona_prompt_id=None, model=None):
        self._write_view(ep, persona, "impression_reaction_text", text, persona_prompt_id, model)

    def write_outlook(self, ep, persona, text, persona_prompt_id=None, model=None):
        self._write_view(ep, persona, "outlook_prediction_text", text, persona_prompt_id, model)

    # ── 読み込み ─────────────────────────────────────────
    def read_lines(self, ep: int) -> list[dict]:
        with transaction(self.db_path) as conn:
            rows = conn.execute(
                "SELECT line_start_sec, line_end_sec, line_speaker, line_text, line_kind "
                "FROM oco_lines WHERE series_id=? AND episode_number=? ORDER BY line_seq",
                (self.series_id, ep),
            ).fetchall()
        return [
            {"t": r["line_start_sec"], "end": r["line_end_sec"],
             "speaker": r["line_speaker"], "text": r["line_text"], "kind": r["line_kind"]}
            for r in rows
        ]

    def read_transcript(self, ep: int) -> str | None:
        return self._read_episode_col(ep, "episode_transcript_text")

    def read_cast(self, ep: int) -> str | None:
        return self._read_episode_col(ep, "cast_roster_text")

    def read_story(self, ep: int) -> str | None:
        return self._read_episode_col(ep, "story_objective_text")

    def _read_episode_col(self, ep: int, column: str) -> str | None:
        with transaction(self.db_path) as conn:
            row = conn.execute(
                f"SELECT {column} AS v FROM oco_episodes WHERE series_id=? AND episode_number=?",
                (self.series_id, ep),
            ).fetchone()
        return row["v"] if row else None

    def read_understanding(self, ep, persona) -> str | None:
        return self._read_view_col(ep, persona, "story_understanding_text")

    def read_impression(self, ep, persona) -> str | None:
        return self._read_view_col(ep, persona, "impression_reaction_text")

    def read_outlook(self, ep, persona) -> str | None:
        return self._read_view_col(ep, persona, "outlook_prediction_text")

    def _read_view_col(self, ep: int, persona: str, column: str) -> str | None:
        with transaction(self.db_path) as conn:
            row = conn.execute(
                f"SELECT {column} AS v FROM oco_persona_views "
                f"WHERE series_id=? AND episode_number=? AND persona_id=?",
                (self.series_id, ep, persona),
            ).fetchone()
        return row["v"] if row else None

    def processed_episodes(self) -> list[int]:
        """story を持つ話数（昇順）。"""
        with transaction(self.db_path) as conn:
            rows = conn.execute(
                "SELECT episode_number FROM oco_episodes "
                "WHERE series_id=? AND story_objective_text IS NOT NULL ORDER BY episode_number",
                (self.series_id,),
            ).fetchall()
        return [r["episode_number"] for r in rows]

    # ── ブロック（§4.5 階層ローリング圧縮・作業コンテキスト用） ────────────
    def write_block(self, block_index: int, ep_lo: int, ep_hi: int, block_size: int,
                    text: str, model: str | None = None) -> None:
        with transaction(self.db_path) as conn:
            self._ensure_series(conn)
            conn.execute(
                "INSERT INTO oco_blocks "
                "(series_id, block_index, ep_lo, ep_hi, block_size, block_story_text, block_model) "
                "VALUES (:sid,:bi,:lo,:hi,:bs,:body,:model) "
                "ON CONFLICT (series_id, block_index) DO UPDATE SET "
                "  ep_lo=excluded.ep_lo, ep_hi=excluded.ep_hi, block_size=excluded.block_size, "
                "  block_story_text=excluded.block_story_text, block_model=excluded.block_model, "
                "  updated_at=datetime('now')",
                {"sid": self.series_id, "bi": block_index, "lo": ep_lo, "hi": ep_hi,
                 "bs": block_size, "body": text, "model": model},
            )

    def read_block(self, block_index: int) -> dict | None:
        with transaction(self.db_path) as conn:
            r = conn.execute(
                "SELECT block_index, ep_lo, ep_hi, block_size, block_story_text FROM oco_blocks "
                "WHERE series_id=? AND block_index=?",
                (self.series_id, block_index),
            ).fetchone()
        if not r:
            return None
        return {"block_index": r["block_index"], "ep_lo": r["ep_lo"], "ep_hi": r["ep_hi"],
                "block_size": r["block_size"], "text": r["block_story_text"]}

    def read_blocks(self) -> list[dict]:
        """封印済みブロック要約を ep_lo 昇順で。作業コンテキストの前段。"""
        with transaction(self.db_path) as conn:
            rows = conn.execute(
                "SELECT block_index, ep_lo, ep_hi, block_size, block_story_text FROM oco_blocks "
                "WHERE series_id=? ORDER BY ep_lo",
                (self.series_id,),
            ).fetchall()
        return [
            {"block_index": r["block_index"], "ep_lo": r["ep_lo"], "ep_hi": r["ep_hi"],
             "block_size": r["block_size"], "text": r["block_story_text"]}
            for r in rows
        ]

    def read_block_covering(self, ep: int) -> dict | None:
        """その話(ep)を含む封印済みブロックを返す（逆引き・無ければ None）。"""
        with transaction(self.db_path) as conn:
            r = conn.execute(
                "SELECT block_index, ep_lo, ep_hi, block_size, block_story_text FROM oco_blocks "
                "WHERE series_id=? AND ep_lo<=? AND ep_hi>=? ORDER BY ep_lo LIMIT 1",
                (self.series_id, ep, ep),
            ).fetchone()
        if not r:
            return None
        return {"block_index": r["block_index"], "ep_lo": r["ep_lo"], "ep_hi": r["ep_hi"],
                "block_size": r["block_size"], "text": r["block_story_text"]}

    def write_block_view(self, block_index: int, persona: str, text: str,
                         persona_prompt_id: int | None = None, model: str | None = None) -> None:
        # 客観ブロック（親行）が先に封印済みである前提（FK）。
        with transaction(self.db_path) as conn:
            conn.execute(
                "INSERT INTO oco_block_views "
                "(series_id, block_index, persona_id, block_digest_text, persona_prompt_id, view_model) "
                "VALUES (:sid,:bi,:pid,:body,:ppid,:model) "
                "ON CONFLICT (series_id, block_index, persona_id) DO UPDATE SET "
                "  block_digest_text=excluded.block_digest_text, "
                "  persona_prompt_id=COALESCE(excluded.persona_prompt_id, oco_block_views.persona_prompt_id), "
                "  view_model=COALESCE(excluded.view_model, oco_block_views.view_model), "
                "  updated_at=datetime('now')",
                {"sid": self.series_id, "bi": block_index, "pid": persona,
                 "body": text, "ppid": persona_prompt_id, "model": model},
            )

    def read_block_view(self, block_index: int, persona: str) -> str | None:
        with transaction(self.db_path) as conn:
            r = conn.execute(
                "SELECT block_digest_text AS v FROM oco_block_views "
                "WHERE series_id=? AND block_index=? AND persona_id=?",
                (self.series_id, block_index, persona),
            ).fetchone()
        return r["v"] if r else None

    # ── recall（LIKE・line/story/impression。現行 store.recall と1:1） ─────
    def recall(self, keyword: str, persona: str | None = None) -> list[dict]:
        like = f"%{keyword}%"
        hits: list[dict] = []
        with transaction(self.db_path) as conn:
            for r in conn.execute(
                "SELECT episode_number AS ep, line_text AS txt FROM oco_lines "
                "WHERE series_id=? AND line_kind='line' AND line_text LIKE ? ORDER BY episode_number, line_seq",
                (self.series_id, like),
            ):
                hits.append({"ep": r["ep"], "type": "line", "excerpt": r["txt"]})
            for r in conn.execute(
                "SELECT episode_number AS ep, story_objective_text AS txt FROM oco_episodes "
                "WHERE series_id=? AND story_objective_text LIKE ? ORDER BY episode_number",
                (self.series_id, like),
            ):
                hits.append({"ep": r["ep"], "type": "story", "excerpt": _snippet(r["txt"], keyword)})
            if persona:
                for r in conn.execute(
                    "SELECT episode_number AS ep, impression_reaction_text AS txt FROM oco_persona_views "
                    "WHERE series_id=? AND persona_id=? AND impression_reaction_text LIKE ? ORDER BY episode_number",
                    (self.series_id, persona, like),
                ):
                    hits.append({"ep": r["ep"], "type": "impression", "excerpt": _snippet(r["txt"], keyword)})
        return hits
