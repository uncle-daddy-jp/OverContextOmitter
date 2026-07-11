from oco.cm import CMRange
from oco.cm.base import NullCMProvider, build_cm_provider
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
