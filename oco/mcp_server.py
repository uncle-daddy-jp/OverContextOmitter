"""FastMCP(stdio) 薄いアダプタ。OcoService へ委譲するだけ。

入力(oco_submit_episode)と取り出し(作品名+話で①②③+生字幕)を MCP ツールとして公開。
stdio はクライアントが `docker compose run --rm -T oco-mcp` 等で spawn する想定。
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .config import Config
from .service import AmbiguousSeries, InvalidMediaPath, OcoService, SeriesNotFound

_svc: OcoService | None = None
mcp = FastMCP("OverContextOmitter")


def _service() -> OcoService:
    global _svc
    if _svc is None:
        _svc = OcoService(Config.load())
    return _svc


def _safe(fn):
    """想定内エラーを例外でなく構造化レスポンスに（MCPクライアントが扱いやすい形）。"""
    try:
        return fn()
    except AmbiguousSeries as e:
        return {"ambiguous": True, "candidates": e.candidates}
    except SeriesNotFound as e:
        return {"error": "series_not_found", "detail": str(e)}
    except InvalidMediaPath as e:
        return {"error": "invalid_media_path", "detail": str(e)}
    except (ValueError, FileNotFoundError) as e:
        return {"error": "bad_request", "detail": str(e)}


@mcp.tool()
def oco_submit_episode(
    title: str, episode: int, media_path: str, persona_id: str = "default",
    persona_prompt: str | None = None, aliases: list[str] | None = None,
    series_id: str | None = None, video_path: str | None = None,
    webhook_url: str | None = None, wait: bool = True, overwrite: bool = False,
) -> dict:
    """録画(.ass)を処理し①あらすじ②人格理解③感想・予想＋整形済み生字幕をSQLiteへ保存。

    media_path はコンテナ内パス(/recordings/...・.ass)。video_path を渡すと映像 scene も抽出。
    既定同期(wait=true)。
    """
    return _safe(lambda: _service().submit(
        title=title, episode=episode, media_path=media_path, persona_id=persona_id,
        persona_prompt=persona_prompt, aliases=aliases or [], series_id=series_id,
        video_path=video_path, webhook_url=webhook_url, wait=wait, overwrite=overwrite,
    ))


@mcp.tool()
def oco_get_episode(title: str, episode: int, persona_id: str = "default",
                    series_id: str | None = None) -> dict:
    """作品名+第〇話で ①ストーリー②人格理解③感想・予想＋生字幕 をまとめ取り。"""
    return _safe(lambda: _service().get_episode(
        title=title, series_id=series_id, episode=episode, persona_id=persona_id))


@mcp.tool()
def oco_get_transcript(title: str, episode: int, series_id: str | None = None) -> dict:
    """整形済み生字幕(HH:MM:SS・客観・人格非依存)のみ取り出す。"""
    return _safe(lambda: _service().get_transcript(
        title=title, series_id=series_id, episode=episode))


@mcp.tool()
def oco_get_story(title: str, episode: int, series_id: str | None = None) -> dict:
    """① 客観ストーリーのみ（persona引数なし＝人格非依存を担保）。"""
    return _safe(lambda: _service().get_story(
        title=title, series_id=series_id, episode=episode))


@mcp.tool()
def oco_get_cast(title: str, episode: int, series_id: str | None = None) -> dict:
    """人物相関（その話時点・客観・人格非依存）。"""
    return _safe(lambda: _service().get_cast(
        title=title, series_id=series_id, episode=episode))


@mcp.tool()
def oco_get_understanding(title: str, episode: int, persona_id: str = "default",
                          series_id: str | None = None) -> dict:
    """② 人格レンズの物語理解。"""
    return _safe(lambda: _service().get_understanding(
        title=title, series_id=series_id, episode=episode, persona_id=persona_id))


@mcp.tool()
def oco_get_reflection(title: str, episode: int, persona_id: str = "default",
                       series_id: str | None = None) -> dict:
    """③ 感想＋予想・期待（impression+outlook を1単位で）。"""
    return _safe(lambda: _service().get_reflection(
        title=title, series_id=series_id, episode=episode, persona_id=persona_id))


@mcp.tool()
def oco_recall(title: str, keyword: str, persona_id: str | None = None,
               series_id: str | None = None) -> dict:
    """全話横断キーワード検索（LIKE・line/story/impression）。"""
    return _safe(lambda: _service().recall(
        title=title, series_id=series_id, keyword=keyword, persona_id=persona_id))


@mcp.tool()
def oco_list_blocks(title: str, series_id: str | None = None) -> dict:
    """§4.5 封印済みの圧縮ブロック一覧（block_index / ep範囲 / block_size）。"""
    return _safe(lambda: _service().list_blocks(title=title, series_id=series_id))


@mcp.tool()
def oco_get_block(title: str, block_index: int | None = None, episode: int | None = None,
                  persona_id: str = "default", series_id: str | None = None) -> dict:
    """§4.5 圧縮版ストーリー（客観 story）＋主観ダイジェスト（persona別）。

    block_index を直接、または episode でその話を含むブロックを逆引き。未封印なら found=False。
    """
    return _safe(lambda: _service().get_block(
        title=title, series_id=series_id, block_index=block_index,
        episode=episode, persona_id=persona_id))


@mcp.tool()
def oco_list_series(query: str | None = None) -> dict:
    """作品一覧（query 部分一致フィルタ任意）。"""
    return {"series": _service().list_series(query=query)}


@mcp.tool()
def oco_list_episodes(title: str, series_id: str | None = None) -> dict:
    """処理済み話一覧と各成果物の有無。"""
    return _safe(lambda: _service().list_episodes(title=title, series_id=series_id))


@mcp.tool()
def oco_get_job_status(job_id: str) -> dict:
    """非同期(wait=false)ジョブの進捗。"""
    j = _service().get_job_status(job_id)
    return j or {"error": "unknown_job", "job_id": job_id}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
