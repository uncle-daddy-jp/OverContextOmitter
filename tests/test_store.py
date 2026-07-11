from oco.store import SeriesStore
from oco.subtitles.ass_parser import Line


def test_write_read_roundtrip_and_recall(tmp_path):
    store = SeriesStore(tmp_path, "frieren-s2")
    lines = [
        Line(start=1.0, end=3.0, text="この鍵は母さんの形見なんだ", speaker="#00FFFF"),
        Line(start=4.0, end=6.0, text="街の喧騒", speaker=None),
    ]
    store.write_lines(29, lines)
    store.write_story(29, "フリーレンが謎の鍵を受け取る。")
    store.write_impression(29, "default", "あの鍵が気になってる。")
    store.write_outlook(29, "default", "次回、鍵の謎が明かされそう。")

    assert store.processed_episodes() == [29]
    assert store.read_lines(29)[0]["text"].startswith("この鍵")
    assert "鍵" in store.read_story(29)

    hits = store.recall("鍵", persona="default")
    types = {(h["ep"], h["type"]) for h in hits}
    assert (29, "line") in types
    assert (29, "story") in types
    assert (29, "impression") in types


def test_processed_episodes_requires_story(tmp_path):
    store = SeriesStore(tmp_path, "s")
    store.write_lines(5, [Line(1.0, 2.0, "x", None)])
    # story が無ければ processed 扱いしない
    assert store.processed_episodes() == []
    store.write_story(5, "y")
    assert store.processed_episodes() == [5]
