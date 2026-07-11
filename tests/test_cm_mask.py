from oco.cm import CMRange, mask_lines
from oco.cm.base import NullCMProvider
from oco.subtitles.ass_parser import Line


def test_mask_drops_lines_inside_cm_ranges():
    lines = [
        Line(10.0, 12.0, "本編A", None),
        Line(100.0, 102.0, "CM中のセリフ", None),
        Line(200.0, 202.0, "本編B", None),
    ]
    ranges = [CMRange(90.0, 150.0)]
    out = mask_lines(lines, ranges)
    texts = [l.text for l in out]
    assert texts == ["本編A", "本編B"]


def test_no_ranges_is_passthrough():
    lines = [Line(1.0, 2.0, "x", None)]
    assert mask_lines(lines, []) == lines
    assert NullCMProvider().ranges("anything.ts") == []
