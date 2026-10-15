"""選抜フレームを VLM に説明させ、scene 行（line_kind='scene'）として返す。

接地: 映像に映っているものだけ。推測しすぎない（人物名は分かる範囲）。
firehose 禁止＝『字幕の空白』に絞って覗く。
"""

from __future__ import annotations

import logging

from ..subtitles.ass_parser import Line
from .frames import extract_frame, select_gap_frames

logger = logging.getLogger("oco.scene")

SCENE_PROMPT = (
    "このアニメ/ドラマの1フレームに何が映っているかを、日本語で1〜2文で簡潔に説明して。"
    "人物（誰か明らかなら名前、不明なら見た目）・場所・動作・雰囲気を、映っているものだけ。"
    "セリフや字幕は読まなくてよい。映っていないことは推測しない。"
)


class SceneEnricher:
    def __init__(self, vision_llm, cfg):
        self._vision = vision_llm
        self._cfg = cfg

    def scenes(self, video_path: str, lines: list[Line], cm_ranges=None) -> list[Line]:
        times, cov = select_gap_frames(
            lines,
            min_gap=self._cfg.scene_min_gap,
            max_gap=self._cfg.scene_max_gap,
            max_frames=self._cfg.scene_max_frames,
            cm_ranges=cm_ranges,
        )
        # 黙って切り捨てない＝カバレッジを必ずログ
        logger.info(
            "scene frames: 候補=%d 採用=%d スキップ=%d（予算 max_frames=%d）",
            cov["qualified"], cov["covered"], cov["dropped"], self._cfg.scene_max_frames,
        )
        if cov["dropped"]:
            logger.info(
                "scene: 予算超過で %d 個の無言区間は映像なし（max_frames を上げると拾える）",
                cov["dropped"],
            )
        out: list[Line] = []
        failures = 0
        for t in times:
            jpg = extract_frame(video_path, t, width=self._cfg.scene_frame_width)
            if not jpg:
                continue
            # vision LLM は非致命扱い＝落ちても scene を諦めて text だけで完走させる
            # （VLM エンドポイント死亡で episode 全体を落とさない。OCO の "多少欠けてもOK" 哲学）
            try:
                desc = self._vision.describe(image_jpeg=jpg, prompt=SCENE_PROMPT)
            except Exception as e:  # noqa: BLE001 接続/HTTP/デコード等すべて非致命
                failures += 1
                logger.warning("scene: フレーム %.1fs の vision 失敗（scene 省略）: %s", t, e)
                # 一度も成功しないまま連続失敗＝エンドポイント死亡とみなし、
                # 残りフレームを叩かず即中止（16回の接続待ちで時間を溶かさない）
                if failures >= 2 and not out:
                    logger.warning("scene: vision 連続失敗＝エンドポイント不通とみなし scene 抽出を中止")
                    break
                continue
            if desc:
                out.append(Line(start=t, end=t, text=desc, speaker=None, kind="scene"))
        return out
