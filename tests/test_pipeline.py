"""process_episode の分岐のユニットテスト（LLM・台本ソースは決定論スタブ）。

見るのは3点:
  A 単発判定 → 単発回は次回予想の LLM を呼ばず outlook に**空文字**を保存する
  B story検証 → 大幅な誤りの指摘があるときだけ1回だけ書き直し、下流はその story を使う
  C ASR警告 → whisper 由来のときだけ各パスの volatile に注入（stable_prefix は不変）
"""

from oco import pipeline, prompts
from oco.config import CMConfig, Config, LLMConfig
from oco.subtitles.ass_parser import Line


class FakeLLM:
    """パスごとに決め打ちの本文を返す決定論スタブ。呼び出しを kind 付きで記録する。"""

    def __init__(self, *, serial="連続\n次回への引きがある", check="問題なし"):
        self.serial = serial
        self.check = check
        self.calls = []                       # [(kind, system, stable_prefix, volatile)]

    @staticmethod
    def _kind(system: str) -> str:
        if system == prompts.CAST_SYSTEM:
            return "cast"
        if system == prompts.OBJECTIVE_SYSTEM:
            return "story"
        if system == prompts.STORY_CHECK_SYSTEM:
            return "story_check"
        if system == prompts.SERIAL_CHECK_SYSTEM:
            return "serial_check"
        if system == prompts.UNDERSTANDING_SYSTEM:
            return "understanding"
        if "今回の話を見た率直な" in system:
            return "impression"
        if "次回への予想・期待" in system:
            return "outlook"
        return "other"

    def complete(self, *, system, stable_prefix, volatile, max_tokens=None):
        kind = self._kind(system)
        self.calls.append((kind, system, stable_prefix, volatile))
        if kind == "serial_check":
            return self.serial
        if kind == "story_check":
            return self.check
        if kind == "story":
            # 書き直し（訂正指示つき）は別本文＝下流にどちらが流れたか見分けられる
            return "訂正後あらすじ" if "訂正して書き直せ" in volatile else "初回あらすじ"
        return f"[{kind}]"

    def kinds(self) -> list[str]:
        return [c[0] for c in self.calls]

    def volatiles(self, kind: str) -> list[str]:
        return [c[3] for c in self.calls if c[0] == kind]

    def stable_prefix(self, kind: str) -> str:
        return next(c[2] for c in self.calls if c[0] == kind)


class FakeSource:
    """台本ソースのスタブ。used_backend で ASR警告の要否を切り替える。"""

    def __init__(self, backend="aribcaption"):
        self.used_backend = backend

    def lines(self, media_path):
        return [Line(start=1.0, end=3.0, text="こんにちは", speaker=None),
                Line(start=4.0, end=6.0, text="またね", speaker=None)]


def _cfg(tmp_path) -> Config:
    return Config(
        store_dir=tmp_path,
        text=LLMConfig(provider="openai_compat", model="stub"),
        cm=CMConfig(backend="none"),
        db_path=tmp_path / "oco.db",
    )


def _run(tmp_path, monkeypatch, *, ep=1, llm=None, backend="aribcaption"):
    """process_episode を1回走らせて (cfg, llm, result) を返す。"""
    cfg = _cfg(tmp_path)
    llm = llm or FakeLLM()
    monkeypatch.setattr(pipeline, "build_text_llm", lambda _c: llm)
    monkeypatch.setattr(pipeline, "build_transcript_source", lambda _c: FakeSource(backend))
    res = pipeline.process_episode(cfg, series_id="demo", ep=ep,
                                   ass_path=str(tmp_path / "x.ass"),
                                   persona_id="default", persona_text="人格",
                                   title="デモ作品")
    return cfg, llm, res


def _store(cfg):
    return pipeline.build_store(cfg, "demo", "デモ作品")


# ── A. 単発判定＋次回予想スキップ ──────────────────────────────
def test_serial_episode_generates_outlook(tmp_path, monkeypatch):
    cfg, llm, res = _run(tmp_path, monkeypatch)
    assert "serial_check" in llm.kinds()               # 整数話数は判定LLMを呼ぶ
    assert "outlook" in llm.kinds()
    assert res.outlook == "[outlook]"
    assert _store(cfg).read_serial_next(1) == 1
    assert "次回への引きがある" in _store(cfg).read_serial_next_reason(1)


