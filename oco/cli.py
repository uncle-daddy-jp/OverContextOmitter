"""OverContextOmitter CLI（service 経由＝API/MCP と同一ロジック）。

  oco process --title <作品名> --ep <N> --media <file.ass> [--persona <id>] [--series-id S]
  oco get     --title <作品名> --ep <N> [--persona <id>]      # ①②③+生字幕まとめ
  oco recall  --title <作品名> <keyword> [--persona <id>]
  oco list    [--title <作品名>]                               # 作品一覧 / 話一覧
  oco extract --ts <file> --ass <out>                         # TS→.ass（ffmpeg・要TS）
  oco probe   --ts <file>                                     # 字幕ストリーム確認
  oco embed   --all | --title <作品名>                          # 意味検索のベクトルを埋め戻す
  oco serve-api                                               # ローカルデバッグ用に API 起動
"""

from __future__ import annotations

import argparse
import sys

from .config import Config
from .service import AmbiguousSeries, InvalidMediaPath, OcoService, SeriesNotFound


def _svc(args) -> OcoService:
    return OcoService(Config.load(args.config))


def _print_bundle(b: dict) -> None:
    r = b.get("result") or b  # skip/wait は result に bundle、async は b 自身
    print(f"[{r.get('title', b.get('title'))}] 第{r.get('episode', b.get('episode'))}話 "
          f"/ persona={r.get('persona_id', b.get('persona_id'))} "
          f"(state={b.get('state', '?')}{'・skipped' if b.get('skipped') else ''})")
    if not r.get("story"):
        print(f"  (未生成 / state={b.get('state')})")
        return
    review = r.get("review", {})
    if r.get("cast"):
        print(f"\n── 人物相関 ──\n{r.get('cast')}")
    print(f"\n── ① ストーリー（客観） ──\n{r.get('story')}")
    print(f"\n── ② 物語理解（人格） ──\n{r.get('understanding')}")
    print(f"\n── ③ 感想 ──\n{review.get('impression')}")
    print(f"\n── ③ 予想・期待 ──\n{review.get('outlook')}")


def _cmd_process(args) -> int:
    svc = _svc(args)
    prompt = None
    if args.persona_prompt_file:
        with open(args.persona_prompt_file, encoding="utf-8") as f:
            prompt = f.read()
    try:
        res = svc.submit(
            title=args.title, episode=args.ep, media_path=args.media,
            persona_id=args.persona, persona_prompt=prompt, series_id=args.series_id,
            video_path=args.video, overwrite=args.overwrite, wait=True,
        )
    except AmbiguousSeries as e:
        print(f"作品名が曖昧。series-id を指定するか正式名を: {[c['title'] for c in e.candidates]}")
        return 2
    except (InvalidMediaPath, SeriesNotFound, ValueError, FileNotFoundError) as e:
        print(f"エラー: {e}")
        return 1
    _print_bundle(res)
    return 0


def _cmd_get(args) -> int:
    svc = _svc(args)
    try:
        b = svc.get_episode(title=args.title, series_id=args.series_id,
                            episode=args.ep, persona_id=args.persona)
    except AmbiguousSeries as e:
        print(f"作品名が曖昧。候補: {[c['title'] for c in e.candidates]}")
        return 2
    except SeriesNotFound as e:
        print(str(e))
        return 1
    if args.transcript:
        print(f"── 整形済み生字幕 ──\n{b.get('transcript')}")
    _print_bundle(b)
    return 0


def _cmd_recall(args) -> int:
    svc = _svc(args)
    res = svc.recall(title=args.title, series_id=args.series_id,
                     keyword=args.keyword, persona_id=args.persona)
    hits = res.get("hits", [])
    if not hits:
        print(f"(no hits) {args.keyword!r}")
        return 0
    for h in hits:
        print(f"  ep{h['ep']:02d} [{h['type']}] {h['excerpt']}")
    return 0


def _cmd_list(args) -> int:
    svc = _svc(args)
    if args.title:
        res = svc.list_episodes(title=args.title, series_id=args.series_id)
        print(f"[{res['title']}]")
        for e in res["episodes"]:
            flags = "".join(k for k, v in
                            [("生", e["has_transcript"]), ("①", e["has_story"]),
                             ("②", e["has_understanding"]), ("感", e["has_impression"]),
                             ("予", e["has_outlook"])] if v)
            print(f"  第{e['ep']:>3}話  {flags}")
    else:
        for s in svc.list_series(query=args.query):
            print(f"  {s['title']}  ({s['n_episodes']}話) [{s['series_id']}]")
    return 0


