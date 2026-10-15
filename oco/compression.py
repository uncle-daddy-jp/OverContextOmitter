"""階層ローリング圧縮（§4.5・長編/小コンテキスト窓対応）。

30〜40話（や 8k〜32k 窓のローカルモデル）で「毎ターン積む作業コンテキスト」が
膨らむのを防ぐ。**生レイヤー（各話 story/impression/outlook・lines）は圧縮も削除も
しない＝recall の対象**。圧縮するのは作業コンテキストだけ。

ブロック = block_size 話。進行中ブロックは生を積み、埋まったら封印して圧縮版に：
  - 客観＝ブロックあらすじ（人物・関係・未回収の伏線=開いた糸を保持・persona非依存）
  - 主観＝ブロック感想ダイジェスト（心情＋未解決の予想・期待を持ち越し・persona別）

作業コンテキスト = 封印済みブロック要約群 ＋ 進行中ブロックの生（< 現在話）。

    ep6  : 圧縮1-5 + 生6(今回)
    ep10 : 圧縮1-5 + 生6..9 + 生10(今回)   → ep10 到達で 圧縮6-10 を封印
    ep11 : 圧縮1-5 + 圧縮6-10 + 生11(今回)

block_size<=0 で無効化＝全話ベタ貼り（後方互換）。伏線の回収は recall() が生レイヤーに
届くので、圧縮に載らなかった細部も引き戻せる（圧縮=作業軽量化 / recall=生から精度回収）。

前提：話は概ね順次投入される（飛ばし投入時は封印が保留され作業コンテキストが少し太るだけ）。
"""

from __future__ import annotations

from . import prompts


# ── 巨大単話transcriptの map-reduce 圧縮（context 溢れ根絶・しきい値超のときだけ） ──
def transcript_too_large(transcript: str, cfg) -> bool:
    """単話transcriptがしきい値（文字数）超か。超過時のみ圧縮する。"""
    return len(transcript or "") > getattr(cfg, "condense_threshold_chars", 30000)


def _split_transcript_by_lines(transcript: str, chunk_chars: int) -> list[str]:
    """行（改行split）境界を保ったまま各チャンクが概ね chunk_chars 以下になるよう貪欲に詰める。

    1行が chunk_chars を超えても必ず1チャンクに入れて無限増殖させない
    （バッファが空なら閾値超でもその1行を入れる）。最後の余りも append。
    """
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for line in (transcript or "").split("\n"):
        add = len(line) + (1 if buf else 0)  # 連結時の改行ぶん
        if buf and size + add > chunk_chars:
            chunks.append("\n".join(buf))
            buf = [line]
            size = len(line)
        else:
            buf.append(line)
            size += add
    if buf:
        chunks.append("\n".join(buf))
    return chunks


def condense_transcript(llm, model: str, transcript: str, cfg,
                        asr: bool = False, _depth: int = 0) -> str:
    """巨大単話transcriptを map-reduce で圧縮した「後段用 analysis_transcript」を返す。

    生 transcript は別に store 済み（recall 無傷）。ここは cast/story/理解/感想 の LLM が
    context を溢れさせないための作業用縮約。深さ2で打ち切り、無限ループを防ぐ。
    asr=True（音声認識由来）なら map 段のプロンプトにも誤変換の警告を入れる。
    """
    chunk_chars = getattr(cfg, "condense_chunk_chars", 12000)
    chunks = _split_transcript_by_lines(transcript, chunk_chars)
    if len(chunks) <= 1:
        return transcript  # これ以上割れない（呼び側#1a側で最終 error→skip の安全網あり）
    digests: list[str] = []
    for i, ch in enumerate(chunks):
        sys_c, pre_c, vol_c = prompts.build_condense_chunk(ch, i + 1, len(chunks), asr=asr)
        digests.append(llm.complete(system=sys_c, stable_prefix=pre_c, volatile=vol_c))
    combined = "\n\n".join(
        f"── 区間{i + 1}/{len(digests)} ──\n{d}" for i, d in enumerate(digests)
    )
    if _depth < 2 and transcript_too_large(combined, cfg):
        return condense_transcript(llm, model, combined, cfg, asr=asr, _depth=_depth + 1)
    return combined


def _covered_max(store) -> int:
    blocks = store.read_blocks()
    return blocks[-1]["ep_hi"] if blocks else 0


def objective_prior_context(store, ep: int, block_size: int) -> list[tuple[str, str]]:
    """build_objective 用: [(ラベル, 本文)]。封印ブロック要約＋進行中ブロックの生 story。"""
    blocks = store.read_blocks() if block_size and block_size > 0 else []
    covered = blocks[-1]["ep_hi"] if blocks else 0
    ctx: list[tuple[str, str]] = []
    for b in blocks:
        ctx.append((f"第{b['ep_lo']}-{b['ep_hi']}話 まとめ（圧縮）", b["text"] or ""))
    for e in store.processed_episodes():
        if e <= covered or e >= ep:
            continue
        st = store.read_story(e)
        if st:
            ctx.append((f"第{e}話 あらすじ", st))
    return ctx


