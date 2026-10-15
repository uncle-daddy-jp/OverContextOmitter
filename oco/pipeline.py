"""第N話の処理フロー（テキスト段）。

入力: .ass（または抽出済み lines）+ これまでの全あらすじ＋感想
出力(保存): transcript(整形済み生字幕) / story① / understanding② / impression③ / outlook③ + lines

LLM 順序固定: cast→story→(story検証)→understanding→impression→outlook。
3人格パス(②③前③後)は同一 persona_prefix を共有しキャッシュを効かせる。
予想の答え合わせ専用機構は作らない（outlook を持って次話を見れば自然に出る）。

- story① は生成直後に検証パスへ回し、大幅な誤り（人物取り違え・因果逆転・捏造・主軸の
  欠落）が指摘されたときだけ**1回だけ**書き直す（再検証はしない＝ループ禁止）。
- 単発回（映画・1話完結・情報番組・日付話数）は次回が無いので outlook を生成しない。
  ただし NULL でなく**空文字**を保存する（呼び出し側の has_outlook は
  `outlook_prediction_text IS NOT NULL` 判定＝空文字なら true のまま＝互換が保たれる）。
- ASR(whisper)由来の台本は誤変換を含むので、全プロンプトの volatile 側に警告を注入する。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from . import compression, prompts
from .cm import build_cm_provider, mask_lines
from .config import Config
from .embed import embed_episode
from .llm import build_text_llm
from .transcript import build_transcript_source

log = logging.getLogger(__name__)


def build_store(cfg: Config, series_id: str, title: str | None = None):
    """Store を生成。SQLite が唯一の正本 store（series/jobs も SQLite に同居）。

    （`store.py` の SeriesStore は v0.1 テキスト段の単体テスト専用＝service 経路では使わない）
    """
    from .store_sqlite import SqliteStore

    return SqliteStore(str(cfg.db_path), series_id, title=title)


# 話数が日付(YYYYMMDD)形式とみなす範囲。日付で回を識別する番組＝連番の続きものではない。
_DATE_EP_LO, _DATE_EP_HI = 19000101, 21001231


def _is_date_episode(ep: int) -> bool:
    """話数が日付形式（8桁 YYYYMMDD）か。"""
    return _DATE_EP_LO <= ep <= _DATE_EP_HI


def _first_line(text: str) -> str:
    """最初の非空行（＝判定語が乗る行）を返す。無ければ空文字。"""
    for row in (text or "").splitlines():
        if row.strip():
            return row.strip()
    return ""


def _decide_serial_next(llm, ep: int, series_title: str, script: str,
                        asr: bool) -> tuple[int, str]:
    """この回が次回に続く連続もの(1)か単発(0)かを判定し (serial_next, 理由) を返す。

    日付形式の話数は判定LLMを呼ばず単発。整数話数のときだけ小さな判定コールを1回打つ。
    1行目が「連続」「単発」のどちらでもなければ安全側＝連続（＝従来どおり予想を出す）。
    """
    if _is_date_episode(ep):
        return 0, f"話数 {ep} が日付形式(YYYYMMDD)＝日付で識別する回＝単発（判定LLM不要）"
    sys_s, pre_s, vol_s = prompts.build_serial_check(script, series_title, asr=asr)
    out = llm.complete(system=sys_s, stable_prefix=pre_s, volatile=vol_s)
    rows = [r.strip() for r in (out or "").splitlines() if r.strip()]
    head = rows[0] if rows else ""
    reason = rows[1] if len(rows) > 1 else ""
    if head == "単発":
        return 0, f"単発: {reason}" if reason else "単発"
    if head == "連続":
        return 1, f"連続: {reason}" if reason else "連続"
    return 1, f"判定不能（1行目={head!r}）→安全側で連続扱い"


def _has_story_issue(check_text: str) -> bool:
    """検証パスの1行目が「大幅な誤りあり」か（＝あらすじを書き直すか）。

    「問題なし」・形式を守らない出力・空はすべて False＝書き直さない（安全側）。
    ★緩い判定（「問題なし」始まりでなければ誤りあり）だと、あらすじを項目ごとに echo して
      各項目に「*(検証)*: 台本通り。」と注記する形式で返すモデル（gemma4-26b 実測・
      日曜美術館 20260802）を毎回「誤りあり」と誤判定し、1コール無駄に書き直していた。
      検証係が形式を守れないときに毎話 +1コールを垂れ流さない側に倒す
      （serial判定の「どちらでもなければ安全側」と同じ思想）。
    """
    return _first_line(check_text) == "大幅な誤りあり"


def _story_check_corrections(check_text: str) -> str:
    """検証パス出力の2行目以降（誤りの箇条書き）だけを返す＝判定語は訂正指示に混ぜない。"""
    return "\n".join((check_text or "").strip().splitlines()[1:]).strip()


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

    # 1. 台本ソース（差し替え式: aribcaption=字幕 / whisper=ASR / auto=自動判別）
    source = build_transcript_source(cfg)
    lines = source.lines(str(ass_path))
    # 実際に使われた backend。ASR(whisper)由来は誤変換を含むので各プロンプトへ警告を注入する
    # （字幕由来なら注入しない＝従来と完全同一のプロンプト＝キャッシュも従来どおり）。
    asr = getattr(source, "used_backend", None) == "whisper"
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

    # 単話でも巨大な transcript は cast/story/理解/感想 の LLM を context 溢れさせる。
    # しきい値超のときだけ map-reduce 圧縮した analysis_transcript を後段に使い、
    # 通常番組は transcript のまま＝無改変。生 transcript は上で store 済み＝recall 無傷。
    oversized = compression.transcript_too_large(transcript, cfg)
    analysis_transcript = (
        compression.condense_transcript(llm, model, transcript, cfg, asr=asr)
        if oversized else transcript
    )

    # 作業コンテキスト（§4.5 階層ローリング圧縮）：
    #   封印済みブロック要約 ＋ 進行中ブロックの生（生レイヤーは不変・recall の対象）。
    B = cfg.block_size
    obj_context = compression.objective_prior_context(store, ep, B)
    history = compression.persona_prior_context(store, persona_id, ep, B)

    # 4. 人物相関（carry-forward・客観・取り違え防止の接地）。以後の全パスに注入する。
    prior_roster = store.read_latest_cast(ep)
    sys_c, pre_c, vol_c = prompts.build_cast(prior_roster, analysis_transcript, asr=asr)
    cast = llm.complete(system=sys_c, stable_prefix=pre_c, volatile=vol_c)
    store.write_cast(ep, cast)

    # 4.5 ② 話者同定（クラスタ→キャラ名）。人物相関を候補に確信ある所だけ命名し、
    #     named 台本に差し替える（以後の story/理解/感想が話者付きで読む）。
    if cfg.speaker_naming and not oversized:
        from .speakers import attribute_speakers

        # クラスタ有(ARIB色/diarization)→クラスタ命名 / 無(ASR)→テキストだけ行単位推定
        if attribute_speakers(llm, lines, cast, asr=asr).get("changed"):
            store.write_lines(ep, lines)
            transcript = prompts.render_transcript([ln.to_record() for ln in lines])
            store.write_transcript(ep, transcript)
            analysis_transcript = transcript  # 話者付与後の台本を後段へ＝通常パス従来同一

    # 5. ① 客観あらすじ（persona非依存・先頭客観・named 台本）
    sys_o, pre_o, vol_o = prompts.build_objective(obj_context, analysis_transcript, cast, asr=asr)
    story = llm.complete(system=sys_o, stable_prefix=pre_o, volatile=vol_o)

    # 5.5 あらすじの検証（大幅な誤り＝人物取り違え/因果逆転/捏造/主軸の欠落 だけを拾う。
    #     細部の省略・表現の差は指摘対象外＝要約であることはこのシステムの仕様）。
    #     1行目が「大幅な誤りあり」のときだけ、2行目以降の箇条書きを添えて**1回だけ**
    #     書き直す（再検証しない＝ループ禁止）。判定語だけで指摘が空なら直す先が無いので
    #     書き直さない（無駄なコールを打たない）。
    sys_v, pre_v, vol_v = prompts.build_story_check(analysis_transcript, story, asr=asr)
    story_check = llm.complete(system=sys_v, stable_prefix=pre_v, volatile=vol_v)
    store.write_story_check(ep, story_check)          # 生の出力を監査用に保存
    corrections = _story_check_corrections(story_check) if _has_story_issue(story_check) else ""
    if corrections:
        sys_o2, pre_o2, vol_o2 = prompts.build_objective(
            obj_context, analysis_transcript, cast, asr=asr, corrections=corrections
        )
        story = llm.complete(system=sys_o2, stable_prefix=pre_o2, volatile=vol_o2)

    # 書き直し後の story を保存し、下流（②③③）にもこれを使う
    store.write_story(ep, story, model=model, n_lines=len(lines), n_masked=n_masked,
                      episode_title=None)

    # 6. ② 人格の物語理解（事実は story① に接地固定）
    sys_u, pre_u, vol_u = prompts.build_understanding(persona_text, history, analysis_transcript,
                                                      story, cast, asr=asr)
    understanding = llm.complete(system=sys_u, stable_prefix=pre_u, volatile=vol_u)
    store.write_understanding(ep, persona_id, understanding,
                              persona_prompt_id=persona_prompt_id, model=model)

    # 7. ③ 感想（理解の上に乗せる）
    sys_i, pre_i, vol_i = prompts.build_impression(persona_text, history, analysis_transcript, story,
                                                   understanding, cast, asr=asr)
    impression = llm.complete(system=sys_i, stable_prefix=pre_i, volatile=vol_i)
    store.write_impression(ep, persona_id, impression,
                           persona_prompt_id=persona_prompt_id, model=model)

    # 7.5 単発/連続の判定（次回予想を出すかの分岐・監査用に保存）。
    #     日付話数は判定LLM不要、それ以外は小さな判定コール1回。
    serial_next, serial_reason = _decide_serial_next(
        llm, ep, title or series_id, analysis_transcript, asr
    )
    store.write_serial_next(ep, serial_next, serial_reason)

    # 8. ③ 予想・期待（連続もののときだけ）
    #    outlook は元々あらすじ(圧縮要約)＋感想しか見ず、今回どこまで描かれたかを取り違えて
    #    一話ずれる。understanding/impression と同じく今回の話の台本(全体)を渡して接地する。
    if serial_next:
        sys_x, pre_x, vol_x = prompts.build_outlook(
            persona_text, history, story, impression, cast,
            episode_transcript=analysis_transcript, asr=asr,
        )
        outlook = llm.complete(system=sys_x, stable_prefix=pre_x, volatile=vol_x)
    else:
        # 単発回は次回が無い＝予想させない（無理に書かせると捏造になる）。NULL でなく空文字を
        # 保存するのが要点＝呼び出し側の has_outlook（IS NOT NULL 判定）が true のまま保たれる。
        outlook = ""
    store.write_outlook(ep, persona_id, outlook,
                        persona_prompt_id=persona_prompt_id, model=model)

    # 8.5 ブロック封印（§4.5）: この話でブロックが1つ埋まったら圧縮版を作る
    #     （客観は persona 非依存で1回・主観は persona 別・生レイヤーは不変のまま）
    compression.maybe_seal(store, llm, model, persona_id, persona_text,
                           persona_prompt_id, ep, cfg.block_size)

    # 9. 意味で引く検索の文書ベクトル（[embed] 設定時のみ）。落ちても話の処理は失敗にしない
    #    （あとで `oco embed --title` で埋め戻せる）。
    if cfg.embed_base_url:
        try:
            embed_episode(cfg, store, ep)
        except Exception as e:
            log.warning("ep%s の埋め込みに失敗（文字検索は使える）: %r", ep, e)

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
