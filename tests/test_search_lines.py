from oco.store_sqlite import (
    SqliteStore, _norm, _score, _bigrams, _rare_floor, _rare_ok_bigrams,
    _chunk_candidates, _sentences,
)
from oco.subtitles.ass_parser import Line


def _store(tmp_path):
    return SqliteStore(str(tmp_path / "oco.db"), "vivant", title="VIVANT")


def _line(text, speaker=None):
    return Line(start=0.0, end=1.0, text=text, speaker=speaker)


def _scene(text):
    return Line(start=0.0, end=1.0, text=text, speaker=None, kind="scene")


# 「ついてる」を6行に散らして、つい/いて/てる を df 大（＝ありふれた側）にした素材。
# 先頭行だけが「ガム」を含む＝希少な2-gram。
_TSUITERU_LINES = [
    "こんなところにガムなんかつけて",
    "新庄についてるとは ちょっとねえ",
    "そっちの部隊についてる連絡員は誰だ",
    "モニターの電源がついてるはずだ",
    "運がついてると思ったんだけどな",
    "電気がついてるみたいだね",
    "ずっと見張りがついてるらしい",
]

# 「よう」を df 大にした素材（短いクエリの誤爆確認用）。
_YOU_LINES = [
    "しつよう",
    "ようやく着いたぞ",
    "どうしようもない",
    "重要な話がある",
    "様子を見ようか",
    "そうしようと思う",
]


def _ep14(store, ep=14):
    """港の倉庫街の場面（scene 1本 + セリフ）。ガムのくだりは2行に割れている。"""
    lines = [
        _scene("港の倉庫街。夜、コンテナの間を歩く二人。"),
        _line("この先に何かあるのか", "野崎守"),
        _line("さあな", "黒須駿"),
        _line("こんなところに", "脇坂昭夫"),          # ← 1発話が2行に割れた前半
        _line("ガムなんかつけて", "脇坂昭夫"),        # ← 後半
        _line("誰かの目印かもしれません", "野崎守"),
        _scene("倉庫の扉が開く。"),
        _line("行くぞ", "乃木憂助"),
    ]
    for i, ln in enumerate(lines):
        ln.start = float(i * 10)
    store.write_lines(ep, lines)
    store.write_story(ep, "タイでの新庄浩太郎の足取りを追い、港の倉庫街へ向かう。")


_NOZAKI_LINES = [
    "野崎さん どこへ行くんですか",
    "野崎の行方は？",
    "野崎さんの車が見つかりました",
    "野崎はもう戻らないだろう",
    "野崎の足取りを追う",
]
_NOZAKI_STORY = (
    "タイでの新庄浩太郎の確保とその後が描かれる。"
    "一方、野崎守は消息を絶ち、公安の佐野部長は既に察知していた。\n\n"
    "東条は警視庁を脱出し、日本海への密航を目論む。"
)


def _nozaki(store, ep=14):
    """野崎の失踪まわり（scene 2本＋セリフ5行＋あらすじ3文）。

    「野崎」を5行に散らして df 大にしてある（ライン側の希少df下駄を発火させない）。
    """
    lines = [
        _scene("夜の埠頭に停まった車から野崎守が降り、暗がりへ走り去る。"),
        _line(_NOZAKI_LINES[0], "黒須駿"),
        _scene("残されたスマートフォンが路上で光っている。"),
        _line(_NOZAKI_LINES[1], "乃木憂助"),
        _line(_NOZAKI_LINES[2], "太田梨歌"),
        _line(_NOZAKI_LINES[3], "上原歩美"),
        _line(_NOZAKI_LINES[4], "乃木憂助"),
    ]
    for i, ln in enumerate(lines):
        ln.start = float(i * 10)
    store.write_lines(ep, lines)
    store.write_story(ep, _NOZAKI_STORY)


