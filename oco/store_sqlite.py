"""SQLite バックエンドの Store。

SeriesStore（フラットファイル）と同名・同シグネチャの read/write/recall/
processed_episodes を満たしつつ、②understanding・整形済みトランスクリプト・
人格プロンプト provenance を足す。pipeline は同名メソッド依存なので非破壊で差替可。

操作ごとに短命接続を開閉する（WAL+busy_timeout でスレッド安全に同時アクセス）。
"""

from __future__ import annotations

import hashlib
import unicodedata

import numpy as np

from .db import init_db, transaction
from .store import _snippet  # スニペット整形は1実装を共有
from .subtitles.ass_parser import Line

# ── セリフあいまい検索のパラメータ（決定的・LLM/埋め込み無し） ─────────
_SUBSTR_BONUS = 0.35      # 正規化クエリが対象文に丸ごと含まれるときの加点
_SCORE_MIN = 0.25         # これ未満は「見つかった」とみなさない
_LOW_CONF_N = 3           # 閾値以上が0件のときに参考提示する件数
# 短いクエリ同士は 2-gram が1個かすっただけで Dice が閾値を越えてしまう
# （実測: 「おはよう」が「しつよう」に『よう』1個の一致で 0.333）。
# 丸ごと含まれるとき（「別班」→「別班？」等）は1個一致でも通す。
_MIN_MATCH = 2
# うろ覚えは「珍しい字面は合っているが用言が言い換わる」形になりやすく、
# 素の Dice だと言い換え側の一致数に負ける。そこで作品内でほとんど出てこない
# 2-gram を共有していたら、その希少さに応じた下限スコアまで押し上げる。
_RARE_DF_MAX = 3          # df(その2-gramを含む行数)がこれ以下なら希少とみなす
_RARE_FLOOR = {1: 0.60, 2: 0.55, 3: 0.50}
# 出来事クエリ（「◯◯が逃げたシーン」等）は場面説明・あらすじの言い回しと
# 大きく違うが、人名や物などの字面はそろう。そこで scene/あらすじ側だけ
# 「かな以外を含む長めの共通チャンク」を手がかりに下限スコアを与える。
# 母数が小さい（scene 数十〜百数十行・あらすじ数十文）ので df は使わない。
# 逆にライン側（数万行）へこれを足すとありふれた漢字語で溢れるので入れない。
_CHUNK_FLOOR = 0.5
_CHUNK_LEN = (2, 6)       # 共通チャンクとして見る長さの範囲
_SCENE_STORY_N = 2        # scene / あらすじ文の返却上限（各）
# NFKC 後に落とす文字カテゴリ: P=句読点 S=記号 Z=空白 C=制御/改行
# （、。！？…・「」『』（）〓 等は全部ここに入る。ー(U+30FC)は Lm なので残る）
_DROP_CATS = ("P", "S", "Z", "C")

# ── 意味で引く検索（埋め込みがあるときだけ・oco/embed.py） ─────────────
_WINDOW_SEC = 30          # セリフはこの秒数ごとにまとめて 1 文書にする（1 行だけでは意味が薄い）
_VERBATIM_MIN = 0.8       # 2-gram がこれ以上＝逐語の引用。意味検索より優先して hits に出す
# cos がこれ以上で「見つかった」とする。EmbeddingGemma 2 は無関係な文どうしでも 0.65〜0.75 出る。
# 実測（2026-10-15・本番 DB の写し 249 話）: 作品にある場面の言い換え 12 本の最高 cos 0.73〜0.86
# （11 本が 0.78 以上）／作品に無い問い 10 本 0.68〜0.76・実際に来た言い回しの問い 32 本（作品に無い場面）0.69〜0.82
_SEMANTIC_MIN = 0.78


def _keep(s: str) -> list[str]:
    """NFKC したうえで記号・空白を落とした文字列（字種はそのまま）。"""
    return [ch for ch in unicodedata.normalize("NFKC", s or "")
            if unicodedata.category(ch)[0] not in _DROP_CATS]


