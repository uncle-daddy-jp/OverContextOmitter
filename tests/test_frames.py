from oco.subtitles.ass_parser import Line
from oco.video.frames import select_gap_frames


def _lines_at(starts):
    # 各 start に短い字幕。kind='line'
    return [Line(start=s, end=s + 0.5, text="x", speaker=None) for s in starts]


def test_gap_selection_range_and_midpoint():
    # 0→10(10sギャップ・採用), 10.5→12(1.5s・短すぎ除外), 12→60(48s・長すぎCM疑い除外)
    lines = _lines_at([0, 10, 10.5, 12, 60])
    times, cov = select_gap_frames(lines, min_gap=4, max_gap=30, max_frames=16)
    assert times == [5.0]                 # 0→10 の中点のみ
    assert cov == {"qualified": 1, "covered": 1, "dropped": 0}


def test_budget_distributes_and_reports_dropped():
    # 等間隔に 20 個の 5秒ギャップ（全て候補）。予算 5 → 5個に均等配分・15ドロップ
    starts = [i * 10.0 for i in range(21)]   # 0,10,...,200 → 20 gaps of 10s
    lines = _lines_at(starts)
    times, cov = select_gap_frames(lines, min_gap=4, max_gap=30, max_frames=5)
    assert cov["qualified"] == 20
    assert cov["covered"] == 5
    assert cov["dropped"] == 15
    assert times == sorted(times)          # 時刻順
    # 均等配分: 前半後半どちらにも散る（先頭<中盤<末尾の幅をカバー）
    assert times[0] < 50 and times[-1] > 150


def test_no_candidates():
    lines = _lines_at([0, 1, 2, 3])        # 全部1sギャップ→候補ゼロ
    times, cov = select_gap_frames(lines, min_gap=4, max_gap=30, max_frames=16)
    assert times == []
    assert cov == {"qualified": 0, "covered": 0, "dropped": 0}