# ── 正規化 ─────────────────────────────────────────────
def test_norm_kana_zenkaku_symbols():
    # カタカナ→ひらがな・記号/空白除去・全角英数→半角小文字
    assert _norm("ガム、ついてる…") == "がむついてる"
    assert _norm("ｶﾞﾑ ついてる") == "がむついてる"          # 半角カナ＋濁点も NFKC で合成
    assert _norm("「ガムついてる」") == "がむついてる"
    assert _norm("ＡＢＣ！") == "abc"
    assert _norm("〓（）・") == ""
    assert _norm("コーヒー") == "こーひー"                   # 長音符は残す
    assert _norm("") == ""


def test_score_substring_bonus():
    """短いクエリでも部分一致ボーナスで閾値を越える／無関係な文は伸びない。"""
    nq = _norm("ガム")
    bq = _bigrams(nq)
    hit, m_hit, sub_hit = _score(nq, bq, _norm("こんなところにガムなんかつけて"))
    miss, m_miss, sub_miss = _score(nq, bq, _norm("この先に何かあるのか"))
    assert hit >= 0.25 and hit > miss
    assert miss < 0.25
    assert (m_hit, sub_hit) == ({"がむ"}, True)
    assert (m_miss, sub_miss) == (set(), False)
    # 完全一致は 1.0 でクリップされる
    assert _score(nq, bq, nq)[0] == 1.0
    # 1文字クエリ（2-gram を作れない縮退）は部分一致だけで判定（入力は正規化済み前提）
    assert _score(_norm("ガ"), set(), _norm("ガムなんか")) == (0.35, set(), True)
    assert _score(_norm("ガ"), set(), _norm("この先に")) == (0.0, set(), False)


def test_rare_floor_by_document_frequency():
    """珍しい2-gramの一致には df に応じた下限スコアが立つ／ありふれた側は立たない。"""
    df = {"がむ": 1, "むつ": 2, "つい": 3, "いて": 4}
    assert _rare_floor({"がむ"}, df) == 0.60
    assert _rare_floor({"むつ"}, df) == 0.55
    assert _rare_floor({"つい"}, df) == 0.50
    assert _rare_floor({"いて"}, df) == 0.0          # df が大きい＝ありふれている
    assert _rare_floor({"いて", "がむ"}, df) == 0.60  # 最も珍しい一致を採る
    assert _rare_floor(set(), df) == 0.0
    assert _rare_floor({"てし"}, df) == 0.0          # どの行にも無い(df=0)なら下駄無し


def test_rare_ok_bigrams_excludes_hiragana_only():
    """下駄が効くのはカタカナ・漢字・英数を含む2-gramだけ（ひらがなだけは対象外）。"""
    assert _rare_ok_bigrams("ガムついてる") == {"がむ", "むつ"}   # カタカナを含む2つだけ
    assert _rare_ok_bigrams("おはよう") == set()                  # 全部ひらがな
    assert _rare_ok_bigrams("別班") == {"別班"}
    assert _rare_ok_bigrams("ノギさん") == {"のぎ", "ぎさ"}       # 境目の1文字でも可


# ── 検索本体 ───────────────────────────────────────────
def test_search_hits_with_context_and_scene(tmp_path):
    s = _store(tmp_path)
    _ep14(s)
    res = s.search_lines("目印かもしれません", context=2)

    assert res["found"] is True
    top = res["hits"][0]
    assert top["episode"] == 14
    assert top["line_seq"] == 5
    assert top["speaker"] == "野崎守"
    assert top["text"] == "誰かの目印かもしれません"
    assert top["low_confidence"] is False
    assert top["start_sec"] == 50.0
    # 直前で最も近い場面説明（後ろの scene は拾わない）
    assert top["scene"].startswith("港の倉庫街")
    # 前後文脈は context 行ずつ・scene 行も混じる
    assert [c["text"] for c in top["before"]] == ["こんなところに", "ガムなんかつけて"]
    assert [c["text"] for c in top["after"]] == ["倉庫の扉が開く。", "行くぞ"]
    assert top["after"][0]["kind"] == "scene"
    assert top["before"][0]["speaker"] == "脇坂昭夫"
    # ヒットした話の客観あらすじを同梱（話番号は文字列キー）
    assert res["stories"]["14"].startswith("タイでの")


