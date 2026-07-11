"""第N話の処理フロー（テキスト段）。

入力: .ass（または抽出済み lines）+ これまでの全あらすじ＋感想
出力(保存): transcript(整形済み生字幕) / story① / understanding② / impression③ / outlook③ + lines

LLM 4回・順序固定: story→understanding→impression→outlook。
3人格パス(②③前③後)は同一 persona_prefix を共有しキャッシュを効かせる。
予想の答え合わせ専用機構は作らない（outlook を持って次話を見れば自然に出る）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import compression, prompts
from .cm import build_cm_provider, mask_lines
from .config import Config
from .llm import build_text_llm
from .transcript import build_transcript_source


def build_store(cfg: Config, series_id: str, title: str | None = None):
    """Store を生成。SQLite が唯一の正本 store（series/jobs も SQLite に同居）。

    （`store.py` の SeriesStore は v0.1 テキスト段の単体テスト専用＝service 経路では使わない）
    """
    from .store_sqlite import SqliteStore

    return SqliteStore(str(cfg.db_path), series_id, title=title)


@dataclass
class EpisodeResult:
    series_id: str
    ep: int
    persona: str
    n_lines: int
    n_masked: int
    transcript: str
    cast: str
    story: str
    understanding: str
    impression: str
    outlook: str


def process_episode(
    cfg: Config,
    series_id: str,
    ep: int,
    ass_path: str | Path,
    persona_id: str,
    persona_text: str,
    title: str | None = None,
    video_path: str | None = None,
) -> EpisodeResult:
    store = build_store(cfg, series_id, title)
    llm = build_text_llm(cfg.text)
    model = cfg.text.model
    persona_prompt_id = store.get_or_create_persona_prompt(persona_id, persona_text)

    # media が動画なら CM検出/映像scene にも使える（字幕なし=ASRのとき media=動画）
    _VIDEO = {".ts", ".m2ts", ".mp4", ".mkv"}
    video = video_path or (str(ass_path) if Path(ass_path).suffix.lower() in _VIDEO else None)

    # 1. 台本ソース（差し替え式: aribcaption=字幕 / whisper=ASR将来）
    lines = build_transcript_source(cfg).lines(str(ass_path))
    n_raw = len(lines)
    if n_raw == 0:
        # 字幕が無い/抽出失敗。無言で空生成しない（ジョブを error にする）
        raise ValueError(
            f"台本が空（0行）: {ass_path}。"
            "字幕(ARIB)付きメディアを渡すこと（字幕なしは将来 whisper backend）。"
        )

    # 2. CM 検出（JL=ロゴ検出は動画が要る）。レンジはテキスト行マスク＋画像フレーム除外で共用。
    cm = build_cm_provider(cfg.cm)
    cm_ranges = cm.ranges(video or str(ass_path))
    lines = mask_lines(lines, cm_ranges)
    n_masked = n_raw - len(lines)

    # 2.5 映像 scene 抽出（vision 設定 + 動画がある時のみ・字幕の空白を覗く）
    if video and cfg.vision:
        from .llm import build_vision_llm
        from .video.enrich import SceneEnricher

        enricher = SceneEnricher(build_vision_llm(cfg.vision), cfg)
        scenes = enricher.scenes(video, lines, cm_ranges)
        lines = sorted(lines + scenes, key=lambda l: l.start)

    # 3. lines 保存 ＋ 整形済みトランスクリプト（=今回の台本＝保存物・scene混合）
    store.write_lines(ep, lines)
    records = [ln.to_record() for ln in lines]
    transcript = prompts.render_transcript(records)
    store.write_transcript(ep, transcript)

    # 作業コンテキスト（§4.5 階層ローリング圧縮）：
    #   封印済みブロック要約 ＋ 進行中ブロックの生（生レイヤーは不変・recall の対象）。
    B = cfg.block_size
    obj_context = compression.objective_prior_context(store, ep, B)
    history = compression.persona_prior_context(store, persona_id, ep, B)

    # 4. 人物相関（carry-forward・客観・取り違え防止の接地）。以後の全パスに注入する。
    prior_roster = store.read_latest_cast(ep)
    sys_c, pre_c, vol_c = prompts.build_cast(prior_roster, transcript)
    cast = llm.complete(system=sys_c, stable_prefix=pre_c, volatile=vol_c)
    store.write_cast(ep, cast)

    # 4.5 ② 話者同定（クラスタ→キャラ名）。人物相関を候補に確信ある所だけ命名し、
    #     named 台本に差し替える（以後の story/理解/感想が話者付きで読む）。
    if cfg.speaker_naming:
        from .speakers import attribute_speakers

        # クラスタ有(ARIB色/diarization)→クラスタ命名 / 無(ASR)→テキストだけ行単位推定
        if attribute_speakers(llm, lines, cast).get("changed"):
            store.write_lines(ep, lines)
            transcript = prompts.render_transcript([ln.to_record() for ln in lines])
            store.write_transcript(ep, transcript)

    # 5. ① 客観あらすじ（persona非依存・先頭客観・named 台本）
    sys_o, pre_o, vol_o = prompts.build_objective(obj_context, transcript, cast)
    story = llm.complete(system=sys_o, stable_prefix=pre_o, volatile=vol_o)
    store.write_story(ep, story, model=model, n_lines=len(lines), n_masked=n_masked,
                      episode_title=None)

    # 6. ② 人格の物語理解（事実は story① に接地固定）
    sys_u, pre_u, vol_u = prompts.build_understanding(persona_text, history, transcript, story, cast)
    understanding = llm.complete(system=sys_u, stable_prefix=pre_u, volatile=vol_u)
    store.write_understanding(ep, persona_id, understanding,
                              persona_prompt_id=persona_prompt_id, model=model)

    # 7. ③ 感想（理解の上に乗せる）
    sys_i, pre_i, vol_i = prompts.build_impression(persona_text, history, transcript, story,
                                                   understanding, cast)
    impression = llm.complete(system=sys_i, stable_prefix=pre_i, volatile=vol_i)
    store.write_impression(ep, persona_id, impression,
                           persona_prompt_id=persona_prompt_id, model=model)

    # 8. ③ 予想・期待
    sys_x, pre_x, vol_x = prompts.build_outlook(persona_text, history, story, impression, cast)
    outlook = llm.complete(system=sys_x, stable_prefix=pre_x, volatile=vol_x)
    store.write_outlook(ep, persona_id, outlook,
                        persona_prompt_id=persona_prompt_id, model=model)

    # 8.5 ブロック封印（§4.5）: この話でブロックが1つ埋まったら圧縮版を作る
    #     （客観は persona 非依存で1回・主観は persona 別・生レイヤーは不変のまま）
    compression.maybe_seal(store, llm, model, persona_id, persona_text,
                           persona_prompt_id, ep, cfg.block_size)

    return EpisodeResult(
        series_id=series_id,
        ep=ep,
        persona=persona_id,
        n_lines=len(lines),
        n_masked=n_masked,
        transcript=transcript,
        cast=cast,
        story=story,
        understanding=understanding,
        impression=impression,
        outlook=outlook,
    )
