"""§4.5 階層ローリング圧縮のユニットテスト。

LLM は決定論スタブ（volatile を見て封印文を返す）。生レイヤーは不変で、封印後の
作業コンテキストが「圧縮ブロック＋進行中の生」に切り替わることを確認する。
"""

from oco import compression, prompts
from oco.store_sqlite import SqliteStore


class StubLLM:
    """volatile の中身で客観/主観の封印文を出し分ける決定論スタブ。"""

    def __init__(self):
        self.calls = []

    def complete(self, *, system, stable_prefix, volatile, max_tokens=None):
        self.calls.append(volatile)
        if "客観まとめ" in volatile or "後の話を読むときの前提" in volatile:
            # build_block_subjective は「客観まとめ」を含む / objective は「前提として使える」
            pass
        if "この区間のあなたの感想" in volatile:
            return "[BLOCK-DIGEST] 未解決の予想: 鍵の正体はまだ不明。"
        return "[BLOCK-STORY] 第1-5話の圧縮まとめ。開いた糸: 鍵の出どころ。"


def _store(tmp_path):
    return SqliteStore(str(tmp_path / "oco.db"), "demo", title="デモ作品")


def _seed(store, ep):
    store.write_lines(ep, [])  # lines は空でも可（story があれば processed）
    store.write_story(ep, f"第{ep}話の客観あらすじ（鍵の話）")
    store.write_understanding(ep, "default", f"第{ep}話の理解")
    store.write_impression(ep, "default", f"第{ep}話の感想")
    store.write_outlook(ep, "default", f"第{ep}話の予想")


def test_no_seal_before_block_full(tmp_path):
    store = _store(tmp_path)
    llm = StubLLM()
    for ep in (1, 2, 3, 4):
        _seed(store, ep)
        assert compression.maybe_seal(store, llm, "m", "default", "P", None, ep, 5) is None
    assert store.read_blocks() == []                       # まだ封印されない
    # ep4 を見るときの作業コンテキストは 生1..3（進行中ブロック）
    ctx = compression.objective_prior_context(store, ep=4, block_size=5)
    labels = [lab for lab, _ in ctx]
    assert labels == ["第1話 あらすじ", "第2話 あらすじ", "第3話 あらすじ"]


def test_seal_at_boundary_and_context_switches(tmp_path):
    store = _store(tmp_path)
    llm = StubLLM()
    for ep in range(1, 6):
        _seed(store, ep)
    # ep5 到達で 1-5 が封印される
    idx = compression.maybe_seal(store, llm, "m", "default", "P", None, 5, 5)
    assert idx == 1
    blocks = store.read_blocks()
    assert len(blocks) == 1
    assert blocks[0]["ep_lo"] == 1 and blocks[0]["ep_hi"] == 5
    assert "BLOCK-STORY" in blocks[0]["text"]
    assert "未解決" in store.read_block_view(1, "default")     # 主観ダイジェスト（予想持ち越し）

    # ep6 を見るときの客観コンテキスト = 圧縮1-5 のみ（生1..5 はもう積まない）
    ctx = compression.objective_prior_context(store, ep=6, block_size=5)
    labels = [lab for lab, _ in ctx]
    assert labels == ["第1-5話 まとめ（圧縮）"]

    # 人格コンテキストも圧縮ブロック（客観まとめ＋主観ダイジェスト）に切替
    hist = compression.persona_prior_context(store, "default", ep=6, block_size=5)
    assert len(hist) == 1 and hist[0]["is_block"] is True
    assert "BLOCK-STORY" in hist[0]["story"]
    assert "未解決" in hist[0]["digest"]

    # 生レイヤーは不変（recall は生 story に届く＝伏線の安全網）
    assert store.read_story(2) == "第2話の客観あらすじ（鍵の話）"
    assert any(h["type"] == "story" and h["ep"] == 2 for h in store.recall("鍵"))


