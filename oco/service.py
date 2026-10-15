"""OcoService — pipeline/store/recall/job/webhook を呼ぶ唯一の窓口。

CLI・MCP・API は薄いトランスポートとしてこれを叩くだけ（ロジック二重化を防ぐ）。
MCP/HTTP 非依存の純Python。

- 入力は「作品名(title) + 第〇話」が主役。内部 slug(series_id) と分離し service が解決。
- 同期(wait=true)既定・非同期(job_id)はオプトイン。worker=1（ローカルLLM多重OOM対策）。
- 完了時に webhook をプッシュ（config既定 + リクエスト上書き）。プルは常時可。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .config import Config
from .db import init_db, transaction
from .embed import embed_query
from .personas import load_persona
from .pipeline import build_store, process_episode

logger = logging.getLogger("oco.service")

# 同一(series_id, episode, persona_id)の未終了ジョブをこの秒数まで in-flight とみなす。
# これより古い running/queued はクラッシュ遺骸（プロセス再起動でrunningのまま死んだ等）と
# 判断して重複排除せず新規作成を許す＝永久にそのエピソードが塞がるのを防ぐ。
# 60分番組でも実測29分なので3時間は正常走行と誤検知しない安全マージン。
_INFLIGHT_STALE_SEC = 3 * 60 * 60


def _slugify(title: str) -> str:
    slug = re.sub(r"[^0-9A-Za-z]+", "-", title).strip("-").lower()
    if len(slug) < 2:
        slug = "series-" + hashlib.sha1(title.encode("utf-8")).hexdigest()[:10]
    return slug


class AmbiguousSeries(Exception):
    """title が複数候補に一致（取り出し系で候補を返す）。"""

    def __init__(self, candidates: list[dict]):
        self.candidates = candidates
        super().__init__(f"作品名が曖昧: {len(candidates)} 候補")


class SeriesNotFound(Exception):
    pass


class InvalidMediaPath(Exception):
    """media_path が許可ルート外/拡張子不正/不在（任意ファイル読み防止）。"""


class OcoService:
    def __init__(self, cfg: Config, max_workers: int | None = None):
        self.cfg = cfg
        init_db(cfg.db_path)
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers or cfg.api.max_workers,
            thread_name_prefix="oco-worker",
        )

    # ── 作品名(title) 解決 ─────────────────────────────────
    def _all_series(self, conn) -> list[dict]:
        return [
            {"series_id": r["series_id"], "title": r["title"],
             "aliases": json.loads(r["aliases"] or "[]")}
            for r in conn.execute("SELECT series_id, title, aliases FROM oco_series")
        ]

    def resolve_series(self, title: str | None = None, series_id: str | None = None) -> dict:
        """既存作品を解決して {series_id, title} を返す。曖昧なら AmbiguousSeries。"""
        with transaction(self.cfg.db_path) as conn:
            if series_id:
                row = conn.execute(
                    "SELECT series_id, title FROM oco_series WHERE series_id=?", (series_id,)
                ).fetchone()
                if row:
                    return {"series_id": row["series_id"], "title": row["title"]}
                raise SeriesNotFound(f"series_id={series_id!r} は存在しない")
            if not title:
                raise SeriesNotFound("title か series_id のどちらかが要る")
            series = self._all_series(conn)
        # 完全一致
        for s in series:
            if s["title"] == title:
                return {"series_id": s["series_id"], "title": s["title"]}
        # 別名一致
        for s in series:
            if title in s["aliases"]:
                return {"series_id": s["series_id"], "title": s["title"]}
        # 部分一致
        partial = [s for s in series if title in s["title"]]
        if len(partial) == 1:
            return {"series_id": partial[0]["series_id"], "title": partial[0]["title"]}
        if len(partial) > 1:
            raise AmbiguousSeries([{"series_id": s["series_id"], "title": s["title"]} for s in partial])
        raise SeriesNotFound(f"作品名 {title!r} に一致する作品が無い")

    def _merge_aliases(self, series_id: str, new_aliases: list[str]) -> None:
        if not new_aliases:
            return
        with transaction(self.cfg.db_path) as conn:
            row = conn.execute(
                "SELECT aliases FROM oco_series WHERE series_id=?", (series_id,)
            ).fetchone()
            if not row:
                return
            cur = json.loads(row["aliases"] or "[]")
            merged = list(dict.fromkeys(cur + list(new_aliases)))  # 和集合・順序保持
            if merged != cur:
                conn.execute(
                    "UPDATE oco_series SET aliases=?, updated_at=datetime('now') WHERE series_id=?",
                    (json.dumps(merged, ensure_ascii=False), series_id),
                )

    def _get_or_create_series(self, title: str, series_id: str | None,
                              aliases: list[str] | None) -> dict:
        """submit 用。既存なら解決（別名はマージ）、無ければ作る。

        - AmbiguousSeries はここで握り潰さず伝播させる（投入でも曖昧は 409 にする）。
        - 採番→INSERT を1トランザクションに畳み、title/series_id の UNIQUE 衝突
          （同時 submit の TOCTOU / 既存 title への別 series_id 指定）は IntegrityError を
          捕捉して title で再解決する＝500 でなく既存へ収束。
        """
        try:
            existing = self.resolve_series(title=title, series_id=series_id)
            self._merge_aliases(existing["series_id"], aliases or [])
            return existing
        except SeriesNotFound:
            pass  # AmbiguousSeries は意図的に伝播（呼び出し側で 409）

        sid = series_id or _slugify(title)
        with transaction(self.cfg.db_path) as conn:
            base, n = sid, 2
            while conn.execute("SELECT 1 FROM oco_series WHERE series_id=?", (sid,)).fetchone():
                sid = f"{base}-{n}"
                n += 1
            try:
                conn.execute(
                    "INSERT INTO oco_series (series_id, title, aliases) VALUES (?, ?, ?)",
                    (sid, title, json.dumps(aliases or [], ensure_ascii=False)),
                )
                return {"series_id": sid, "title": title}
            except sqlite3.IntegrityError:
                pass  # title が同時/既存で埋まった → 下で title 再解決
        return self.resolve_series(title=title)

    # ── 取り出し（要件6の3出力 + 要件7のトランスクリプト） ──
    def _bundle(self, series_id: str, title: str, ep: int, persona_id: str) -> dict:
        store = build_store(self.cfg, series_id, title)
        return {
            "series_id": series_id, "title": title, "episode": ep, "persona_id": persona_id,
            "transcript": store.read_transcript(ep),              # 要件7
            "cast": store.read_cast(ep),                          # 人物相関（客観）
            "story": store.read_story(ep),                        # ① 客観
            "understanding": store.read_understanding(ep, persona_id),  # ② 人格理解
            "review": {                                           # ③ 感想+予想
                "impression": store.read_impression(ep, persona_id),
                "outlook": store.read_outlook(ep, persona_id),
            },
        }

    def get_episode(self, *, title=None, series_id=None, episode: int, persona_id="default") -> dict:
        s = self.resolve_series(title=title, series_id=series_id)
        return self._bundle(s["series_id"], s["title"], episode, persona_id)

    def get_transcript(self, *, title=None, series_id=None, episode: int) -> dict:
        s = self.resolve_series(title=title, series_id=series_id)
        store = build_store(self.cfg, s["series_id"], s["title"])
        return {**s, "episode": episode, "transcript": store.read_transcript(episode)}

    def get_story(self, *, title=None, series_id=None, episode: int) -> dict:
        s = self.resolve_series(title=title, series_id=series_id)
        store = build_store(self.cfg, s["series_id"], s["title"])
        return {**s, "episode": episode, "story": store.read_story(episode)}

    def get_cast(self, *, title=None, series_id=None, episode: int) -> dict:
        s = self.resolve_series(title=title, series_id=series_id)
        store = build_store(self.cfg, s["series_id"], s["title"])
        return {**s, "episode": episode, "cast": store.read_cast(episode)}

    def get_understanding(self, *, title=None, series_id=None, episode: int, persona_id="default") -> dict:
        s = self.resolve_series(title=title, series_id=series_id)
        store = build_store(self.cfg, s["series_id"], s["title"])
        return {**s, "episode": episode, "persona_id": persona_id,
                "understanding": store.read_understanding(episode, persona_id)}

    def get_reflection(self, *, title=None, series_id=None, episode: int, persona_id="default") -> dict:
        s = self.resolve_series(title=title, series_id=series_id)
        store = build_store(self.cfg, s["series_id"], s["title"])
        return {**s, "episode": episode, "persona_id": persona_id,
                "impression": store.read_impression(episode, persona_id),
                "outlook": store.read_outlook(episode, persona_id)}

    def recall(self, *, title=None, series_id=None, keyword: str, persona_id=None) -> dict:
        s = self.resolve_series(title=title, series_id=series_id)
        store = build_store(self.cfg, s["series_id"], s["title"])
        return {**s, "hits": store.recall(keyword, persona=persona_id)}

    def search_lines(self, *, title=None, series_id=None, q: str, episode: int | None = None,
                     context: int = 8, limit: int = 5) -> dict:
        """うろ覚えのセリフ・出来事を全話横断で探す（前後文脈・場面説明・あらすじ同梱）。

        セリフ(hits)・場面説明(scene_hits)・あらすじ文(story_hits)を同時に返す。
        作品が解決できなければ SeriesNotFound / AmbiguousSeries を伝播（他の取り出しと同じ）。
        """
        s = self.resolve_series(title=title, series_id=series_id)
        store = build_store(self.cfg, s["series_id"], s["title"])
        query_vec = None
        if self.cfg.embed_base_url and q.strip():
            try:
                query_vec = embed_query(self.cfg, q)
            except Exception as e:                  # 埋め込みサーバーが落ちていても検索は止めない
                logger.warning("embed_query 失敗・文字検索だけで返す: %r", e)
        res = store.search_lines(q, episode=episode, context=context, limit=limit,
                                 query_vec=query_vec, model=self.cfg.embed_model)
        return {**s, "q": q, "episode": episode, **res}

    # ── 圧縮ブロック（§4.5 作業コンテキストの中身を外から確認/取得） ────────
    def list_blocks(self, *, title=None, series_id=None) -> dict:
        """封印済みの圧縮ブロック一覧（メタのみ・客観なので persona 非依存）。"""
        s = self.resolve_series(title=title, series_id=series_id)
        store = build_store(self.cfg, s["series_id"], s["title"])
        blocks = [
            {"block_index": b["block_index"], "ep_lo": b["ep_lo"],
             "ep_hi": b["ep_hi"], "block_size": b["block_size"]}
            for b in store.read_blocks()
        ]
        return {**s, "blocks": blocks}

    def get_block(self, *, title=None, series_id=None, block_index: int | None = None,
                  episode: int | None = None, persona_id: str = "default") -> dict:
        """圧縮版ストーリー（客観 story）＋主観ダイジェスト（persona別）。

        block_index 直接指定、または episode でその話を含むブロックを逆引き。
        まだ封印されていなければ found=False。
        """
        if block_index is None and episode is None:
            raise ValueError("block_index か episode のどちらかが要る")
        s = self.resolve_series(title=title, series_id=series_id)
        store = build_store(self.cfg, s["series_id"], s["title"])
        b = (store.read_block(block_index) if block_index is not None
             else store.read_block_covering(episode))
        if not b:
            return {**s, "found": False, "block_index": block_index, "episode": episode,
                    "persona_id": persona_id, "story": None, "digest": None}
        return {
            **s, "found": True, "block_index": b["block_index"],
            "ep_lo": b["ep_lo"], "ep_hi": b["ep_hi"], "block_size": b["block_size"],
            "persona_id": persona_id,
            "story": b["text"],                                        # 客観 圧縮あらすじ
            "digest": store.read_block_view(b["block_index"], persona_id),  # 主観ダイジェスト
        }

    def list_series(self, query: str | None = None) -> list[dict]:
        with transaction(self.cfg.db_path) as conn:
            out = []
            for s in self._all_series(conn):
                if query and query not in s["title"] and not any(query in a for a in s["aliases"]):
                    continue
                n = conn.execute(
                    "SELECT COUNT(*) AS c FROM oco_episodes WHERE series_id=? AND story_objective_text IS NOT NULL",
                    (s["series_id"],),
                ).fetchone()["c"]
                out.append({**s, "n_episodes": n})
            return out

    def list_episodes(self, *, title=None, series_id=None) -> dict:
        s = self.resolve_series(title=title, series_id=series_id)
        with transaction(self.cfg.db_path) as conn:
            eps = []
            for e in conn.execute(
                "SELECT e.episode_number AS ep, e.episode_transcript_text IS NOT NULL AS has_t, "
                "e.story_objective_text IS NOT NULL AS has_s FROM oco_episodes e "
                "WHERE e.series_id=? ORDER BY e.episode_number",
                (s["series_id"],),
            ):
                vrow = conn.execute(
                    "SELECT story_understanding_text IS NOT NULL AS has_u, "
                    "impression_reaction_text IS NOT NULL AS has_i, "
                    "outlook_prediction_text IS NOT NULL AS has_o "
                    "FROM oco_persona_views WHERE series_id=? AND episode_number=? LIMIT 1",
                    (s["series_id"], e["ep"]),
                ).fetchone()
                eps.append({
                    "ep": e["ep"], "has_transcript": bool(e["has_t"]), "has_story": bool(e["has_s"]),
                    "has_understanding": bool(vrow["has_u"]) if vrow else False,
                    "has_impression": bool(vrow["has_i"]) if vrow else False,
                    "has_outlook": bool(vrow["has_o"]) if vrow else False,
                })
        return {**s, "episodes": eps}

    _VIDEO_EXTS = {".ts", ".m2ts", ".mp4", ".mkv"}

    # ── 入力検証（任意ファイル読み防止） ──────────────────
    def _validate_media_path(self, media_path: str) -> str:
        """media_root 配下の .ass か動画のみ許可（resolve でリンク/.. を潰す）。

        .ass=字幕 / 動画=字幕抽出 or ASR（字幕なし）。
        """
        root = self.cfg.media_root.resolve()
        p = Path(media_path).resolve()
        if not p.is_relative_to(root):
            raise InvalidMediaPath(f"media_path は {root} 配下のみ許可: {media_path!r}")
        if p.suffix.lower() != ".ass" and p.suffix.lower() not in self._VIDEO_EXTS:
            raise InvalidMediaPath(
                f".ass か動画{sorted(self._VIDEO_EXTS)}のみ受理: {media_path!r}")
        if not p.exists():
            raise InvalidMediaPath(f"media_path が存在しない: {media_path!r}")
        return str(p)

    def _validate_video_path(self, video_path: str | None) -> str | None:
        """映像 scene 用の動画。media_root 配下の動画拡張子のみ（任意なので None 可）。"""
        if not video_path:
            return None
        root = self.cfg.media_root.resolve()
        p = Path(video_path).resolve()
        if not p.is_relative_to(root):
            raise InvalidMediaPath(f"video_path は {root} 配下のみ許可: {video_path!r}")
        if p.suffix.lower() not in self._VIDEO_EXTS:
            raise InvalidMediaPath(f"動画拡張子のみ受理{sorted(self._VIDEO_EXTS)}: {video_path!r}")
        if not p.exists():
            raise InvalidMediaPath(f"video_path が存在しない: {video_path!r}")
        return str(p)

    # ── 投入（プッシュ/同期） ──────────────────────────────
    def submit(self, *, title: str, episode: int, media_path: str,
               persona_id: str = "default", persona_prompt: str | None = None,
               aliases: list[str] | None = None, series_id: str | None = None,
               video_path: str | None = None,
               webhook_url: str | None = None, wait: bool = True,
               overwrite: bool = False) -> dict:
        media_path = self._validate_media_path(media_path)
        video_path = self._validate_video_path(video_path)
        persona_text = persona_prompt or load_persona(persona_id)  # slug検証もここで
        s = self._get_or_create_series(title, series_id, aliases)  # Ambiguous は伝播
        sid, title = s["series_id"], s["title"]

        # 既処理スキップ（overwrite=false かつ ①②③揃い）。戻り値は get_job_status と同形に正規化
        # ※outlook だけ `is not None` で見る: 単発回は生成せず**空文字**を保存するので、
        #   truthy 判定だと処理済みなのに毎回まるごと再生成してしまう。
        if not overwrite:
            b = self._bundle(sid, title, episode, persona_id)
            if (b["story"] and b["understanding"] and b["review"]["impression"]
                    and b["review"]["outlook"] is not None):
                return {"job_id": None, "state": "done", "skipped": True,
                        "series_id": sid, "title": title, "episode": episode,
                        "persona_id": persona_id, "result": b}

        job_id = uuid.uuid4().hex
        wh = webhook_url if webhook_url is not None else (self.cfg.webhook.default_url or None)
        with transaction(self.cfg.db_path) as conn:
            # in-flight 重複排除: 同一(series,ep,persona)の未終了ジョブが既に走っていれば
            # 新規ジョブを作らず既存を即返す（wait=true でも待たない＝呼び出し側は後で完成品を回収）。
            dup = self._find_inflight_job(conn, sid, episode, persona_id)
            if dup:
                logger.info(
                    "in-flight dedup: series=%s ep=%s persona=%s -> 既存 job=%s を再利用（新規作成せず）",
                    sid, episode, persona_id, dup["job_id"],
                )
                return {"job_id": dup["job_id"], "state": "running", "already_running": True,
                        "series_id": sid, "title": title, "episode": episode,
                        "persona_id": persona_id}
            conn.execute(
                "INSERT INTO oco_jobs (job_id, series_id, episode_number, persona_id, state, phase, webhook_url) "
                "VALUES (?, ?, ?, ?, 'queued', 'queued', ?)",
                (job_id, sid, episode, persona_id, wh),
            )
        fut = self._pool.submit(self._run, job_id, sid, title, episode, media_path,
                                persona_id, persona_text, wh, video_path)
        if wait:
            fut.result()
            return self.get_job_status(job_id)
        return {"job_id": job_id, "series_id": sid, "title": title,
                "episode": episode, "persona_id": persona_id, "state": "queued"}

    def _find_inflight_job(self, conn, sid: str, episode: int, persona_id: str):
        """同一(series_id, episode, persona_id)で state が未終了(queued/running)かつ
        updated_at が _INFLIGHT_STALE_SEC 以内の既存ジョブ行を返す（無ければ None）。

        updated_at は state 遷移時のみ更新（処理中は running 開始時刻のまま据え置き）だが、
        正常走行は最長でも約29分なので3時間しきい値なら誤検知しない。クラッシュ遺骸
        （runningのまま更新が止まった行）はしきい値超えで in-flight とみなさず新規作成を許す。
        """
        return conn.execute(
            "SELECT job_id FROM oco_jobs "
            "WHERE series_id=? AND episode_number=? AND persona_id=? "
            "AND state IN ('queued', 'running') "
            "AND updated_at >= datetime('now', ?) "
            "ORDER BY created_at DESC LIMIT 1",
            (sid, episode, persona_id, f"-{_INFLIGHT_STALE_SEC} seconds"),
        ).fetchone()

    def _run(self, job_id, sid, title, ep, media_path, persona_id, persona_text,
             webhook_url, video_path=None):
        self._set_job(job_id, state="running", phase="processing")
        bundle = None
        err = None
        try:
            process_episode(self.cfg, series_id=sid, ep=ep, ass_path=media_path,
                            persona_id=persona_id, persona_text=persona_text, title=title,
                            video_path=video_path)
            bundle = self._bundle(sid, title, ep, persona_id)
            self._set_job(job_id, state="done", phase="done", result_json=json.dumps(bundle, ensure_ascii=False))
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
            self._set_job(job_id, state="error", phase="error", error=err)
        # 成功/失敗とも webhook を1回プッシュ。wait=true の同期応答を塞がないよう
        # 別スレッドに切り離す（リトライの time.sleep が応答をブロックしないように）。
        if webhook_url:
            threading.Thread(
                target=self._fire_webhook,
                args=(job_id, webhook_url, sid, title, ep, persona_id, bundle, err),
                name=f"oco-webhook-{job_id[:8]}",
            ).start()

    def _set_job(self, job_id, **fields) -> None:
        cols = ", ".join(f"{k}=:{k}" for k in fields)
        with transaction(self.cfg.db_path) as conn:
            conn.execute(
                f"UPDATE oco_jobs SET {cols}, updated_at=datetime('now') WHERE job_id=:job_id",
                {**fields, "job_id": job_id},
            )

    def get_job_status(self, job_id: str) -> dict | None:
        with transaction(self.cfg.db_path) as conn:
            r = conn.execute("SELECT * FROM oco_jobs WHERE job_id=?", (job_id,)).fetchone()
        if not r:
            return None
        out = dict(r)
        out["result"] = json.loads(out["result_json"]) if out.get("result_json") else None
        out.pop("result_json", None)
        out["episode"] = out.get("episode_number")  # 取り出しと同じ 'episode' キーも提供
        return out

    # ── webhook（プッシュ出力） ────────────────────────────
    def _fire_webhook(self, job_id, url, sid, title, ep, persona_id, bundle, err) -> None:
        import httpx

        payload = {
            "event": "episode.processed",
            "job_id": job_id,
            "state": "error" if err else "done",
            "series_id": sid, "title": title, "episode": ep, "persona_id": persona_id,
            "outputs": None if err else {
                "transcript": bundle["transcript"], "story": bundle["story"],
                "understanding": bundle["understanding"], "review": bundle["review"],
            },
            "error": err,
            "ts": int(time.time()),
        }
        headers = {"Content-Type": "application/json"}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if self.cfg.webhook.secret:
            sig = hmac_sha256(self.cfg.webhook.secret, body)
            headers["X-OCO-Signature"] = sig

        backoffs = [2, 8, 30][: max(1, self.cfg.webhook.retry_max)]
        for i, delay in enumerate([0] + backoffs):
            if delay:
                time.sleep(delay)
            try:
                resp = httpx.post(url, content=body, headers=headers, timeout=15.0)
                if 200 <= resp.status_code < 300:
                    self._set_job(job_id, webhook_state="sent")
                    return
            except Exception:  # noqa: BLE001
                pass
        self._set_job(job_id, webhook_state="failed")


def hmac_sha256(secret: str, body: bytes) -> str:
    import hmac

    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