def _cmd_extract(args) -> int:
    from .subtitles.extract import extract_ass

    print(f"[extract] {extract_ass(args.ts, args.ass)}")
    return 0


def _cmd_probe(args) -> int:
    from .subtitles.extract import probe_subtitle_streams

    print(probe_subtitle_streams(args.ts))
    return 0


def _cmd_embed(args) -> int:
    """処理済みの話の文書ベクトルを作り直す（過去話の埋め戻し）。"""
    from .embed import embed_episode
    from .pipeline import build_store

    svc = _svc(args)
    if not svc.cfg.embed_base_url:
        print("エラー: config に [embed] base_url が無い")
        return 1
    if args.all:
        targets = svc.list_series()
    else:
        try:
            targets = [svc.resolve_series(title=args.title, series_id=args.series_id)]
        except (AmbiguousSeries, SeriesNotFound) as e:
            print(f"エラー: {e}")
            return 1
    n_ep = n_doc = n_fail = 0
    for s in targets:
        store = build_store(svc.cfg, s["series_id"], s["title"])
        for ep in store.processed_episodes():
            try:
                n = embed_episode(svc.cfg, store, ep)
            except Exception as e:
                n_fail += 1
                print(f"  失敗 [{s['title']}] 第{ep}話: {e!r}")
                continue
            n_ep += 1
            n_doc += n
            print(f"  [{s['title']}] 第{ep}話  {n}件")
    print(f"完了: {n_ep}話 {n_doc}件（失敗 {n_fail}話）")
    return 1 if n_fail else 0


def _cmd_serve_api(args) -> int:
    import uvicorn

    cfg = Config.load(args.config)
    uvicorn.run("oco.api:app", host=cfg.api.host, port=cfg.api.port, workers=1)
    return 0


def main(argv: list[str] | None = None) -> int:
    import logging

    logging.basicConfig(level=logging.INFO, format="%(message)s")  # scene カバレッジ等を表示
    p = argparse.ArgumentParser(prog="oco", description="番組理解コア（OverContextOmitter）")
    p.add_argument("--config", default=None, help="設定ファイル（既定 $OCO_CONFIG）")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("process", help="第N話を処理して①②③+生字幕を SQLite に書く")
    sp.add_argument("--title", required=True, help="作品名")
    sp.add_argument("--ep", type=int, required=True, help="第〇話")
    sp.add_argument("--media", required=True, help=".ass パス")
    sp.add_argument("--video", default=None, help="映像 scene 抽出用の動画(.ts等・任意・vision設定時)")
    sp.add_argument("--persona", default="default")
    sp.add_argument("--persona-prompt-file", default=None, help="人格プロンプトをファイルで直指定")
    sp.add_argument("--series-id", default=None)
    sp.add_argument("--overwrite", action="store_true", help="既処理でも再生成")
    sp.set_defaults(func=_cmd_process)

    sp = sub.add_parser("get", help="作品名+話で①②③（+生字幕）を取り出す")
    sp.add_argument("--title", default=None)
    sp.add_argument("--series-id", default=None)
    sp.add_argument("--ep", type=int, required=True)
    sp.add_argument("--persona", default="default")
    sp.add_argument("--transcript", action="store_true", help="整形済み生字幕も表示")
    sp.set_defaults(func=_cmd_get)

    sp = sub.add_parser("recall", help="作品内をキーワード横断検索")
    sp.add_argument("--title", default=None)
    sp.add_argument("--series-id", default=None)
    sp.add_argument("keyword")
    sp.add_argument("--persona", default=None)
    sp.set_defaults(func=_cmd_recall)

    sp = sub.add_parser("list", help="作品一覧（--title で話一覧）")
    sp.add_argument("--title", default=None)
    sp.add_argument("--series-id", default=None)
    sp.add_argument("--query", default=None)
    sp.set_defaults(func=_cmd_list)

    sp = sub.add_parser("extract", help="TS→.ass（ffmpeg）")
    sp.add_argument("--ts", required=True)
    sp.add_argument("--ass", required=True)
    sp.set_defaults(func=_cmd_extract)

    sp = sub.add_parser("probe", help="TS の字幕ストリーム確認")
    sp.add_argument("--ts", required=True)
    sp.set_defaults(func=_cmd_probe)

    sp = sub.add_parser("embed", help="意味で引く検索用のベクトルを作る（過去話の埋め戻し）")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--all", action="store_true", help="全作品の処理済みの話")
    g.add_argument("--title", default=None)
    sp.add_argument("--series-id", default=None)
    sp.set_defaults(func=_cmd_embed)

    sp = sub.add_parser("serve-api", help="API をローカル起動（デバッグ用）")
    sp.set_defaults(func=_cmd_serve_api)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
