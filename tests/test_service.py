import pytest

from oco import service as service_mod
from oco.config import Config, LLMConfig
from oco.db import transaction
from oco.pipeline import build_store
from oco.service import AmbiguousSeries, OcoService, SeriesNotFound


def _cfg(tmp_path) -> Config:
    return Config(
        store_dir=tmp_path,
        text=LLMConfig(provider="openai_compat", model="stub", base_url="http://x/v1"),
        db_path=tmp_path / "oco.db",
        media_root=tmp_path,                 # テストでは tmp を許可ルートに
    )


def _make_ass(tmp_path, name="x.ass") -> str:
    p = tmp_path / name
    p.write_text("dummy", encoding="utf-8")
    return str(p)


def test_title_resolution_exact_partial_ambiguous(tmp_path):
    svc = OcoService(_cfg(tmp_path))
    svc._get_or_create_series("葬送のフリーレン", None, ["Frieren"])
    svc._get_or_create_series("葬送のフリーレン(第2期)", None, [])

    assert svc.resolve_series(title="葬送のフリーレン")["title"] == "葬送のフリーレン"
    assert svc.resolve_series(title="Frieren")["title"] == "葬送のフリーレン"   # 別名
    with pytest.raises(AmbiguousSeries):
        svc.resolve_series(title="フリーレン")                                  # 部分一致2件
    with pytest.raises(SeriesNotFound):
        svc.resolve_series(title="存在しない作品")


def test_get_episode_groups_three_outputs(tmp_path):
    cfg = _cfg(tmp_path)
    svc = OcoService(cfg)
    s = svc._get_or_create_series("テスト作品", None, [])
    store = build_store(cfg, s["series_id"], s["title"])
    store.write_transcript(1, "00:00:01 セリフ")
    store.write_story(1, "客観あらすじ")
    store.write_understanding(1, "default", "人格理解")
    store.write_impression(1, "default", "感想")
    store.write_outlook(1, "default", "予想")

    b = svc.get_episode(title="テスト作品", episode=1, persona_id="default")
    assert b["transcript"].startswith("00:00:01")
    assert b["story"] == "客観あらすじ"
    assert b["understanding"] == "人格理解"
    assert b["review"] == {"impression": "感想", "outlook": "予想"}