def test_search_tolerates_kana_and_symbol_variants(tmp_path):
    """表記揺れ（カタカナ/ひらがな・記号・全角空白）でも同じ行に当たる。"""
    s = _store(tmp_path)
    _ep14(s)
    seqs = {q: [h["line_seq"] for h in s.search_lines(q)["hits"]]
            for q in ("ガムなんかつけて", "がむなんか、つけて…", "ｶﾞﾑ なんかつけて")}
    assert all(v == seqs["ガムなんかつけて"] for v in seqs.values())
    assert 3 in seqs["ガムなんかつけて"]                  # 2行連結（アンカーは前の行）


def test_search_matches_two_line_concat(tmp_path):
    s = _store(tmp_path)
    _ep14(s)
    res = s.search_lines("こんなところにガムなんかつけて", context=2)

    assert res["found"] is True
    top = res["hits"][0]
    assert top["line_seq"] == 3                       # アンカーは line_seq が小さい方
    assert top["text"] == "こんなところにガムなんかつけて"   # 連結して返す
    assert top["score"] == 1.0
    # 連結の2行目は text に入っているので after には出さない
    assert [c["text"] for c in top["after"]] == ["誰かの目印かもしれません", "倉庫の扉が開く。"]
    assert [c["text"] for c in top["before"]] == ["この先に何かあるのか", "さあな"]


def test_scene_lines_are_not_line_hits(tmp_path):
    s = _store(tmp_path)
    _ep14(s)
    res = s.search_lines("倉庫の扉が開く")
    # scene 行は hits(セリフ)には混ざらず、scene_hits 側に出る
    assert all(h["line_seq"] not in (0, 6) for h in res["hits"])
    assert 6 in [h["line_seq"] for h in res["scene_hits"]]


def test_rare_bigram_lifts_paraphrased_query(tmp_path):
    """珍しい字面が合っていれば、用言が言い換わっていても拾い上げる。"""
    s = _store(tmp_path)
    s.write_lines(1, [_line(t) for t in _TSUITERU_LINES])

    res = s.search_lines("ガムついてる")
    assert res["found"] is True
    top = res["hits"][0]
    assert top["line_seq"] == 0                        # 「ガム」を含む唯一の行
    assert top["score"] == 0.6                         # df=1 の下限スコア
    # 「ついてる」を素で共有するだけの行より上に来る
    assert all(h["score"] < 0.6 for h in res["hits"][1:])


def test_rare_floor_ignores_bigrams_made_by_the_join(tmp_path):
    """連結のつなぎ目で偶然できた希少2-gramでは浮かない（別の語と語の境目のため）。"""
    s = _store(tmp_path)
    # 「話は変わるが」＋「ムルーデルの…」を繋ぐと『がむ』ができてしまう並び
    s.write_lines(1, [_line("話は変わるが"), _line("ムルーデルのフローライト")]
                  + [_line(t) for t in _TSUITERU_LINES[1:]])
    s.write_lines(2, [_line(t) for t in _TSUITERU_LINES])

    res = s.search_lines("ガムついてる")
    assert res["hits"][0]["episode"] == 2                 # 行そのものが「ガム」を含む方
    assert all(h["episode"] == 2 or h["score"] < 0.5 for h in res["hits"])


def test_common_bigram_does_not_lift(tmp_path):
    """ありふれた2-gramでは下駄が履かず、1個かすっただけなら found=false。"""
    s = _store(tmp_path)
    s.write_lines(1, [_line(t) for t in _YOU_LINES])

    res = s.search_lines("おはよう")                    # 「よう」1個だけ一致
    assert res["found"] is False
    assert all(h["low_confidence"] is True for h in res["hits"])

    # 丸ごと含まれる短いクエリは1個一致でも通す
    s.write_lines(2, [_line("別班？"), _line("行くぞ")])
    hit = s.search_lines("別班", episode=2)
    assert hit["found"] is True
    assert hit["hits"][0]["text"] == "別班？"


