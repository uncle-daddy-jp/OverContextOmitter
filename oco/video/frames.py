"""フレーム抽出と『字幕の空白』からのフレーム時刻選抜。

firehose 禁止＝質問/空白駆動。長すぎるギャップ（CM疑い）は当座スキップする
ヒューリスティック（JL=ロゴ検出で精密化するまでの繋ぎ）。CMレンジが与えられれば
そこも除外する。
"""

from __future__ import annotations

import subprocess

from ..subtitles.ass_parser import Line


def extract_frame(video_path: str, t_sec: float, width: int = 768) -> bytes | None:
    """video の t_sec 地点のフレームを JPEG bytes で返す（高速シーク・縮小）。"""
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-ss", f"{t_sec:.3f}", "-i", str(video_path),
        "-frames:v", "1",
        "-vf", f"scale={width}:-1",
        "-f", "image2", "-c:v", "mjpeg", "-q:v", "3",
        "pipe:1",
    ]
    proc = subprocess.run(cmd, capture_output=True)
    return proc.stdout or None


def select_gap_frames(
    lines: list[Line],
    min_gap: float,
    max_gap: float,
    max_frames: int,
    cm_ranges=None,
) -> tuple[list[float], dict]:
    """字幕の空白（無言区間）からフレーム時刻を選ぶ。返り値 (時刻リスト, 集計)。

    - 候補＝隣接字幕の間隔が min_gap〜max_gap（短すぎる息継ぎ/長すぎるCM疑いを除外）かつ
      CMレンジ外。各ギャップの中点を候補時刻に。
    - 予算 max_frames を超えたら『時間軸を max_frames 等分し、各区間の最長ギャップを1つ』
      ＝全体に均等に散らす（長い順の偏り/前半集中を避ける）。落とした数は集計で返す
      （黙って切り捨てない）。
    """
    starts = sorted(l.start for l in lines if l.kind == "line")
    candidates: list[tuple[float, float]] = []  # (gap_len, midpoint)
    for a, b in zip(starts, starts[1:]):
        g = b - a
        if not (min_gap <= g <= max_gap):
            continue
        mid = a + g / 2
        if cm_ranges and any(r.contains(mid) for r in cm_ranges):
            continue
        candidates.append((g, mid))

    if not candidates:
        return [], {"qualified": 0, "covered": 0, "dropped": 0}

    if len(candidates) <= max_frames:
        chosen = sorted(m for _, m in candidates)
        return chosen, {"qualified": len(candidates), "covered": len(chosen), "dropped": 0}

    # 予算超過 → 時間軸を等分し各バケツの最長ギャップを採る（均等カバレッジ）
    t0, t1 = starts[0], starts[-1]
    span = max(t1 - t0, 1.0)
    buckets: dict[int, tuple[float, float]] = {}
    for g, mid in candidates:
        idx = min(int((mid - t0) / span * max_frames), max_frames - 1)
        if idx not in buckets or g > buckets[idx][0]:
            buckets[idx] = (g, mid)
    chosen = sorted(mid for _, mid in buckets.values())
    return chosen, {
        "qualified": len(candidates),
        "covered": len(chosen),
        "dropped": len(candidates) - len(chosen),
    }