def _fold(ch: str) -> str:
    """1文字を照合用に畳む（カタカナ(ァ〜ヶ・ヴ含む)→ひらがな→小文字化）。"""
    if "ァ" <= ch <= "ヶ":
        ch = chr(ord(ch) - 0x60)
    low = ch.casefold()
    return low if len(low) == 1 else ch.lower()[:1]


def _norm(s: str) -> str:
    """照合用の正規化。NFKC → 記号・空白除去 → カタカナをひらがな化 → 小文字化。

    字幕は全角/半角・記号・カタカナ表記が揺れるので、うろ覚えフレーズと
    当たるよう見た目の違いを潰してから比べる。
    """
    return "".join(_fold(ch) for ch in _keep(s))


def _bigrams(s: str) -> set[str]:
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _score(nq: str, bq: set[str], nt: str) -> tuple[float, set[str], bool]:
    """正規化済みクエリと対象文の (近さ0..1, 一致した2-gramの集合, 部分一致か)。

    近さ＝文字2-gramのDice係数＋部分一致ボーナス（1.0でクリップ）。
    クエリが2文字未満で 2-gram を作れない縮退時は部分一致だけで判定する。
    一致集合と部分一致は、かすっただけのヒットを落とす足切り(_MIN_MATCH)と
    希少一致の下駄(_rare_floor)に使う。
    """
    if not nt:
        return 0.0, set(), False
    sub = nq in nt
    bonus = _SUBSTR_BONUS if sub else 0.0
    bt = _bigrams(nt)
    if not bq or not bt:                    # 1文字クエリ/1文字セリフ＝Dice不能
        return bonus, set(), sub
    inter = bq & bt
    return min(1.0, 2 * len(inter) / (len(bq) + len(bt)) + bonus), inter, sub


def _rare_ok_bigrams(q: str) -> set[str]:
    """下駄を許す2-gram（クエリ側でひらがな以外＝カタカナ・漢字・英数を含むもの）。

    ひらがなだけの2文字は助詞のつなぎ目で偶然そろいやすく、珍しくても手がかりに
    ならない（「おはよう」の『はよ』が「それはよかった」に当たる類）。
    """
    kept = _keep(q)
    folded = "".join(_fold(ch) for ch in kept)
    return {folded[i:i + 2] for i in range(len(folded) - 1)
            if not all("ぁ" <= c <= "ゟ" for c in kept[i:i + 2])}


def _is_kana(ch: str) -> bool:
    """ひらがな・カタカナ（長音符・繰り返し記号を含む）か。"""
    return "ぁ" <= ch <= "ゟ" or "ァ" <= ch <= "ヿ"


def _chunk_candidates(q: str) -> set[str]:
    """クエリから切り出す共通チャンク候補（照合用に畳んだ形）。

    長さ _CHUNK_LEN の部分文字列のうち、かな以外（漢字・英数）を1文字以上含むものだけ。
    かなだけの断片は語のつなぎ目で偶然そろうため手がかりにしない。
    """
    kept = _keep(q)
    folded = "".join(_fold(ch) for ch in kept)
    lo, hi = _CHUNK_LEN
    out: set[str] = set()
    for i in range(len(folded)):
        for n in range(lo, min(hi, len(folded) - i) + 1):
            if not all(_is_kana(c) for c in kept[i:i + n]):
                out.add(folded[i:i + n])
    return out


def _chunk_floor(chunks: set[str], nt: str) -> float:
    """共通チャンクが対象文に含まれていれば下限スコアを返す（scene/あらすじ用）。"""
    return _CHUNK_FLOOR if any(c in nt for c in chunks) else 0.0


def _sentences(text: str | None) -> list[str]:
    """あらすじを「。」と改行で文に割る（前後の空白を落とし、空文は捨てる）。"""
    blob = (text or "").replace("\r", "\n").replace("。", "\n")
    return [s.strip() for s in blob.split("\n") if s.strip()]