def test_low_confidence_when_nothing_passes_threshold(tmp_path):
    """ありふれた2-gramを複数共有しても、似ていなければ低信頼の参考提示どまり。"""
    s = _store(tmp_path)
    s.write_lines(1, [_line(t) for t in _TSUITERU_LINES[1:]])
    res = s.search_lines("ついてるかまだ確認できていません")

    assert res["found"] is False
    assert 1 <= len(res["hits"]) <= 3                 # 参考として上位3件まで
    assert all(h["low_confidence"] is True for h in res["hits"])
    assert all(0 < h["score"] < 0.25 for h in res["hits"])


def test_adjacent_anchors_are_collapsed(tmp_path):
    """連結ヒットとその2行目の単体ヒットは1件に畳む。"""
    s = _store(tmp_path)
    s.write_lines(1, [_line("何だよ"), _line("こんなところにガムなんかつけて"),
                      _line("イケメンが台なしだな")])
    res = s.search_lines("こんなところにガムなんかつけて")

    assert [h["line_seq"] for h in res["hits"]] == [0]
    assert res["hits"][0]["text"] == "何だよこんなところにガムなんかつけて"


# ── 出来事クエリ（scene / あらすじ） ─────────────────────
def test_chunk_candidates_needs_non_kana():
    """共通チャンクは「かな以外を含む長さ2〜6」だけ（かなだけの断片は対象外）。"""
    c = _chunk_candidates("野崎が逃げた")
    assert "野崎" in c and "野崎が逃げた" in c and "が逃げ" in c
    assert "げた" not in c                              # かなだけ
    assert all(2 <= len(x) <= 6 for x in c)
    assert _chunk_candidates("おはよう") == set()        # 全部ひらがな
    assert _chunk_candidates("ガムついてる") == set()     # カタカナも「かな」なので対象外


def test_sentences_split():
    assert _sentences("A。B\n\nC。") == ["A", "B", "C"]
    assert _sentences("  前後の空白は落とす  ") == ["前後の空白は落とす"]
    assert _sentences(None) == [] and _sentences("。\n。") == []


def test_event_query_hits_scene_and_story(tmp_path):
    """「野崎が逃げた」のような出来事クエリが場面説明とあらすじ文に当たる。"""
    s = _store(tmp_path)
    _nozaki(s)
    res = s.search_lines("野崎が逃げた", context=2)

    assert res["found"] is True
    # セリフ側にはチャンク下駄が無いので、閾値に届く行は無い（詰め物もしない）
    assert res["hits"] == []

    assert len(res["scene_hits"]) == 1
    sc = res["scene_hits"][0]
    assert (sc["episode"], sc["line_seq"], sc["start_sec"]) == (14, 0, 0.0)
    assert sc["text"].startswith("夜の埠頭")
    assert sc["score"] == 0.5                            # 「野崎」チャンクの下駄
    # window は直後に続くセリフ行を context 行（間の scene 行は飛ばす）
    assert [w["text"] for w in sc["window"]] == _NOZAKI_LINES[:2]
    assert all(w["kind"] == "line" for w in sc["window"])

    assert 1 <= len(res["story_hits"]) <= 2               # 各2件まで
    st = res["story_hits"][0]
    assert st["episode"] == 14
    assert "野崎守は消息を絶ち" in st["sentence"]
    assert "。" not in st["sentence"]                     # 「。」で割ってある
    assert st["score"] == 0.5
    assert st["scenes_digest"] == ["夜の埠頭に停まった車から野崎守が降り、暗がりへ走り去る。",
                                   "残されたスマートフォンが路上で光っている。"]
    # あらすじ文が当たった話は stories にも入れる
    assert res["stories"]["14"].startswith("タイでの")