def test_single_episode_skips_outlook_and_saves_empty(tmp_path, monkeypatch):
    llm = FakeLLM(serial="単発\n1話完結の刑事ドラマ")
    cfg, llm, res = _run(tmp_path, monkeypatch, llm=llm)
    assert "outlook" not in llm.kinds()                 # 予想の LLM は呼ばない
    assert res.outlook == ""
    store = _store(cfg)
    # ★NULL でなく空文字＝呼び出し側の has_outlook（IS NOT NULL）は true のまま
    assert store.read_outlook(1, "default") == ""
    assert store.read_serial_next(1) == 0
    assert "1話完結" in store.read_serial_next_reason(1)


def test_date_episode_skips_serial_llm(tmp_path, monkeypatch):
    cfg, llm, res = _run(tmp_path, monkeypatch, ep=20260812)
    assert "serial_check" not in llm.kinds()            # 日付話数は判定LLMを呼ばない
    assert "outlook" not in llm.kinds()
    assert res.outlook == ""
    store = _store(cfg)
    assert store.read_serial_next(20260812) == 0
    assert "日付形式" in store.read_serial_next_reason(20260812)


def test_unparsable_serial_answer_falls_back_to_serial(tmp_path, monkeypatch):
    # 1行目が「連続」「単発」のどちらでもない＝安全側で連続扱い（予想は従来どおり出す）
    llm = FakeLLM(serial="判断がつきません")
    cfg, llm, res = _run(tmp_path, monkeypatch, llm=llm)
    assert "outlook" in llm.kinds()
    assert res.outlook == "[outlook]"
    assert _store(cfg).read_serial_next(1) == 1


def test_date_episode_boundaries():
    assert pipeline._is_date_episode(19000101)
    assert pipeline._is_date_episode(21001231)
    assert not pipeline._is_date_episode(19000100)
    assert not pipeline._is_date_episode(21001232)
    assert not pipeline._is_date_episode(29)            # 普通の話数


# ── B. story 検証パス ──────────────────────────────────────
def test_story_check_ok_keeps_first_story(tmp_path, monkeypatch):
    cfg, llm, res = _run(tmp_path, monkeypatch)
    assert llm.kinds().count("story") == 1              # 再生成しない
    assert llm.kinds().count("story_check") == 1
    assert res.story == "初回あらすじ"
    assert _store(cfg).read_story_check(1) == "問題なし"


def test_story_check_regenerates_once_and_feeds_downstream(tmp_path, monkeypatch):
    issue = "- 人物の取り違え: 鍵を渡したのはフェルンでなくフリーレン"
    llm = FakeLLM(check=f"大幅な誤りあり\n{issue}")
    cfg, llm, res = _run(tmp_path, monkeypatch, llm=llm)

    assert llm.kinds().count("story") == 2              # 1回だけ書き直す
    assert llm.kinds().count("story_check") == 1        # 再検証しない＝ループ禁止
    redo = llm.volatiles("story")[1]
    assert "訂正して書き直せ" in redo and issue in redo
    assert "大幅な誤りあり" not in redo                  # 判定語は訂正指示に混ぜない
    assert res.story == "訂正後あらすじ"
    assert _store(cfg).read_story(1) == "訂正後あらすじ"
    assert _store(cfg).read_story_check(1).startswith("大幅な誤りあり")   # 生の出力を監査用に保存
    # 下流（②③③）にも書き直し後の story が渡る
    assert "訂正後あらすじ" in llm.volatiles("understanding")[0]
    assert "訂正後あらすじ" in llm.volatiles("impression")[0]
    assert "訂正後あらすじ" in llm.volatiles("outlook")[0]


def test_story_check_empty_output_is_treated_as_ok(tmp_path, monkeypatch):
    cfg, llm, res = _run(tmp_path, monkeypatch, llm=FakeLLM(check="   "))
    assert llm.kinds().count("story") == 1
    assert res.story == "初回あらすじ"


def test_story_check_unformatted_output_does_not_regenerate(tmp_path, monkeypatch):
    """形式を守らない出力（あらすじを逐条 echo して注記する型）は書き直さない＝安全側。

    gemma4-26b の実出力（日曜美術館 20260802）の再現形。緩い判定だとこれを毎話「誤りあり」と
    誤判定し、1コール無駄に書き直していた。
    """
    echo = (
        "東京で開催中の、空海の生誕1250年を記念した秘仏展覧会。\n"
        "        *   *(検証)*: 台本通り。\n\n"
        "    2.  まず訪れたのは観菩提寺。33年に一度しか開帳されない本尊。\n"
        "        *   *(検証)*: 台本通り。"
    )
    cfg, llm, res = _run(tmp_path, monkeypatch, llm=FakeLLM(check=echo))
    assert llm.kinds().count("story") == 1              # 書き直しコールを打たない
    assert res.story == "初回あらすじ"
    assert _store(cfg).read_story_check(1) == echo      # 生の出力は監査用に残る


