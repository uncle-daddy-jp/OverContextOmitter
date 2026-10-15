from oco import prompts
from oco.prompts import render_transcript


def test_render_hhmmss_scene_and_color_key():
    recs = [
        {"t": 1281, "speaker": "フェルン", "text": "行きましょう", "kind": "line"},
        {"t": 1290, "text": "少女が立っている", "kind": "scene"},
        {"t": 5, "speaker": "#00FFFF", "text": "色キーは前置しない", "kind": "line"},
    ]
    out = render_transcript(recs).splitlines()
    assert out[0] == "00:21:21 （フェルン）行きましょう"   # 名前は前置
    assert out[1] == "00:21:30 〔映像〕少女が立っている"   # scene
    assert out[2] == "00:00:05 色キーは前置しない"          # #色 は前置しない


def _persona_passes(asr: bool):
    """3人格パス(②③③)を同じ引数で組み立て [(system, stable_prefix, volatile)] を返す。"""
    hist = [{"label": "第1話", "ep": 1, "story": "第1話のあらすじ", "impression": "感想",
             "outlook": "予想"}]
    return [
        prompts.build_understanding("人格", hist, "台本", "あらすじ", "相関", asr=asr),
        prompts.build_impression("人格", hist, "台本", "あらすじ", "理解", "相関", asr=asr),
        prompts.build_outlook("人格", hist, "あらすじ", "感想", "相関",
                              episode_transcript="台本", asr=asr),
    ]


def test_asr_warning_shared_position_in_persona_passes():
    passes = _persona_passes(asr=True)
    prefixes = {pre for _, pre, _ in passes}
    assert len(prefixes) == 1                                   # stable_prefix は完全一致
    assert "音声認識" not in prefixes.pop()                      # キャッシュ対象には入れない
    for _, _, vol in passes:
        # 3パスとも volatile の同一位置（人物相関ブロックの直後）に同一文字列で入る
        assert vol.startswith(prompts._cast_block("相関") + prompts._ASR_WARNING)


def test_no_asr_warning_when_captions():
    for _, pre, vol in _persona_passes(asr=False):
        assert "音声認識" not in pre and "音声認識" not in vol


def test_objective_corrections_are_appended_to_volatile_tail():
    issue = "- 因果関係の逆転: 停電の後に事件が起きた"
    _, _, plain = prompts.build_objective([], "台本", "相関")
    _, _, redo = prompts.build_objective([], "台本", "相関", corrections=issue)
    assert redo.startswith(plain)                               # 従来の本体は変えず末尾に足す
    assert redo.endswith(issue)
    assert "訂正して書き直せ" in redo


def test_story_check_asks_only_for_major_errors():
    sys_, pre_, vol = prompts.build_story_check("台本", "あらすじ")
    assert pre_ == ""
    assert "細部の省略" in vol                                   # 省略は指摘対象外＝仕様
    # 出力形式は serial判定と同じ「1行目に判定語」方式（逐条echoを封じる）
    assert "1行目: 「問題なし」か「大幅な誤りあり」のどちらか1語だけ" in vol
    assert "あらすじの引用・逐条の確認結果・書き直し案は書かない。" in vol
    assert "1行目は「問題なし」か「大幅な誤りあり」のどちらか1語だけ" in sys_


def test_serial_check_demands_one_word_first_line():
    _, _, vol = prompts.build_serial_check("台本", "デモ作品")
    assert "作品名: デモ作品" in vol
    assert "1行目: 「連続」か「単発」のどちらか1語だけ" in vol


def test_impression_asks_for_negative_feelings_too():
    _, _, vol = prompts.build_impression("人格", [], "台本", "あらすじ", "理解")
    assert "動かなかった所" in vol and "退屈・不満・違和感" in vol
    assert "理解の言い直しでなく" in vol                          # 従来の指示は維持
