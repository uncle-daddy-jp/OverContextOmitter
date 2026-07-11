from oco.store_sqlite import SqliteStore
from oco.subtitles.ass_parser import Line


def _store(tmp_path):
    return SqliteStore(str(tmp_path / "oco.db"), "frieren-s2", title="葬送のフリーレン(第2期)")


def test_roundtrip_and_recall(tmp_path):
    s = _store(tmp_path)
    lines = [
        Line(start=1.0, end=3.0, text="この鍵は母さんの形見なんだ", speaker="#00FFFF"),
        Line(start=4.0, end=6.0, text="街の喧騒", speaker=None),
    ]
    s.write_lines(29, lines)
    s.write_transcript(29, "00:00:01 この鍵は母さんの形見なんだ")
    ppid = s.get_or_create_persona_prompt("default", "人格プロンプト")
    s.write_story(29, "フリーレンが謎の鍵を受け取る。", model="gemma", n_lines=2, n_masked=0)
    s.write_understanding(29, "default", "鍵が物語の核だと感じた。", persona_prompt_id=ppid)
    s.write_impression(29, "default", "あの鍵が気になってる。", persona_prompt_id=ppid)
    s.write_outlook(29, "default", "次回、鍵の謎が明かされそう。", persona_prompt_id=ppid)

    assert s.processed_episodes() == [29]
    assert s.read_transcript(29).startswith("00:00:01")
    assert "鍵" in s.read_story(29)
    assert "核" in s.read_understanding(29, "default")
    assert s.read_impression(29, "default") == "あの鍵が気になってる。"
    assert s.read_outlook(29, "default").startswith("次回")
    recs = s.read_lines(29)
    assert recs[0]["text"].startswith("この鍵")
    assert recs[0]["kind"] == "line"

    hits = s.recall("鍵", persona="default")
    types = {(h["ep"], h["type"]) for h in hits}
    assert (29, "line") in types
    assert (29, "story") in types
    assert (29, "impression") in types


def test_processed_requires_story(tmp_path):
    s = _store(tmp_path)
    s.write_lines(5, [Line(1.0, 2.0, "x", None)])
    assert s.processed_episodes() == []
    s.write_story(5, "y")
    assert s.processed_episodes() == [5]


def test_upsert_overwrites(tmp_path):
    s = _store(tmp_path)
    s.write_story(29, "古いあらすじ")
    s.write_story(29, "新しいあらすじ")
    assert s.read_story(29) == "新しいあらすじ"
    # 部分更新: understanding を書いても impression は消えない
    s.write_understanding(29, "default", "理解A")
    s.write_impression(29, "default", "感想B")
    s.write_understanding(29, "default", "理解C")
    assert s.read_understanding(29, "default") == "理解C"
    assert s.read_impression(29, "default") == "感想B"


def test_cast_carry_forward(tmp_path):
    s = _store(tmp_path)
    s.write_story(1, "第1話")
    s.write_cast(1, "フリーレン(魔法使い)、フェルン(弟子) — 師弟")
    s.write_story(2, "第2話")
    s.write_cast(2, "フリーレン(魔法使い)、フェルン(弟子) — 師弟。シュタルク — 同行する戦士")
    assert s.read_cast(1).startswith("フリーレン")
    # 引き継ぎ元は「ep より前で最後に確定したもの」
    assert s.read_latest_cast(2) == s.read_cast(1)
    assert "シュタルク" in s.read_latest_cast(3)    # ep3 の引き継ぎ元は ep2
    assert s.read_latest_cast(1) is None            # 先頭は引き継ぎ無し


def test_persona_prompt_dedup(tmp_path):
    s = _store(tmp_path)
    a = s.get_or_create_persona_prompt("default", "同じ本文")
    b = s.get_or_create_persona_prompt("default", "同じ本文")
    c = s.get_or_create_persona_prompt("default", "違う本文")
    assert a == b
    assert a != c
