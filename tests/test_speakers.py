from oco.speakers import attribute_speakers
from oco.speakers.naming import _label, apply_names, name_speakers
from oco.subtitles.ass_parser import Line


class FakeLLM:
    def __init__(self, out):
        self._out = out

    def complete(self, **kwargs):
        return self._out


def test_label_sequence():
    assert _label(0) == "話者A"
    assert _label(25) == "話者Z"
    assert _label(26) == "話者AA"


def test_name_and_apply_uses_address_cue():
    lines = [
        Line(1.0, 2.0, "フリーレン！", speaker="#00FF00"),   # 話者A が呼びかけ
        Line(2.0, 3.0, "なに？", speaker="#00FFFF"),          # 話者B＝フリーレン
        Line(3.0, 4.0, "行こう", speaker="#00FFFF"),
    ]
    llm = FakeLLM("話者A=シュタルク\n話者B=フリーレン")
    m = name_speakers(llm, lines, "（人物相関）")
    assert m == {"#00FF00": "シュタルク", "#00FFFF": "フリーレン"}
    apply_names(lines, m)
    assert lines[0].speaker == "シュタルク"
    assert lines[1].speaker == "フリーレン"


def test_unknown_is_dropped():
    lines = [Line(1, 2, "a", speaker="#A"), Line(2, 3, "b", speaker="#B")]
    m = name_speakers(FakeLLM("話者A=ケイ\n話者B=unknown"), lines, None)
    assert m == {"#A": "ケイ"}                 # unknown は除外
    assert "#B" not in m


def test_single_cluster_is_noop():
    lines = [Line(1, 2, "a", speaker="#A"), Line(2, 3, "b", speaker="#A")]
    assert name_speakers(FakeLLM("x"), lines, None) == {}


def test_dispatch_cluster_mode():
    # 色クラスタ2つ → クラスタ命名モード
    lines = [Line(1, 2, "x", speaker="#A"), Line(2, 3, "y", speaker="#B")]
    s = attribute_speakers(FakeLLM("話者A=ケイ\n話者B=ユリ"), lines, None)
    assert s["mode"] == "cluster" and s["changed"]
    assert lines[0].speaker == "ケイ" and lines[1].speaker == "ユリ"


def test_dispatch_per_line_cleans_and_filters():
    # 話者None(ASR) → 行単位推定モード。注釈除去・長文/疑問符を弾く
    lines = [Line(1, 2, "フリーレン！", speaker=None),
             Line(2, 3, "なに？", speaker=None),
             Line(3, 4, "…", speaker=None)]
    out = "1=シュタルク (Context: calling)\n2=フリーレン\n3=ブライアンの本体はどこ?"
    s = attribute_speakers(FakeLLM(out), lines, "（人物相関）")
    assert s["mode"] == "per_line"
    assert lines[0].speaker == "シュタルク"     # 注釈(Context)除去
    assert lines[1].speaker == "フリーレン"
    assert lines[2].speaker is None             # 長文＋疑問符は弾く
