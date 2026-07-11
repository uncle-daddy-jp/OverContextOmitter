from pathlib import Path

from oco.subtitles.ass_parser import parse_ass

FIXTURE = Path(__file__).parent / "fixtures" / "sample.ass"


def _lines():
    return parse_ass(FIXTURE.read_text(encoding="utf-8"))


def test_parses_all_dialogue_lines():
    lines = _lines()
    assert len(lines) == 4


def test_times_parsed_to_seconds():
    lines = _lines()
    assert lines[0].start == 1.0
    assert lines[2].start == 30.0
    assert lines[3].start == 60.0


def test_override_tags_stripped_and_newline_folded():
    lines = _lines()
    assert "{" not in lines[0].text
    assert lines[0].text == "フリーレン「この鍵は母さんの形見なんだ」"
    # \N が空白に畳まれる
    assert lines[3].text == "シュタルク「待ってくれ！」 まだ準備が"


def test_speaker_from_color_and_name():
    lines = _lines()
    # 色上書き → #RRGGBB キー
    assert lines[0].speaker == "#00FFFF"
    # 白(catch-all)は話者不明
    assert lines[1].speaker is None
    # Name フィールド優先
    assert lines[2].speaker == "フェルン"


def test_to_record_shape():
    rec = _lines()[0].to_record()
    assert set(rec) == {"t", "end", "speaker", "text", "kind"}
    assert rec["t"] == 1.0
    assert rec["kind"] == "line"