def test_lines_get_no_chunk_floor(tmp_path):
    """チャンク下駄はセリフ行には効かない（ありふれた漢字語で溢れないように）。"""
    s = _store(tmp_path)
    s.write_lines(14, [_line(t) for t in _NOZAKI_LINES])   # scene も あらすじ も無し
    res = s.search_lines("野崎が逃げた")

    assert res["found"] is False
    assert res["scene_hits"] == [] and res["story_hits"] == []
    assert all(h["low_confidence"] is True for h in res["hits"])


def test_kana_only_query_does_not_hit_scene_or_story(tmp_path):
    """かなだけのクエリは scene/あらすじで浮かない（チャンク下駄の対象外）。"""
    s = _store(tmp_path)
    _nozaki(s)
    res = s.search_lines("おはよう")

    assert res["found"] is False
    assert res["scene_hits"] == []
    assert res["story_hits"] == []


def test_no_hits_at_all(tmp_path):
    s = _store(tmp_path)
    _ep14(s)
    res = s.search_lines("ラーメン食べたい")
    assert res["found"] is False
    assert res["hits"] == []
    assert res["stories"] == {}


def test_episode_filter_and_limit(tmp_path):
    s = _store(tmp_path)
    _ep14(s, ep=14)
    _ep14(s, ep=15)                                   # 同じセリフを別の話にも入れる

    both = s.search_lines("こんなところにガムなんかつけて", limit=5)
    assert {h["episode"] for h in both["hits"]} == {14, 15}
    assert set(both["stories"]) == {"14", "15"}

    only15 = s.search_lines("こんなところにガムなんかつけて", episode=15)
    assert {h["episode"] for h in only15["hits"]} == {15}
    assert set(only15["stories"]) == {"15"}

    assert len(s.search_lines("ガム", limit=1)["hits"]) == 1


def test_same_anchor_keeps_best_score(tmp_path):
    """単体ヒットと2行連結ヒットが同じ行を指すとき、高い方だけ残る。"""
    s = _store(tmp_path)
    _ep14(s)
    res = s.search_lines("こんなところにガムなんかつけて")
    anchors = [(h["episode"], h["line_seq"]) for h in res["hits"]]
    assert len(anchors) == len(set(anchors))


def test_empty_query_returns_nothing(tmp_path):
    s = _store(tmp_path)
    _ep14(s)
    assert s.search_lines("　、。")["hits"] == []      # 正規化すると空になるクエリ
    assert s.search_lines("")["found"] is False


# ── 意味で引く検索（偽の埋め込み） ─────────────────────────────
import numpy as np                                               # noqa: E402

from oco import embed                                            # noqa: E402
from oco.config import Config, LLMConfig                         # noqa: E402
from oco.pipeline import build_store                             # noqa: E402
from oco.service import OcoService                               # noqa: E402

# 偽の埋め込み: 「意味」ごとに 1 次元。手がかりの字を含めばその次元が立つ（字面が違っても同じ意味）。
_FAKE_MEANINGS = [("隠れ", "潜"), ("泥",), ("飯", "腹"), ("宇宙",)]


