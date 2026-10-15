from pathlib import Path

from oco.cm import CMRange
from oco.cm.base import NullCMProvider, build_cm_provider
from oco.cm.chapter_backend import (
    ChapterCMProvider,
    _TAIL_FALLBACK_SEC,
    _sidecar_candidates,
)
from oco.cm.jl_backend import JLCMProvider, _merge
from oco.config import CMConfig


def test_merge_joins_near_and_keeps_far():
    r = [CMRange(0, 10), CMRange(12, 20), CMRange(40, 50)]
    out = _merge(r, gap=4)                 # 10→12(=2) は結合 / 20→40(=20) は別
    assert len(out) == 2
    assert (out[0].start, out[0].end) == (0, 20)
    assert (out[1].start, out[1].end) == (40, 50)


def test_merge_empty():
    assert _merge([], 4) == []


def test_jl_returns_empty_without_video():
    jl = JLCMProvider(CMConfig(backend="jl"))
    assert jl.ranges(None) == []                 # 動画なし
    assert jl.ranges("/x/ep1.ass") == []         # 字幕(.ass)は動画でない


def test_build_cm_provider_dispatch():
    assert isinstance(build_cm_provider(CMConfig(backend="none")), NullCMProvider)
    assert isinstance(build_cm_provider(CMConfig(backend="jl")), JLCMProvider)
    assert isinstance(build_cm_provider(CMConfig(backend="chapter")), ChapterCMProvider)


# ── chapter backend（Amatsukaze sidecar 読取） ─────────────────────────


def _chapter_provider() -> ChapterCMProvider:
    return ChapterCMProvider(CMConfig(backend="chapter"))


_OGM_TWO_CMS = """CHAPTER01=00:00:00.000
CHAPTER01NAME=Aパート
CHAPTER02=00:10:00.000
CHAPTER02NAME=CM
CHAPTER03=00:11:30.000
CHAPTER03NAME=Bパート
CHAPTER04=00:21:00.000
CHAPTER04NAME=CM
CHAPTER05=00:22:30.000
CHAPTER05NAME=Cパート
"""


def test_chapter_ogm_two_cms(tmp_path):
    media = tmp_path / "ep1.hevc.ts"
    (tmp_path / "ep1.hevc.ts.chapter").write_text(_OGM_TWO_CMS, encoding="utf-8")
    out = _chapter_provider().ranges(str(media))
    # 区間 = [CMチャプター開始, 次チャプター開始)
    assert [(r.start, r.end) for r in out] == [(600.0, 690.0), (1260.0, 1350.0)]


def test_chapter_cm_name_variants_and_tail(tmp_path):
    # "cm 3" や 全角 "ＣＭ" も CM 扱い / 最終チャプターが CM → 末尾まで
    text = (
        "CHAPTER01=00:00:00.000\n"
        "CHAPTER01NAME=本編\n"
        "CHAPTER02=00:05:00.000\n"
        "CHAPTER02NAME=cm 3\n"
        "CHAPTER03=00:06:00.000\n"
        "CHAPTER03NAME=本編\n"
        "CHAPTER04=00:23:00.000\n"
        "CHAPTER04NAME=ＣＭ\n"
    )
    media = tmp_path / "ep1.hevc.ts"
    (tmp_path / "ep1.hevc.ts.chapter").write_text(text, encoding="utf-8")
    out = _chapter_provider().ranges(str(media))
    assert len(out) == 2
    assert (out[0].start, out[0].end) == (300.0, 360.0)
    # 末尾CM: 実メディア無し→ffprobe不可→有限フォールバック（inf でない・末尾まで扱い）
    assert out[1].start == 1380.0
    assert out[1].end == 1380.0 + _TAIL_FALLBACK_SEC


def test_chapter_consecutive_cms_merged(tmp_path):
    # 連続する CM チャプターは 1 区間に畳まれる
    text = (
        "CHAPTER01=00:00:00.000\nCHAPTER01NAME=本編\n"
        "CHAPTER02=00:10:00.000\nCHAPTER02NAME=CM 1\n"
        "CHAPTER03=00:10:30.000\nCHAPTER03NAME=CM 2\n"
        "CHAPTER04=00:11:30.000\nCHAPTER04NAME=本編\n"
    )
    media = tmp_path / "ep1.hevc.ts"
    (tmp_path / "ep1.hevc.ts.chapter").write_text(text, encoding="utf-8")
    out = _chapter_provider().ranges(str(media))
    assert [(r.start, r.end) for r in out] == [(600.0, 690.0)]


def test_chapter_bom_and_crlf(tmp_path):
    # UTF-8 BOM 付き + CRLF でも読める
    raw = b"\xef\xbb\xbf" + (
        "CHAPTER01=00:00:00.000\r\nCHAPTER01NAME=CM\r\n"
        "CHAPTER02=00:01:00.000\r\nCHAPTER02NAME=本編\r\n"
    ).encode("utf-8")
    media = tmp_path / "ep1.hevc.ts"
    (tmp_path / "ep1.hevc.ts.chapter").write_bytes(raw)
    out = _chapter_provider().ranges(str(media))
    assert [(r.start, r.end) for r in out] == [(0.0, 60.0)]


def test_chapter_broken_or_empty_returns_empty(tmp_path):
    media = tmp_path / "ep1.hevc.ts"
    sidecar = tmp_path / "ep1.hevc.ts.chapter"
    sidecar.write_text("これはチャプターではないただのテキスト\n", encoding="utf-8")
    assert _chapter_provider().ranges(str(media)) == []   # 形式違い → []
    sidecar.write_text("", encoding="utf-8")
    assert _chapter_provider().ranges(str(media)) == []   # 空ファイル → []
    sidecar.write_bytes(b"\x00\x01\x02\xff\xfe")
    assert _chapter_provider().ranges(str(media)) == []   # バイナリ誤爆 → []


def test_chapter_no_sidecar_returns_empty(tmp_path):
    assert _chapter_provider().ranges(str(tmp_path / "ep1.hevc.ts")) == []
    assert _chapter_provider().ranges(None) == []


def test_chapter_sidecar_candidate_order():
    # 複合拡張子 X.hevc.ts の候補が仕様どおりの優先順で並ぶ
    cands = [p.name for p in _sidecar_candidates(Path("/rec/ep1.hevc.ts"))]
    assert cands == [
        "ep1.hevc.ts.chapter",
        "ep1.hevc.chapter",
        "ep1.hevc.chapter.txt",
        "ep1.hevc.chapters.txt",
        "ep1.hevc.ts.chapters.txt",
        "ep1.chapter",
        "ep1.hevc.txt",
    ]


def test_chapter_sidecar_search_prefers_first_candidate(tmp_path):
    # 複数 sidecar があれば優先順の先頭が勝つ
    media = tmp_path / "ep1.hevc.ts"
    (tmp_path / "ep1.chapter").write_text(
        "CHAPTER01=00:05:00.000\nCHAPTER01NAME=CM\n"
        "CHAPTER02=00:06:00.000\nCHAPTER02NAME=本編\n",
        encoding="utf-8",
    )
    (tmp_path / "ep1.hevc.ts.chapter").write_text(
        "CHAPTER01=00:01:00.000\nCHAPTER01NAME=CM\n"
        "CHAPTER02=00:02:00.000\nCHAPTER02NAME=本編\n",
        encoding="utf-8",
    )
    out = _chapter_provider().ranges(str(media))
    assert [(r.start, r.end) for r in out] == [(60.0, 120.0)]  # ep1.hevc.ts.chapter 側