def test_second_block_and_mixed_context(tmp_path):
    store = _store(tmp_path)
    llm = StubLLM()
    for ep in range(1, 6):
        _seed(store, ep)
    compression.maybe_seal(store, llm, "m", "default", "P", None, 5, 5)
    for ep in range(6, 11):
        _seed(store, ep)
        # ep7〜ep9 では 圧縮1-5 ＋ 進行中の生6.. が混在
        if ep == 8:
            ctx = compression.objective_prior_context(store, ep=8, block_size=5)
            labels = [lab for lab, _ in ctx]
            assert labels == ["第1-5話 まとめ（圧縮）", "第6話 あらすじ", "第7話 あらすじ"]
        idx = compression.maybe_seal(store, llm, "m", "default", "P", None, ep, 5)
    assert idx == 2                                        # ep10 で 6-10 を封印
    assert len(store.read_blocks()) == 2
    # ep11 の客観コンテキスト = 圧縮1-5 ＋ 圧縮6-10（生話なし）
    ctx = compression.objective_prior_context(store, ep=11, block_size=5)
    assert [lab for lab, _ in ctx] == ["第1-5話 まとめ（圧縮）", "第6-10話 まとめ（圧縮）"]


def test_catch_up_seals_all_missed_blocks(tmp_path):
    """境界話を封印前に通過していても、後の1回で未封印の完成ブロックを全部追いつき封印する。

    実障害の再現形（豊臣兄弟）: 圧縮機能の後付けで ep5/10/15 の封印を取りこぼし、さらに
    16-17 が欠番。covered=0 のまま ep21 に到達したケース。1回の maybe_seal で 1-5/6-10/
    11-15/16-20 が全部封印され、作業コンテキストが圧縮ブロックだけに畳まれることを確認する。
    """
    store = _store(tmp_path)
    llm = StubLLM()
    for ep in list(range(1, 16)) + [18, 19, 20, 21]:      # 16,17 は欠番
        _seed(store, ep)
    assert store.read_blocks() == []                       # まだ何も封印されていない（旧実装で詰む状態）

    idx = compression.maybe_seal(store, llm, "m", "default", "P", None, 21, 5)
    assert idx == 4                                        # 最後に封印したのは 16-20（block 4）
    blocks = store.read_blocks()
    assert [(b["ep_lo"], b["ep_hi"]) for b in blocks] == [(1, 5), (6, 10), (11, 15), (16, 20)]

    # ep21 を見るときの作業コンテキストは圧縮4ブロックのみ（生の過去話は積まない＝窓が膨らまない）
    ctx = compression.objective_prior_context(store, ep=21, block_size=5)
    assert [lab for lab, _ in ctx] == [
        "第1-5話 まとめ（圧縮）", "第6-10話 まとめ（圧縮）",
        "第11-15話 まとめ（圧縮）", "第16-20話 まとめ（圧縮）",
    ]
    # 生レイヤーは不変（欠番でない話は recall で引ける）
    assert store.read_story(20) == "第20話の客観あらすじ（鍵の話）"

    # 再呼び出しは冪等（未完成の 21-25 は封印しない）
    assert compression.maybe_seal(store, llm, "m", "default", "P", None, 21, 5) is None
    assert len(store.read_blocks()) == 4


def test_empty_outlook_is_not_carried_into_context(tmp_path):
    """単発回の空 outlook は履歴に載らない（truthy チェックで落ちる＝空の見出しを積まない）。"""
    store = _store(tmp_path)
    _seed(store, 1)
    store.write_outlook(1, "default", "")               # 第1話は単発＝予想なし
    _seed(store, 2)

    hist = compression.persona_prior_context(store, "default", ep=3, block_size=5)
    assert hist[0]["outlook"] == "" and hist[1]["outlook"] == "第2話の予想"
    prefix = prompts._persona_prefix("人格", hist)
    assert prefix.count("[あなたの予想・期待]") == 1     # 空の第1話ぶんは載らない
    assert "第2話の予想" in prefix

    # ブロック封印の主観ダイジェストにも空の予想は載らない
    llm = StubLLM()
    for ep in range(3, 6):
        _seed(store, ep)
    compression.maybe_seal(store, llm, "m", "default", "P", None, 5, 5)
    subjective = next(v for v in llm.calls if "この区間のあなたの感想" in v)
    assert subjective.count("[予想・期待]") == 4        # 第1話（空）を除く4話ぶん


def test_disabled_block_size_falls_back_to_full(tmp_path):
    store = _store(tmp_path)
    llm = StubLLM()
    for ep in range(1, 8):
        _seed(store, ep)
        assert compression.maybe_seal(store, llm, "m", "default", "P", None, ep, 0) is None
    assert store.read_blocks() == []                       # 無効化なら封印しない
    # 全話ベタ貼り（生1..6 が全部）
    ctx = compression.objective_prior_context(store, ep=7, block_size=0)
    assert [lab for lab, _ in ctx] == [f"第{e}話 あらすじ" for e in range(1, 7)]