def _fake_post(cfg, inputs, timeout):
    v = np.array([[0.1] + [1.0 if any(w in t for w in ws) else 0.0 for ws in _FAKE_MEANINGS]
                  for t in inputs], dtype=np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def _embed_cfg(tmp_path, base_url="http://fake-embed"):
    return Config(store_dir=tmp_path, text=LLMConfig(provider="openai_compat", model="stub"),
                  db_path=tmp_path / "oco.db", embed_base_url=base_url)


def _trench(store, ep=1):
    """0〜29秒の窓（塹壕で身を潜める）と 30〜59秒の窓（腹が減った）。"""
    rows = [(0, _scene("塹壕の中。兵士が泥の中を這う。")),
            (2, _line("伏せろ", "隊長")),
            (5, _line("敵に見つかるな、身を潜めろ", "隊長")),
            (8, _scene("だらけて倒れた見張り。")),               # 2-gram だけなら問いに当たる
            (40, _line("朝飯はまだか", "兵士A")),
            (45, _line("腹が減った", "兵士B"))]
    lines = []
    for t, ln in rows:
        ln.start, ln.end = float(t), float(t) + 1
        lines.append(ln)
    store.write_lines(ep, lines)
    store.write_story(ep, "部隊は泥にまみれて塹壕を進む。夜明けに朝食をとる。")


def _semantic_store(tmp_path, monkeypatch):
    monkeypatch.setattr(embed, "_post", _fake_post)
    cfg = _embed_cfg(tmp_path)
    store = _store(tmp_path)
    _trench(store)
    embed.embed_episode(cfg, store, 1)
    return cfg, store


def _semantic_search(cfg, store, q):
    return store.search_lines(q, query_vec=embed.embed_query(cfg, q), model=cfg.embed_model)


def test_vector_docs_for_episode(tmp_path):
    store = _store(tmp_path)
    _trench(store)
    by_kind = {}
    for d in store.vector_docs_for_episode(1):
        by_kind.setdefault(d["doc_kind"], []).append((d["doc_seq"], d["doc_text"]))
    # 窓は start_sec//30。scene 行の文も窓に混ぜる
    assert by_kind["win30"] == [
        (0, "塹壕の中。兵士が泥の中を這う。 伏せろ 敵に見つかるな、身を潜めろ だらけて倒れた見張り。"),
        (1, "朝飯はまだか 腹が減った")]
    assert by_kind["scene"] == [(0, "塹壕の中。兵士が泥の中を這う。"), (3, "だらけて倒れた見張り。")]
    assert by_kind["story"] == [(0, "部隊は泥にまみれて塹壕を進む"), (1, "夜明けに朝食をとる")]


def test_semantic_window_hit_shape(tmp_path, monkeypatch):
    cfg, store = _semantic_store(tmp_path, monkeypatch)
    lexical = store.search_lines("敵に見つかるな、身を潜めろ")
    res = _semantic_search(cfg, store, "泥の中で兵士が隠れている")
    assert res["search"] == "semantic" and res["found"] is True
    h = res["hits"][0]
    assert set(h) == set(lexical["hits"][0])                    # 項目は今までと同じ
    # 窓の最初のセリフが anchor、窓の残りが after
    assert (h["episode"], h["line_seq"], h["text"]) == (1, 1, "伏せろ")
    assert h["scene"] == "塹壕の中。兵士が泥の中を這う。"
    assert [x["text"] for x in h["after"]] == ["敵に見つかるな、身を潜めろ", "だらけて倒れた見張り。"]
    assert h["low_confidence"] is False and h["score"] > 0.9
    assert "1" in res["stories"]


def test_semantic_replaces_scene_and_story(tmp_path, monkeypatch):
    cfg, store = _semantic_store(tmp_path, monkeypatch)
    q = "泥だらけで倒れた場面"
    lexical = store.search_lines(q)
    assert "だらけて倒れた見張り。" in [h["text"] for h in lexical["scene_hits"]]   # 2-gram の外れ
    res = _semantic_search(cfg, store, q)
    assert res["search"] == "semantic"
    assert [h["text"] for h in res["scene_hits"]] == ["塹壕の中。兵士が泥の中を這う。"]
    assert res["scene_hits"][0]["window"][0]["text"] == "伏せろ"
    assert set(res["scene_hits"][0]) == set(lexical["scene_hits"][0])
    assert [h["sentence"] for h in res["story_hits"]] == ["部隊は泥にまみれて塹壕を進む"]
    assert set(res["story_hits"][0]) == {"episode", "sentence", "score", "scenes_digest"}


def test_semantic_keeps_verbatim_first(tmp_path, monkeypatch):
    cfg, store = _semantic_store(tmp_path, monkeypatch)
    res = _semantic_search(cfg, store, "朝飯はまだか")          # 逐語の引用（2-gram 1.0）
    assert res["search"] == "semantic"
    assert [(h["line_seq"], h["text"], h["score"]) for h in res["hits"]] == [(4, "朝飯はまだか", 1.0)]


def test_semantic_nothing_close_is_low_confidence(tmp_path, monkeypatch):
    cfg, store = _semantic_store(tmp_path, monkeypatch)
    res = _semantic_search(cfg, store, "宇宙船が爆発する")      # どの意味にも当たらない
    assert res["search"] == "semantic" and res["found"] is False
    assert res["scene_hits"] == [] and res["story_hits"] == []
    assert res["hits"] and all(h["low_confidence"] for h in res["hits"])


def test_lexical_when_no_vectors_written(tmp_path, monkeypatch):
    monkeypatch.setattr(embed, "_post", _fake_post)
    cfg = _embed_cfg(tmp_path)
    store = _store(tmp_path)
    _trench(store)                                              # 埋め戻し前
    assert _semantic_search(cfg, store, "兵士")["search"] == "lexical"


def _service_with_vectors(tmp_path, monkeypatch, base_url):
    """[embed] の URL だけ変えた service（ベクトルは偽の埋め込みで先に書いておく）。"""
    monkeypatch.setattr(embed, "_post", _fake_post)
    svc = OcoService(_embed_cfg(tmp_path, base_url))
    s = svc._get_or_create_series("塹壕もの", None, [])
    store = build_store(svc.cfg, s["series_id"], s["title"])
    _trench(store)
    embed.embed_episode(_embed_cfg(tmp_path), store, 1)
    return svc, store


_LEXICAL_KEYS = ("found", "hits", "scene_hits", "story_hits", "stories")


def test_service_semantic_when_embed_set(tmp_path, monkeypatch):
    svc, _ = _service_with_vectors(tmp_path, monkeypatch, "http://fake-embed")
    res = svc.search_lines(title="塹壕もの", q="泥の中で兵士が隠れている")
    assert res["search"] == "semantic" and res["hits"][0]["text"] == "伏せろ"


def test_service_lexical_when_embed_unset(tmp_path, monkeypatch):
    svc, store = _service_with_vectors(tmp_path, monkeypatch, None)
    res = svc.search_lines(title="塹壕もの", q="泥だらけで倒れた場面")
    old = store.search_lines("泥だらけで倒れた場面")
    assert res["search"] == "lexical"
    assert {k: res[k] for k in _LEXICAL_KEYS} == {k: old[k] for k in _LEXICAL_KEYS}


def test_service_lexical_when_embed_unreachable(tmp_path, monkeypatch):
    svc, store = _service_with_vectors(tmp_path, monkeypatch, "http://127.0.0.1:9")
    monkeypatch.undo()                                          # 本物の httpx で繋がらないことを確かめる
    res = svc.search_lines(title="塹壕もの", q="泥だらけで倒れた場面")
    old = store.search_lines("泥だらけで倒れた場面")
    assert res["search"] == "lexical"
    assert {k: res[k] for k in _LEXICAL_KEYS} == {k: old[k] for k in _LEXICAL_KEYS}


def test_semantic_keeps_short_word_substring(tmp_path, monkeypatch):
    """短い語（2-gram は 0.8 に届かない）でも、セリフにそのまま含まれれば逐語扱いで旧どおり返す。"""
    cfg, store = _semantic_store(tmp_path, monkeypatch)
    assert store.search_lines("見つかる")["hits"][0]["score"] < 0.8
    res = _semantic_search(cfg, store, "見つかる")
    assert res["search"] == "semantic" and res["found"] is True
    assert [(h["text"], h["low_confidence"]) for h in res["hits"]] == [("敵に見つかるな、身を潜めろ", False)]