def persona_prior_context(store, persona: str, ep: int, block_size: int) -> list[dict]:
    """_persona_prefix 用: history list[dict]。

    封印ブロック（客観まとめ＋主観ダイジェスト・is_block=True）＋進行中ブロックの生話。
    """
    blocks = store.read_blocks() if block_size and block_size > 0 else []
    covered = blocks[-1]["ep_hi"] if blocks else 0
    hist: list[dict] = []
    for b in blocks:
        hist.append({
            "label": f"第{b['ep_lo']}-{b['ep_hi']}話（圧縮）",
            "is_block": True,
            "story": b["text"] or "",                              # 客観ブロック要約
            "digest": store.read_block_view(b["block_index"], persona),  # 主観ダイジェスト
        })
    for e in store.processed_episodes():
        if e <= covered or e >= ep:
            continue
        st = store.read_story(e)
        if not st:
            continue
        hist.append({
            "label": f"第{e}話",
            "ep": e,
            "story": st,
            "impression": store.read_impression(e, persona),
            "outlook": store.read_outlook(e, persona),
        })
    return hist


def maybe_seal(store, llm, model, persona_id: str, persona_text: str,
               persona_prompt_id: int | None, ep: int, block_size: int) -> int | None:
    """ep 到達までに完成した未封印ブロックを（追いつけるだけ）全て封印する。

    客観封印は persona 非依存で1回だけ（既存ならスキップ）、主観封印は persona 別。
    最後に封印した block_index を返す（何も封印しなければ None）。

    ★追いつき（2026-07 実障害の根治）: 旧実装は `ep == covered + block_size` の**ちょうどの
    境界話でしか封印しなかった**。そのため境界話を封印前に通過すると（圧縮機能の後付けで
    既存作品の境界が過去になった／飛ばし投入で境界を跨いだ 等）covered が 0 のまま進まず、
    以後どの話でも境界に一致せず**永久に封印されない**＝作業コンテキストが話数に比例して
    無限増殖し、いつかモデルのコンテキスト窓を割る（豊臣兄弟 第21話が 33176>32768 tokens で
    400 になった実例）。境界の「取りこぼし」に耐えるよう、covered より上・ep 以下で完成
    している区間を昇順に全て封印して追いつく。
    """
    if not block_size or block_size <= 0:
        return None
    sealed: int | None = None
    # covered は封印のたびに前進する。完成済み（hi <= ep）の区間が尽きるまで繰り返す。
    while True:
        covered = _covered_max(store)
        hi = covered + block_size
        if hi > ep:
            break  # 次のブロックはまだ完成していない（今話がその区間の途中/未達）
        lo = covered + 1
        block_index = hi // block_size
        if not _seal_block(store, llm, model, persona_id, persona_text,
                           persona_prompt_id, lo, hi, block_size):
            break  # 素の story が皆無の区間は封印できない＝covered が進まず無限ループになるので打ち切り
        sealed = block_index
    return sealed


def _seal_block(store, llm, model, persona_id: str, persona_text: str,
                persona_prompt_id: int | None, lo: int, hi: int, block_size: int) -> bool:
    """区間 [lo..hi] を1ブロック封印する。客観（persona非依存）＋主観（persona別）を未生成のときだけ。

    客観封印に必要な素の story が区間内に1つも無ければ False（＝封印せず・呼び側で打ち切り）。
    欠番（ディスクに無い話）は story/感想が無いだけなので、在る話だけで封印する。
    封印できた（または既に封印済みだった）なら True。
    """
    block_index = hi // block_size

    # 客観封印（未生成のときだけ・persona非依存）
    if store.read_block(block_index) is None:
        block_stories = [(e, store.read_story(e)) for e in range(lo, hi + 1) if store.read_story(e)]
        if not block_stories:
            return False  # 素の story が無い区間は封印しない（安全）
        prior_blocks = [(b["ep_lo"], b["ep_hi"], b["text"] or "") for b in store.read_blocks()]
        sys_b, pre_b, vol_b = prompts.build_block_objective(prior_blocks, block_stories, lo, hi)
        text = llm.complete(system=sys_b, stable_prefix=pre_b, volatile=vol_b)
        store.write_block(block_index, lo, hi, block_size, text, model=model)

    # 主観封印（persona別・未生成のときだけ）。欠番話は感想も予想も無いので view から落とす。
    if store.read_block_view(block_index, persona_id) is None:
        views = []
        for e in range(lo, hi + 1):
            imp = store.read_impression(e, persona_id)
            out = store.read_outlook(e, persona_id)
            if imp or out:
                views.append({"ep": e, "impression": imp, "outlook": out})
        block_obj = store.read_block(block_index)
        sys_s, pre_s, vol_s = prompts.build_block_subjective(
            persona_text, views, block_obj["text"] if block_obj else None, lo, hi
        )
        digest = llm.complete(system=sys_s, stable_prefix=pre_s, volatile=vol_s)
        store.write_block_view(block_index, persona_id, digest,
                               persona_prompt_id=persona_prompt_id, model=model)

    return True