def test_submit_sync_and_skip(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    svc = OcoService(cfg)
    media = _make_ass(tmp_path)
    calls = {"n": 0}

    def fake_process(cfg_, series_id, ep, ass_path, persona_id, persona_text,
                     title=None, video_path=None):
        calls["n"] += 1
        store = build_store(cfg_, series_id, title)
        store.write_transcript(ep, "00:00:01 x")
        store.write_story(ep, "S")
        store.write_understanding(ep, persona_id, "U")
        store.write_impression(ep, persona_id, "I")
        store.write_outlook(ep, persona_id, "O")

    monkeypatch.setattr(service_mod, "process_episode", fake_process)

    res = svc.submit(title="作品X", episode=3, media_path=media,
                     persona_id="default", persona_prompt="P", wait=True)
    assert res["state"] == "done"
    assert res["result"]["story"] == "S"          # 正規化: bundle は result に
    assert res["episode"] == 3
    assert calls["n"] == 1

    res2 = svc.submit(title="作品X", episode=3, media_path=media,
                      persona_id="default", persona_prompt="P", wait=True, overwrite=False)
    assert res2.get("skipped") is True
    assert res2["result"]["story"] == "S"          # スキップも同形
    assert calls["n"] == 1                          # 再生成しない


def test_submit_ambiguous_title_propagates_not_creates(tmp_path):
    cfg = _cfg(tmp_path)
    svc = OcoService(cfg)
    svc._get_or_create_series("葬送のフリーレン", None, [])
    svc._get_or_create_series("葬送のフリーレン(第2期)", None, [])
    media = _make_ass(tmp_path)
    before = len(svc.list_series())
    with pytest.raises(AmbiguousSeries):
        svc.submit(title="フリーレン", episode=1, media_path=media, wait=True)
    assert len(svc.list_series()) == before        # 黙って新規作成しない


def test_aliases_merged_on_existing(tmp_path):
    svc = OcoService(_cfg(tmp_path))
    svc._get_or_create_series("作品Z", None, ["A"])
    svc._get_or_create_series("作品Z", None, ["B"])      # 既存に別名追加
    assert svc.resolve_series(title="A")["title"] == "作品Z"
    assert svc.resolve_series(title="B")["title"] == "作品Z"


def _insert_running_job(cfg, sid, episode, persona_id, job_id="preexisting", age_sec=0):
    """oco_jobs に running のジョブ行を直接差し込む（age_sec だけ過去に created/updated）。"""
    with transaction(cfg.db_path) as conn:
        conn.execute(
            "INSERT INTO oco_jobs (job_id, series_id, episode_number, persona_id, state, phase, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, 'running', 'processing', "
            "datetime('now', ?), datetime('now', ?))",
            (job_id, sid, episode, persona_id, f"-{age_sec} seconds", f"-{age_sec} seconds"),
        )


def _job_count(cfg) -> int:
    with transaction(cfg.db_path) as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM oco_jobs").fetchone()["c"]


def _fake_process_full(calls):
    def fake_process(cfg_, series_id, ep, ass_path, persona_id, persona_text,
                     title=None, video_path=None):
        calls["n"] += 1
        store = build_store(cfg_, series_id, title)
        store.write_transcript(ep, "00:00:01 x")
        store.write_story(ep, "S")
        store.write_understanding(ep, persona_id, "U")
        store.write_impression(ep, persona_id, "I")
        store.write_outlook(ep, persona_id, "O")
    return fake_process


def test_inflight_dedup_running_returns_already_running(tmp_path, monkeypatch):
    """(a) running中の同一キーPOSTは新規ジョブを作らず already_running を即返す。"""
    cfg = _cfg(tmp_path)
    svc = OcoService(cfg)
    media = _make_ass(tmp_path)
    s = svc._get_or_create_series("作品IF", None, [])
    sid = s["series_id"]
    _insert_running_job(cfg, sid, 5, "default", job_id="running1")

    calls = {"n": 0}
    monkeypatch.setattr(service_mod, "process_episode", _fake_process_full(calls))

    res = svc.submit(title="作品IF", episode=5, media_path=media,
                     persona_id="default", persona_prompt="P", wait=True)
    assert res.get("already_running") is True
    assert res["state"] == "running"
    assert res["job_id"] == "running1"          # 既存ジョブを返す
    assert calls["n"] == 0                        # 処理は起動しない
    assert _job_count(cfg) == 1                   # 新規ジョブ行は作られない


def test_inflight_stale_over_3h_creates_new_job(tmp_path, monkeypatch):
    """(b) updated_at が3時間より古い遺骸は in-flight とみなさず新規作成する。"""
    cfg = _cfg(tmp_path)
    svc = OcoService(cfg)
    media = _make_ass(tmp_path)
    s = svc._get_or_create_series("作品IF", None, [])
    sid = s["series_id"]
    _insert_running_job(cfg, sid, 5, "default", job_id="stale1", age_sec=4 * 3600)

    calls = {"n": 0}
    monkeypatch.setattr(service_mod, "process_episode", _fake_process_full(calls))

    res = svc.submit(title="作品IF", episode=5, media_path=media,
                     persona_id="default", persona_prompt="P", wait=True)
    assert res.get("already_running") is not True
    assert res["state"] == "done"
    assert calls["n"] == 1                         # 新規に処理が走る
    assert _job_count(cfg) == 2                     # 遺骸 + 新規


def test_inflight_dedup_scoped_to_episode_and_persona(tmp_path, monkeypatch):
    """(c) 別エピソード / 別persona は重複排除されず新規作成される。"""
    cfg = _cfg(tmp_path)
    svc = OcoService(cfg)
    media = _make_ass(tmp_path)
    s = svc._get_or_create_series("作品IF", None, [])
    sid = s["series_id"]
    _insert_running_job(cfg, sid, 5, "default", job_id="run5def")

    calls = {"n": 0}
    monkeypatch.setattr(service_mod, "process_episode", _fake_process_full(calls))

    # 別エピソード（ep=6）→ 新規
    r6 = svc.submit(title="作品IF", episode=6, media_path=media,
                    persona_id="default", persona_prompt="P", wait=True)
    assert r6.get("already_running") is not True
    assert r6["state"] == "done"

    # 同一エピソードだが別persona → 新規
    r5o = svc.submit(title="作品IF", episode=5, media_path=media,
                     persona_id="other", persona_prompt="P", wait=True)
    assert r5o.get("already_running") is not True
    assert r5o["state"] == "done"

    assert calls["n"] == 2


def test_recall_delegates(tmp_path):
    cfg = _cfg(tmp_path)
    svc = OcoService(cfg)
    s = svc._get_or_create_series("作品Y", None, [])
    store = build_store(cfg, s["series_id"], s["title"])
    store.write_story(1, "鍵の話")
    res = svc.recall(title="作品Y", keyword="鍵")
    assert any(h["type"] == "story" for h in res["hits"])


def test_search_lines_delegates(tmp_path):
    from oco.subtitles.ass_parser import Line

    cfg = _cfg(tmp_path)
    svc = OcoService(cfg)
    s = svc._get_or_create_series("作品Z", None, [])
    store = build_store(cfg, s["series_id"], s["title"])
    store.write_lines(14, [Line(1.0, 2.0, "こんなところにガムなんかつけて", "脇坂")])
    store.write_story(14, "港の倉庫街を調べる話")

    res = svc.search_lines(title="作品Z", q="ガムついてる")
    assert res["title"] == "作品Z" and res["q"] == "ガムついてる"
    assert res["hits"][0]["episode"] == 14
    assert res["stories"]["14"] == "港の倉庫街を調べる話"

    with pytest.raises(SeriesNotFound):
        svc.search_lines(title="無い作品", q="ガム")


def test_block_getters(tmp_path):
    cfg = _cfg(tmp_path)
    svc = OcoService(cfg)
    s = svc._get_or_create_series("圧縮作品", None, [])
    store = build_store(cfg, s["series_id"], s["title"])
    store.write_block(1, 1, 5, 5, "第1-5話の圧縮あらすじ", model="m")
    store.write_block_view(1, "default", "第1-5話の主観ダイジェスト", model="m")

    lst = svc.list_blocks(title="圧縮作品")
    assert lst["blocks"] == [{"block_index": 1, "ep_lo": 1, "ep_hi": 5, "block_size": 5}]

    b = svc.get_block(title="圧縮作品", block_index=1, persona_id="default")
    assert b["found"] and b["story"] == "第1-5話の圧縮あらすじ"
    assert b["digest"] == "第1-5話の主観ダイジェスト"

    b2 = svc.get_block(title="圧縮作品", episode=3, persona_id="default")   # 話→カバーするブロック逆引き
    assert b2["found"] and b2["block_index"] == 1

    b3 = svc.get_block(title="圧縮作品", block_index=99, persona_id="default")
    assert b3["found"] is False