def _rare_floor(matched: set[str], df: dict[str, int]) -> float:
    """一致2-gram に希少なものがあれば、その希少さに応じた下限スコアを返す。"""
    floor = 0.0
    for g in matched:
        n = df.get(g, 0)
        if 1 <= n <= _RARE_DF_MAX:
            floor = max(floor, _RARE_FLOOR[n])
    return floor


def _drop_adjacent(ranked: list[dict], limit: int) -> list[dict]:
    """隣り合うアンカーのヒットを畳む（連結ヒットとその2行目が二重に出るのを防ぐ）。

    スコア降順で見て、採用済みと同じ話・アンカーが隣接(差1以内)なら捨てる。
    """
    picked: list[dict] = []
    for h in ranked:
        if any(p["ep"] == h["ep"] and abs(p["seq"] - h["seq"]) <= 1 for p in picked):
            continue
        picked.append(h)
        if len(picked) >= limit:
            break
    return picked


def _ctx(item: dict) -> dict:
    """前後文脈の1行（scene 行も混じるので kind を残す）。"""
    return {"speaker": item["speaker"], "text": item["text"], "kind": item["kind"]}


def _following_lines(items: list[dict], i: int, n: int) -> list[dict]:
    """items[i] の直後に続くセリフ行を n 行（間に挟まる scene 行は飛ばす）。"""
    out = []
    for x in items[i + 1:]:
        if x["kind"] != "line":
            continue
        out.append(_ctx(x))
        if len(out) >= n:
            break
    return out


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

    def write_story_check(self, ep: int, text: str) -> None:
        """① の検証結果（大幅な誤りの指摘 or「問題なし」）を保存（監査用・再生成の有無はここで追える）。"""
        with transaction(self.db_path) as conn:
            self._ensure_episode(conn, ep)
            conn.execute(
                "UPDATE oco_episodes SET story_check_text=?, updated_at=datetime('now') "
                "WHERE series_id=? AND episode_number=?",
                (text, self.series_id, ep),
            )

    def read_story_check(self, ep: int) -> str | None:
        return self._read_episode_col(ep, "story_check_text")

    def write_serial_next(self, ep: int, serial_next: int, reason: str | None = None) -> None:
        """単発/連続の判定（1=連続・0=単発）と理由を保存（監査用）。"""
        with transaction(self.db_path) as conn:
            self._ensure_episode(conn, ep)
            conn.execute(
                "UPDATE oco_episodes SET serial_next=?, serial_next_reason=?, "
                "updated_at=datetime('now') WHERE series_id=? AND episode_number=?",
                (serial_next, reason, self.series_id, ep),
            )

    def read_serial_next(self, ep: int) -> int | None:
        """1=連続 / 0=単発 / None=旧データ・未判定。"""
        return self._read_episode_col(ep, "serial_next")

    def read_serial_next_reason(self, ep: int) -> str | None:
        return self._read_episode_col(ep, "serial_next_reason")

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

    # ── セリフあいまい検索（うろ覚えフレーズ→全話横断・前後文脈つき） ────
    def search_lines(self, q: str, *, episode: int | None = None,
                     context: int = 8, limit: int = 5,
                     query_vec: np.ndarray | None = None, model: str | None = None) -> dict:
        """うろ覚えのセリフ・出来事を全話から探し、前後の文脈やあらすじを添えて返す。

        照合は決定的（LLM/埋め込み無し）。3種類を同時に探す:

        - hits       : セリフ（line 行の単体と、line_seq が連続する2行の連結）
        - scene_hits : 場面説明（scene 行）＋直後に続くセリフ行
        - story_hits : 客観あらすじの文＋その話の場面説明の並び

        セリフは希少2-gramの下駄、scene/あらすじは共通チャンクの下駄で拾う
        （母数が違うので手がかりを撃ち分ける。_CHUNK_FLOOR のコメント参照）。

        どれも閾値未満なら、セリフの上位 _LOW_CONF_N 件を low_confidence 付きで返す
        （「実は言っていないセリフ」でも近いものを見せる。found=False で区別可）。

        query_vec（問いの埋め込み）が渡され、この作品の文書ベクトルがあれば意味検索に
        切り替える（_semantic_result）。返す形は同じで "search" が "semantic" になる。
        """
        context = max(0, int(context))
        limit = max(1, int(limit))
        nq = _norm(q)
        if not nq:
            return {"found": False, "hits": [], "scene_hits": [], "story_hits": [], "stories": {},
                    "search": "lexical"}
        bq = _bigrams(nq)
        rare_ok = bq & _rare_ok_bigrams(q)
        chunks = _chunk_candidates(q)

        sql = ("SELECT episode_number, line_seq, line_kind, line_start_sec, line_speaker, line_text "
               "FROM oco_lines WHERE series_id=?")
        params: list = [self.series_id]
        if episode is not None:
            sql += " AND episode_number=?"
            params.append(episode)
        sql += " ORDER BY episode_number, line_seq"
        with transaction(self.db_path) as conn:
            rows = conn.execute(sql, params).fetchall()

        # 話ごとに時系列で並べ直す（前後文脈と scene 逆引きに使う）。正規化と
        # クエリ2-gramの一致は1回だけ計算して使い回す（hit は df 用なので line 行のみ）。
        by_ep: dict[int, list[dict]] = {}
        for r in rows:
            norm = _norm(r["line_text"])
            by_ep.setdefault(r["episode_number"], []).append({
                "seq": r["line_seq"], "kind": r["line_kind"],
                "start": r["line_start_sec"], "speaker": r["line_speaker"],
                "text": r["line_text"], "norm": norm,
                "hit": bq & _bigrams(norm) if r["line_kind"] == "line" else set(),
            })

        df = self._bigram_df(bq, by_ep, scoped_to_episode=episode is not None)

        # 採点。同じアンカー行（単体ヒットと2行連結ヒット）は高い方だけ残す。
        best: dict[tuple[int, int], dict] = {}
        for ep, items in by_ep.items():
            for i, it in enumerate(items):
                if it["kind"] != "line":
                    continue
                # 希少判定は行そのものが持つ一致だけを見る（連結のつなぎ目で
                # 偶然できた2-gram＝別の語と語の境目なので手がかりにしない）。
                cands = [(it["text"], it["norm"], it["hit"], i)]     # 単体
                nxt = items[i + 1] if i + 1 < len(items) else None
                if nxt and nxt["kind"] == "line" and nxt["seq"] == it["seq"] + 1:
                    joined = it["text"] + nxt["text"]                # 2行連結
                    cands.append((joined, _norm(joined), it["hit"] | nxt["hit"], i + 1))
                for text, nt, in_row, last_i in cands:
                    score, matched, sub = _score(nq, bq, nt)
                    floor = _rare_floor(in_row & rare_ok, df)
                    score = max(score, floor)
                    key = (ep, it["seq"])
                    cur = best.get(key)
                    if cur is None or score > cur["score"]:
                        best[key] = {"ep": ep, "seq": it["seq"], "idx": i, "last_idx": last_i,
                                     "text": text, "score": score, "n_match": len(matched),
                                     "sub": sub, "rare": floor > 0}

        ranked = sorted(best.values(), key=lambda h: (-h["score"], h["ep"], h["idx"]))
        # 足切り: 希少2-gram一致（それ自体が強い手がかり）と丸ごと一致は免除
        strong = [h for h in ranked if h["score"] >= _SCORE_MIN
                  and (h["rare"] or h["sub"] or h["n_match"] >= _MIN_MATCH)]

        stories_all = self._read_stories(episode)
        sem = self.search_vectors(query_vec, model, episode) if query_vec is not None else []
        if sem:
            return self._semantic_result(by_ep, strong, sem, stories_all, context, limit)

        scene_hits = self._scene_hits(by_ep, nq, bq, chunks, context)
        story_hits = self._story_hits(by_ep, stories_all, nq, bq, chunks)

        found = bool(strong or scene_hits or story_hits)
        # セリフが1件も閾値に届かないときだけ参考提示（scene/あらすじで見つかっていれば不要）
        pool = strong if strong else ([] if found else [h for h in ranked if h["score"] > 0])
        picked = _drop_adjacent(pool, limit if strong else _LOW_CONF_N)

        hits = [self._build_hit(by_ep[h["ep"]], h, context, low_confidence=not found)
                for h in picked]
        eps = ({h["episode"] for h in hits} | {h["episode"] for h in story_hits}
               | {h["episode"] for h in scene_hits})
        return {"found": found, "hits": hits,
                "scene_hits": scene_hits, "story_hits": story_hits,
                "stories": {str(e): stories_all[e] for e in sorted(eps) if e in stories_all},
                "search": "lexical"}

    def _semantic_result(self, by_ep: dict[int, list[dict]], strong: list[dict], sem: list[dict],
                         stories_all: dict[int, str], context: int, limit: int) -> dict:
        """意味検索の結果を今までと同じ形に組む。

        - hits: 逐語ヒット（2-gram が _VERBATIM_MIN 以上 or 問いがそのまま含まれる）があればそれだけ。無ければ 30 秒窓の
          ヒットを「窓の最初のセリフ＝anchor・窓の残り＝after」の形で入れる。
        - scene_hits / story_hits: 意味検索だけ（2-gram の照合は外れが多いので使わない）。
        - found: 逐語ヒットがある or 意味検索の最高 cos が _SEMANTIC_MIN 以上。
        """
        # 問いがセリフにそのまま含まれる（短い語の「泥」等）も逐語扱い＝旧どおり返す
        verbatim = [h for h in strong if h["score"] >= _VERBATIM_MIN or h["sub"]]
        found = bool(verbatim) or sem[0]["score"] >= _SEMANTIC_MIN
        passed = [d for d in sem if d["score"] >= _SEMANTIC_MIN]

        if verbatim:
            hits = [self._build_hit(by_ep[h["ep"]], h, context, low_confidence=False)
                    for h in _drop_adjacent(verbatim, limit)]
        else:
            hits = []
            for d in (passed if found else sem):
                if len(hits) >= (limit if found else _LOW_CONF_N):
                    break
                if d["doc_kind"] == "win30" and (
                        h := self._window_hit(by_ep.get(d["episode"], []), d, context, not found)):
                    hits.append(h)

        scene_hits = []
        for d in passed:
            if d["doc_kind"] != "scene" or len(scene_hits) >= _SCENE_STORY_N:
                continue
            items = by_ep.get(d["episode"], [])
            i = next((k for k, x in enumerate(items)
                      if x["seq"] == d["doc_seq"] and x["kind"] == "scene"), None)
            if i is None:                      # 書いた後に台本が作り直された
                continue
            scene_hits.append({
                "episode": d["episode"], "line_seq": d["doc_seq"], "start_sec": items[i]["start"],
                "text": items[i]["text"], "score": round(d["score"], 4),
                "window": _following_lines(items, i, context),
            })

        story_hits = [
            {"episode": d["episode"], "sentence": d["doc_text"], "score": round(d["score"], 4),
             "scenes_digest": [x["text"] for x in by_ep.get(d["episode"], []) if x["kind"] == "scene"]}
            for d in passed if d["doc_kind"] == "story"
        ][:_SCENE_STORY_N]

        eps = ({h["episode"] for h in hits} | {h["episode"] for h in story_hits}
               | {h["episode"] for h in scene_hits})
        return {"found": found, "hits": hits,
                "scene_hits": scene_hits, "story_hits": story_hits,
                "stories": {str(e): stories_all[e] for e in sorted(eps) if e in stories_all},
                "search": "semantic"}

    def _window_hit(self, items: list[dict], d: dict, context: int,
                    low_confidence: bool) -> dict | None:
        """30 秒窓の文書ヒットを hits の 1 件に組む（窓にセリフが無くなっていれば None）。"""
        rows = [k for k, x in enumerate(items) if int(x["start"] // _WINDOW_SEC) == d["doc_seq"]]
        i = next((k for k in rows if items[k]["kind"] == "line"), None)
        if i is None:
            return None
        hit = self._build_hit(items, {"ep": d["episode"], "idx": i, "last_idx": i,
                                      "text": items[i]["text"], "score": d["score"]},
                              context, low_confidence=low_confidence)
        hit["after"] = [_ctx(items[k]) for k in rows if k > i]
        return hit

    # ── 意味で引く検索の文書ベクトル（書く・探す・文書を作る） ─────────────
    def vector_docs_for_episode(self, ep: int) -> list[dict]:
        """その話の検索用文書: 30 秒窓（セリフ＋scene 行の文）・scene 行・あらすじの 1 文。"""
        items = self.read_lines(ep)                     # line_seq 順＝添字が line_seq
        windows: dict[int, list[dict]] = {}
        for x in items:
            windows.setdefault(int(x["t"] // _WINDOW_SEC), []).append(x)
        docs = [{"doc_kind": "win30", "doc_seq": b, "start_sec": rows[0]["t"],
                 "doc_text": " ".join(r["text"] for r in rows)}
                for b, rows in windows.items() if any(r["kind"] == "line" for r in rows)]
        docs += [{"doc_kind": "scene", "doc_seq": seq, "start_sec": x["t"], "doc_text": x["text"]}
                 for seq, x in enumerate(items) if x["kind"] == "scene"]
        docs += [{"doc_kind": "story", "doc_seq": n, "start_sec": None, "doc_text": s}
                 for n, s in enumerate(_sentences(self.read_story(ep)))]
        return docs

    def write_vectors(self, ep: int, docs: list[dict], vecs: np.ndarray, model: str) -> None:
        """その話の文書ベクトルを入れ替える（delete→insert）。"""
        with transaction(self.db_path) as conn:
            self._ensure_episode(conn, ep)
            conn.execute("DELETE FROM oco_vectors WHERE series_id=? AND episode_number=?",
                         (self.series_id, ep))
            conn.executemany(
                "INSERT INTO oco_vectors (series_id, episode_number, doc_kind, doc_seq, start_sec, "
                "doc_text, model, vec) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(self.series_id, ep, d["doc_kind"], d["doc_seq"], d["start_sec"], d["doc_text"],
                  model, np.asarray(v, dtype=np.float32).tobytes())
                 for d, v in zip(docs, vecs)],
            )

    def search_vectors(self, query_vec: np.ndarray, model: str | None,
                       episode: int | None = None) -> list[dict]:
        """作品 1 本ぶんの文書ベクトルと cos を取り、高い順に全部返す（無ければ空）。"""
        # ponytail: numpy 総当たり。作品あたり 10 万文書を超えたら sqlite-vec
        sql = ("SELECT episode_number, doc_kind, doc_seq, doc_text, vec FROM oco_vectors "
               "WHERE series_id=? AND model=?")
        params: list = [self.series_id, model]
        if episode is not None:
            sql += " AND episode_number=?"
            params.append(episode)
        with transaction(self.db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        if not rows:
            return []
        mat = np.frombuffer(b"".join(r["vec"] for r in rows), dtype=np.float32).reshape(len(rows), -1)
        scores = mat @ np.asarray(query_vec, dtype=np.float32)
        return [{"episode": rows[k]["episode_number"], "doc_kind": rows[k]["doc_kind"],
                 "doc_seq": rows[k]["doc_seq"], "doc_text": rows[k]["doc_text"],
                 "score": float(scores[k])}
                for k in np.argsort(-scores)]

    def _scene_hits(self, by_ep: dict[int, list[dict]], nq: str, bq: set[str],
                    chunks: set[str], context: int) -> list[dict]:
        """場面説明（scene 行）のヒット。閾値以上のみ・詰め物はしない。"""
        out = []
        for ep, items in by_ep.items():
            for i, it in enumerate(items):
                if it["kind"] != "scene":
                    continue
                score, matched, sub = _score(nq, bq, it["norm"])
                floor = _chunk_floor(chunks, it["norm"])
                score = max(score, floor)
                if score < _SCORE_MIN or not (floor or sub or len(matched) >= _MIN_MATCH):
                    continue
                out.append({
                    "episode": ep, "line_seq": it["seq"], "start_sec": it["start"],
                    "text": it["text"], "score": round(score, 4),
                    "window": _following_lines(items, i, context),
                })
        out.sort(key=lambda h: (-h["score"], h["episode"], h["line_seq"]))
        return out[:_SCENE_STORY_N]

    def _story_hits(self, by_ep: dict[int, list[dict]], stories: dict[int, str],
                    nq: str, bq: set[str], chunks: set[str]) -> list[dict]:
        """客観あらすじの文のヒット。閾値以上のみ・詰め物はしない。"""
        out = []
        for ep, story in stories.items():
            digest = [x["text"] for x in by_ep.get(ep, []) if x["kind"] == "scene"]
            for sent in _sentences(story):
                nt = _norm(sent)
                score, matched, sub = _score(nq, bq, nt)
                floor = _chunk_floor(chunks, nt)
                score = max(score, floor)
                if score < _SCORE_MIN or not (floor or sub or len(matched) >= _MIN_MATCH):
                    continue
                out.append({"episode": ep, "sentence": sent, "score": round(score, 4),
                            "scenes_digest": digest})
        out.sort(key=lambda h: (-h["score"], h["episode"]))
        return out[:_SCENE_STORY_N]

    def _read_stories(self, episode: int | None) -> dict[int, str]:
        """客観あらすじ（話→全文）。あらすじ文の照合と stories 同梱の両方に使う。"""
        sql = ("SELECT episode_number AS ep, story_objective_text AS story FROM oco_episodes "
               "WHERE series_id=? AND story_objective_text IS NOT NULL")
        params: list = [self.series_id]
        if episode is not None:
            sql += " AND episode_number=?"
            params.append(episode)
        with transaction(self.db_path) as conn:
            return {r["ep"]: r["story"] for r in conn.execute(sql, params)}

    def _bigram_df(self, bq: set[str], by_ep: dict[int, list[dict]],
                   scoped_to_episode: bool) -> dict[str, int]:
        """クエリ2-gramごとの df（それを含む line 行の数）。希少さの判定に使う。

        df は話を絞っても作品全体で数える（絞り込みで珍しさの尺度が動かないように）。
        必要なのはクエリの2-gramだけなので、全2-gramの索引は作らない。
        """
        df = dict.fromkeys(bq, 0)
        if scoped_to_episode:
            with transaction(self.db_path) as conn:
                rows = conn.execute(
                    "SELECT line_text FROM oco_lines WHERE series_id=? AND line_kind='line'",
                    (self.series_id,),
                ).fetchall()
            hits = (bq & _bigrams(_norm(r["line_text"])) for r in rows)
        else:
            hits = (it["hit"] for items in by_ep.values() for it in items
                    if it["kind"] == "line")
        for h in hits:
            for g in h:
                df[g] += 1
        return df

    def _build_hit(self, items: list[dict], h: dict, context: int, low_confidence: bool) -> dict:
        """1ヒット分の返り値を組む（前後文脈・直前の scene 説明を添える）。"""
        i, last = h["idx"], h["last_idx"]
        anchor = items[i]
        scene = None
        for prev in reversed(items[:i]):                 # 直前で最も近い場面説明
            if prev["kind"] == "scene":
                scene = prev["text"]
                break
        lo = max(0, i - context)
        return {
            "episode": h["ep"],
            "line_seq": anchor["seq"],
            "start_sec": anchor["start"],
            "speaker": anchor["speaker"],
            "text": h["text"],
            "score": round(h["score"], 4),
            "low_confidence": low_confidence,
            "scene": scene,
            "before": [_ctx(x) for x in items[lo:i]],
            # 連結ヒットの2行目は text に入っているので after には含めない
            "after": [_ctx(x) for x in items[last + 1:last + 1 + context]],
        }

