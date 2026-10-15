"""FastAPI 薄いトランスポート。OcoService を叩くだけ。

認証は既定 無認証（LAN内信頼）。config [auth].token_env を設定すると bearer 必須化。
uvicorn は --workers 1 前提（ジョブ表/Executor がワーカ間で分裂しないため）。
"""

from __future__ import annotations

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .config import Config
from .service import AmbiguousSeries, InvalidMediaPath, OcoService, SeriesNotFound

cfg = Config.load()
svc = OcoService(cfg)
app = FastAPI(title="OverContextOmitter", version="0.2.0")


def require_auth(authorization: str | None = Header(default=None)):
    if not cfg.auth_token:
        return  # 無認証（LAN内信頼）
    expected = f"Bearer {cfg.auth_token}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="unauthorized")


@app.exception_handler(SeriesNotFound)
async def _not_found(_req, exc: SeriesNotFound):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(AmbiguousSeries)
async def _ambiguous(_req, exc: AmbiguousSeries):
    return JSONResponse(status_code=409, content={"detail": str(exc), "candidates": exc.candidates})


@app.exception_handler(InvalidMediaPath)
async def _bad_media(_req, exc: InvalidMediaPath):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


class ProcessReq(BaseModel):
    title: str
    episode: int
    media_path: str                 # コンテナ内パス（/recordings/...）。.ass 主・.ts は extract 併用
    persona_id: str = "default"
    persona_prompt: str | None = None
    aliases: list[str] = []
    series_id: str | None = None
    video_path: str | None = None       # 映像 scene 抽出用の動画（任意・vision設定時）
    webhook_url: str | None = None
    wait: bool = True
    overwrite: bool = False


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/readyz")
def readyz():
    return {"store": "sqlite", "db": str(cfg.db_path),
            "text_provider": cfg.text.provider, "db_exists": cfg.db_path.exists()}


@app.post("/episodes", dependencies=[Depends(require_auth)])
def submit_episode(req: ProcessReq):
    res = svc.submit(
        title=req.title, episode=req.episode, media_path=req.media_path,
        persona_id=req.persona_id, persona_prompt=req.persona_prompt,
        aliases=req.aliases, series_id=req.series_id, video_path=req.video_path,
        webhook_url=req.webhook_url, wait=req.wait, overwrite=req.overwrite,
    )
    # 202 は本当に非同期キュー投入したときだけ（skip/同期完了は 200）
    if res.get("state") == "queued":
        return JSONResponse(status_code=202, content=res)
    return res


@app.get("/jobs/{job_id}", dependencies=[Depends(require_auth)])
def job_status(job_id: str):
    j = svc.get_job_status(job_id)
    if not j:
        raise HTTPException(404, "unknown job")
    return j


@app.get("/series", dependencies=[Depends(require_auth)])
def list_series(query: str | None = None):
    return {"series": svc.list_series(query=query)}


@app.get("/series/resolve", dependencies=[Depends(require_auth)])
def resolve_series(title: str):
    return svc.resolve_series(title=title)


@app.get("/episodes", dependencies=[Depends(require_auth)])
def list_episodes(title: str | None = None, series_id: str | None = None):
    return svc.list_episodes(title=title, series_id=series_id)


@app.get("/episodes/{episode}", dependencies=[Depends(require_auth)])
def get_episode(episode: int, title: str | None = None, series_id: str | None = None,
                persona_id: str = Query("default")):
    b = svc.get_episode(title=title, series_id=series_id, episode=episode, persona_id=persona_id)
    if b["story"] is None:
        raise HTTPException(404, "未処理 or 存在しない話")
    return b


@app.get("/episodes/{episode}/transcript", dependencies=[Depends(require_auth)])
def get_transcript(episode: int, title: str | None = None, series_id: str | None = None):
    return svc.get_transcript(title=title, series_id=series_id, episode=episode)


@app.get("/episodes/{episode}/story", dependencies=[Depends(require_auth)])
def get_story(episode: int, title: str | None = None, series_id: str | None = None):
    return svc.get_story(title=title, series_id=series_id, episode=episode)


@app.get("/episodes/{episode}/cast", dependencies=[Depends(require_auth)])
def get_cast(episode: int, title: str | None = None, series_id: str | None = None):
    return svc.get_cast(title=title, series_id=series_id, episode=episode)


@app.get("/episodes/{episode}/understanding", dependencies=[Depends(require_auth)])
def get_understanding(episode: int, title: str | None = None, series_id: str | None = None,
                      persona_id: str = Query("default")):
    return svc.get_understanding(title=title, series_id=series_id, episode=episode, persona_id=persona_id)


@app.get("/episodes/{episode}/reflection", dependencies=[Depends(require_auth)])
def get_reflection(episode: int, title: str | None = None, series_id: str | None = None,
                   persona_id: str = Query("default")):
    return svc.get_reflection(title=title, series_id=series_id, episode=episode, persona_id=persona_id)


@app.get("/recall", dependencies=[Depends(require_auth)])
def recall(keyword: str, title: str | None = None, series_id: str | None = None,
           persona_id: str | None = None):
    return svc.recall(title=title, series_id=series_id, keyword=keyword, persona_id=persona_id)


@app.get("/lines/search", dependencies=[Depends(require_auth)])
def search_lines(q: str, title: str | None = None, series_id: str | None = None,
                 episode: int | None = None, context: int = Query(8), limit: int = Query(5)):
    """うろ覚えのセリフ・出来事を全話横断であいまい検索。

    セリフ(hits)・場面説明(scene_hits)・あらすじ文(story_hits)を前後文脈つきで返す。
    """
    return svc.search_lines(title=title, series_id=series_id, q=q, episode=episode,
                            context=context, limit=limit)


# ── 圧縮ブロック（§4.5・作業コンテキストの中身） ──────────────────────
@app.get("/blocks", dependencies=[Depends(require_auth)])
def list_blocks(title: str | None = None, series_id: str | None = None):
    return svc.list_blocks(title=title, series_id=series_id)


@app.get("/blocks/{block_index}", dependencies=[Depends(require_auth)])
def get_block(block_index: int, title: str | None = None, series_id: str | None = None,
              persona_id: str = Query("default")):
    b = svc.get_block(title=title, series_id=series_id, block_index=block_index,
                      persona_id=persona_id)
    if not b.get("found"):
        raise HTTPException(404, "そのブロックはまだ封印されていない")
    return b


@app.get("/episodes/{episode}/block", dependencies=[Depends(require_auth)])
def get_block_for_episode(episode: int, title: str | None = None, series_id: str | None = None,
                          persona_id: str = Query("default")):
    b = svc.get_block(title=title, series_id=series_id, episode=episode, persona_id=persona_id)
    if not b.get("found"):
        raise HTTPException(404, "その話を含む圧縮ブロックはまだ無い")
    return b