def test_story_check_verdict_without_bullets_does_not_regenerate(tmp_path, monkeypatch):
    # 「大幅な誤りあり」だけで指摘が無い＝直す先が無いので書き直さない
    cfg, llm, res = _run(tmp_path, monkeypatch, llm=FakeLLM(check="大幅な誤りあり"))
    assert llm.kinds().count("story") == 1
    assert res.story == "初回あらすじ"


def test_story_check_parsers():
    assert pipeline._has_story_issue("大幅な誤りあり\n- 取り違え")
    assert not pipeline._has_story_issue("問題なし")
    assert not pipeline._has_story_issue("あらすじは台本通り。大幅な誤りあり？")   # 形式不明
    assert not pipeline._has_story_issue("")
    assert pipeline._has_story_issue("\n  大幅な誤りあり  \n- 取り違え")          # 前後の空白/空行は許容
    assert pipeline._story_check_corrections("大幅な誤りあり\n- a\n- b") == "- a\n- b"
    assert pipeline._story_check_corrections("大幅な誤りあり") == ""


# ── C. ASR台本への警告注入 ────────────────────────────────
_PERSONA_KINDS = ("understanding", "impression", "outlook")


def test_asr_warning_injected_into_every_volatile(tmp_path, monkeypatch):
    cfg, llm, res = _run(tmp_path, monkeypatch, backend="whisper")
    for kind in ("cast", "story", "story_check", "serial_check", *_PERSONA_KINDS):
        for vol in llm.volatiles(kind):
            assert prompts._ASR_WARNING in vol, kind


def test_asr_warning_never_touches_shared_stable_prefix(tmp_path, monkeypatch):
    cfg, llm, res = _run(tmp_path, monkeypatch, backend="whisper")
    prefixes = {llm.stable_prefix(k) for k in _PERSONA_KINDS}
    assert len(prefixes) == 1                           # 3人格パスは完全一致で共有（キャッシュ）
    assert "音声認識" not in prefixes.pop()


def test_caption_source_prompts_are_unchanged(tmp_path, monkeypatch):
    cfg, llm, res = _run(tmp_path, monkeypatch, backend="aribcaption")
    assert all("音声認識" not in vol for _, _, _, vol in llm.calls)
    assert all("音声認識" not in sys_ for _, sys_, _, _ in llm.calls)


# ── D. 意味で引く検索のベクトル（[embed] 設定時・偽の埋め込み） ─────────────
def _run_with_embed(tmp_path, monkeypatch, fake_post):
    from oco import embed

    monkeypatch.setattr(embed, "_post", fake_post)
    monkeypatch.setattr(pipeline, "build_text_llm", lambda _c: FakeLLM())
    monkeypatch.setattr(pipeline, "build_transcript_source", lambda _c: FakeSource())
    cfg = _cfg(tmp_path)
    cfg.embed_base_url = "http://fake-embed"
    res = pipeline.process_episode(cfg, series_id="demo", ep=1, ass_path=str(tmp_path / "x.ass"),
                                   persona_id="default", persona_text="人格", title="デモ作品")
    return cfg, res


def test_process_writes_vectors_when_embed_set(tmp_path, monkeypatch):
    import numpy as np

    from oco.db import transaction

    cfg, _ = _run_with_embed(tmp_path, monkeypatch,
                             lambda cfg, inputs, timeout: np.full((len(inputs), 4), 0.5, np.float32))
    with transaction(cfg.db_path) as conn:
        rows = conn.execute("SELECT doc_kind, doc_text, model FROM oco_vectors "
                            "WHERE episode_number=1 ORDER BY doc_kind").fetchall()
    assert [tuple(r) for r in rows] == [("story", "初回あらすじ", "eg2"),
                                        ("win30", "こんにちは またね", "eg2")]


def test_process_survives_embed_failure(tmp_path, monkeypatch):
    def down(cfg, inputs, timeout):
        raise ConnectionError("embed server down")

    _, res = _run_with_embed(tmp_path, monkeypatch, down)
    assert res.story == "初回あらすじ"                           # 埋め込みが落ちても話の処理は成功
