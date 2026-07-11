"""プロンプト組み立て。

組み立て順は「過去の固定プレフィックス → 今回の台本 → 指示」を厳守
（安定プレフィックスを壊さない＝キャッシュを効かせる）。

- 客観パス（あらすじ）＝人格非依存・接地必須・捏造禁止。
- 人格パス（感想・予想）＝主観自由だが「何が起きたか」は動かさない。
"""

from __future__ import annotations

# ── 共通の接地ガード（暴走/捏造抑制） ──────────────────────────
_GROUNDING = (
    "重要な制約:\n"
    "- 字幕（台本）をそのまま書き写さない。要約・解釈する。\n"
    "- 台本にいない話者・出来事を作らない（捏造禁止）。\n"
    "- 同じ語の反復・列挙で字数を埋めない。\n"
)


def _hhmmss(sec: float) -> str:
    s = int(sec)
    h, rem = divmod(s, 3600)
    m, ss = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{ss:02d}"


def render_transcript(records: list[dict]) -> str:
    """エントリ列を HH:MM:SS 付きの整形済みトランスクリプト（=保存物＝LLM入力台本）に。

    kind='line'  … 字幕セリフ。話者名(色キー #... は省く)を（）で前置。
    kind='scene' … 後で VLM が入れる映像状況説明。〔映像〕で前置。
    時刻順（records は時系列で渡る前提）にそのまま連結するので、後から scene を
    時刻順に挿入しても自然に混ざる。
    """
    out = []
    for r in records:
        ts = _hhmmss(r.get("t", 0))
        text = r.get("text", "")
        if r.get("kind", "line") == "scene":
            out.append(f"{ts} 〔映像〕{text}")
            continue
        sp = r.get("speaker")
        # 色キー(#RRGGBB)は人物名でないので前置しない（recall/lines には保持）
        if sp and not sp.startswith("#"):
            out.append(f"{ts} （{sp}）{text}")
        else:
            out.append(f"{ts} {text}")
    return "\n".join(out)


# ── 人物相関パス：cast（carry-forward・客観・取り違え防止の接地） ──────
CAST_SYSTEM = (
    "あなたは映像作品の登場人物の記録係。台本から、登場人物とその関係（人物相関）を"
    "簡潔な箇条書きで日本語にまとめる。各人物は「名前（立場/別名） — 誰とどういう関係か」の形で。"
    "台本に根拠のある人物・関係だけ書く。原作知識や一般常識で勝手に補わない"
    "（誰と誰が将来結ばれる等の先回り・先読みは禁止）。曖昧な関係は「関係不明」とし推測で結びつけない。\n"
    "★相関に残すのは『今回の台本に登場する人物』と『シリーズのレギュラー（毎回出る主要人物）』だけ。"
    "前回までの相関は参考にするが、前回限りのゲストで今回の台本に出てこない人物は外す"
    "（1話完結ものは回ごとに事件もゲストも替わる）。前回からの続き物なら、またがる人物は今回も"
    "台本に出てくるはずなので自然に残る。\n"
    + _GROUNDING
)


def build_cast(prior_roster: str | None, script: str) -> tuple[str, str, str]:
    """人物相関を更新生成。(system, stable_prefix, volatile)。"""
    pr = f"これまでの人物相関（引き継ぎ元）:\n{prior_roster}\n\n" if prior_roster else ""
    volatile = (
        f"{pr}"
        "── 今回の話の台本 ──\n"
        f"{script}\n\n"
        "── 指示 ──\n"
        "現時点の人物相関を出力せよ。今回の台本に出る人物＋シリーズのレギュラーだけ。"
        "前回限りのゲストで今回出てこない人物は外す。続き物で今回も出る人物は残し関係を更新する。"
        "前回の相関に誤りがあれば台本に基づいて訂正する。"
    )
    return CAST_SYSTEM, "", volatile


# ── 話者同定（クラスタ→キャラ名・確信優先・確信なければ unknown） ──────
SPEAKER_NAMING_SYSTEM = (
    "あなたは映像作品の話者同定係。各『話者クラスタ』のセリフ例と登場人物リストを見て、"
    "どのクラスタがどのキャラクターかを対応づける。手がかりは呼びかけ（「フリーレン！」）・"
    "自己紹介・口調・文脈。確信できるクラスタだけ名付け、判断できないクラスタは unknown とする"
    "（無理に当てない＝確信優先）。台本にない人物を作らない。"
)


def build_line_attribution(numbered_script: str, cast: str | None) -> tuple[str, str, str]:
    """字幕なし(ASR・クラスタ無し)用: 各行の話者をテキストだけで推定。"""
    volatile = (
        f"── 登場人物（候補・人物相関） ──\n{cast or '(なし)'}\n\n"
        "── 番号付きの台本（話者不明） ──\n"
        f"{numbered_script}\n\n"
        "── 指示 ──\n"
        "各行を喋っているのが登場人物の誰かを推定せよ。手がかりは『呼びかけ』（誰が誰の名を呼ぶか）・"
        "口調・一人称・会話のターン交替（普通は話者が交互）・文脈。"
        "確信できる行だけキャラ名、不明な行は unknown。\n"
        "全行を1行ずつ『行番号=キャラ名』で出力。**名前だけ書く**（注釈・理由・括弧書き・"
        "英語の説明を一切付けない）。例: 1=フリーレン / 2=unknown / 3=シュタルク"
    )
    return SPEAKER_NAMING_SYSTEM, "", volatile


def build_speaker_naming(labeled_script: str, cast: str | None) -> tuple[str, str, str]:
    volatile = (
        f"── 登場人物（候補・人物相関） ──\n{cast or '(なし)'}\n\n"
        "── 話者ラベル付きの台本（時系列・(話者A)等は匿名の声クラスタ） ──\n"
        f"{labeled_script}\n\n"
        "── 指示 ──\n"
        "各話者ラベル(話者A,話者B,…)が登場人物の誰かを対応づけよ。"
        "最大の手がかりは『呼びかけ』＝あるキャラが名前を呼んだ直後/直前に喋るラベルがその人。"
        "口調・一人称・自己紹介・文脈も使う。確信できるラベルだけ名付け、不明は unknown。\n"
        "各ラベルを1行ずつ『話者X=キャラ名』で出力（確信なければ『話者X=unknown』）。\n"
        "例:\n話者A=フリーレン\n話者B=unknown"
    )
    return SPEAKER_NAMING_SYSTEM, "", volatile


def _cast_block(cast: str | None) -> str:
    if not cast:
        return ""
    return (
        "── 登場人物の相関（今わかっている範囲・誰が誰かはこれに従い取り違えない） ──\n"
        f"{cast}\n\n"
    )


# ── 客観パス：あらすじ生成 ────────────────────────────────────
OBJECTIVE_SYSTEM = (
    "あなたは映像作品の客観的な記録係。与えられた話の字幕（台本）から、"
    "その話で「何が起きたか」だけを淡々と日本語であらすじにする。"
    "人格・感想・推測は混ぜない。登場人物の相関が与えられたら、誰が誰かはそれに従い取り違えない。\n"
    + _GROUNDING
)


def build_objective(prior_context: list[tuple[str, str]], script: str,
                    cast: str | None = None) -> tuple[str, str, str]:
    """(system, stable_prefix, volatile) を返す。

    prior_context: (ラベル, 本文) の列（§4.5 作業コンテキスト）。
      封印済みブロックは「第1-5話 まとめ（圧縮）」、進行中の生話は「第6話 あらすじ」等。
    """
    prefix_parts = []
    for label, text in prior_context:
        prefix_parts.append(f"=== {label} ===\n{text}")
    stable_prefix = (
        "これまでの流れ（前提。『まとめ』は複数話を圧縮した区間・『あらすじ』は単話そのまま）:\n\n"
        + "\n\n".join(prefix_parts)
        if prior_context
        else ""
    )
    volatile = (
        f"{_cast_block(cast)}"
        "── 今回の話の台本（CMマスク済み） ──\n"
        f"{script}\n\n"
        "── 指示 ──\n"
        "この話のあらすじを 400 字程度で書け。これまでの流れを踏まえ、"
        "この話で起きた出来事・判明した事実を時系列で。前提説明は不要、本題だけ。"
    )
    return OBJECTIVE_SYSTEM, stable_prefix, volatile


# ── 人格パス共通：安定プレフィックス（3パスで同一文字列＝キャッシュ共有の要） ──
def _persona_prefix(
    persona_text: str,
    history: list[dict],
) -> str:
    """人格設定＋作業コンテキスト（§4.5 階層ローリング圧縮）。

    history は封印済みブロック（客観まとめ＋主観ダイジェスト）と、進行中ブロックの生話
    （あらすじ＋感想＋予想）が混在する。生レイヤーは削除されず recall の対象のまま。
    ②understanding は容量試算を守るため履歴に含めない（今回話のみ生成）。
    understanding/impression/outlook の3人格パスはこの文字列を**完全一致**で共有すること
    （崩れるとキャッシュミスでコストが線形に増える）。
    """
    parts = [f"あなたの人格:\n{persona_text}"]
    if history:
        parts.append(
            "これまで見てきた流れ（『まとめ』は複数話を圧縮した区間・単話はそのまま）。"
            "今回が前回からの“続き”なら、前回の自分の予想と今回の出来事を照らして"
            "（当たった/外れた/予想外）触れたり、人物の変化や伏線を踏まえて深く繋げてよい。"
            "逆に独立した回（1話完結）なら、前回の出来事やテーマを今回へ無理に持ち込まない"
            "（事件もゲストも回ごとに替わる）。続きか独立かは、今回の台本が前回から地続きかで判断する:"
        )
        for h in history:
            label = h.get("label") or f"第{h.get('ep')}話"
            block = [f"=== {label} ==="]
            if h.get("is_block"):
                block.append(f"[この区間のまとめ（客観）] {h.get('story', '')}")
                if h.get("digest"):
                    block.append(f"[この区間の自分（感想・予想の要約）] {h['digest']}")
            else:
                block.append(f"[あらすじ] {h.get('story', '')}")
                if h.get("impression"):
                    block.append(f"[あなたの感想] {h['impression']}")
                if h.get("outlook"):
                    block.append(f"[あなたの予想・期待] {h['outlook']}")
            parts.append("\n".join(block))
    return "\n\n".join(parts)


# ── §4.5 ブロック圧縮：客観（ブロックあらすじ・開いた糸を残す） ─────────────
BLOCK_OBJECTIVE_SYSTEM = (
    "あなたは映像作品の客観的な記録係。連続する数話ぶんの各話あらすじを、"
    "後の話を読むときの前提として使える『一つの区間まとめ』に圧縮する。"
    "人格・感想・推測は混ぜない。原作知識や一般常識で先読み・補完しない。\n"
    + _GROUNDING
)


def build_block_objective(
    prior_blocks: list[tuple[int, int, str]],
    block_stories: list[tuple[int, str]],
    lo: int,
    hi: int,
) -> tuple[str, str, str]:
    """区間 [lo..hi] の各話あらすじ→客観ブロックまとめ。(system, stable_prefix, volatile)。

    prior_blocks: これより前の封印済み [(ep_lo, ep_hi, text)]（継続性の前提）。
    """
    prefix = ""
    if prior_blocks:
        prefix = "これより前の区間のまとめ（前提）:\n\n" + "\n\n".join(
            f"=== 第{a}-{b}話 まとめ ===\n{t}" for a, b, t in prior_blocks
        )
    body = "\n\n".join(f"=== 第{ep}話 あらすじ ===\n{st}" for ep, st in block_stories)
    volatile = (
        f"── 今回まとめる区間（第{lo}-{hi}話）の各話あらすじ ──\n{body}\n\n"
        "── 指示 ──\n"
        f"第{lo}話から第{hi}話までを、後の話を読むときの前提として使える一つのまとめに"
        "600字程度で圧縮せよ。起きた大筋・登場人物と関係の変化を残す。"
        "★特に**まだ回収されていない伏線・謎・未決の問い（開いた糸）は必ず具体的に明示して残す**"
        "（後の回で回収されるので落とすと繋がらなくなる）。決着済みの些末な描写は削ってよい。"
    )
    return BLOCK_OBJECTIVE_SYSTEM, prefix, volatile


# ── §4.5 ブロック圧縮：主観（感想ダイジェスト・未解決の予想を持ち越す） ──────
def build_block_subjective(
    persona_text: str,
    views: list[dict],
    block_objective: str | None,
    lo: int,
    hi: int,
) -> tuple[str, str, str]:
    """区間 [lo..hi] の感想・予想→主観ブロックダイジェスト。

    views: [{ep, impression, outlook}]。
    予想・期待は「開いた糸」と対称に、未決着のものを持ち越して後で照合できるようにする。
    """
    system = (
        "あなたは下記の人格として映像作品を見てきた。連続する数話ぶんの自分の感想・予想を、"
        "後の話を見るときに引き継ぐ『一つの区間ダイジェスト』にまとめる。"
        "この区間を通しての心情・スタンス（何に惹かれ・何を気にしてきたか）を残す。\n"
        + _GROUNDING
    )
    stable_prefix = f"あなたの人格:\n{persona_text}"
    segs = []
    for v in views:
        seg = [f"=== 第{v['ep']}話 ==="]
        if v.get("impression"):
            seg.append(f"[感想] {v['impression']}")
        if v.get("outlook"):
            seg.append(f"[予想・期待] {v['outlook']}")
        segs.append("\n".join(seg))
    body = "\n\n".join(segs)
    volatile = (
        f"── この区間（第{lo}-{hi}話）の客観まとめ（事実の骨格） ──\n{block_objective or '(なし)'}\n\n"
        f"── この区間のあなたの感想・予想（各話） ──\n{body}\n\n"
        "── 指示 ──\n"
        f"第{lo}-{hi}話を通してのあなたの心情・スタンスのまとめを 400〜600字で書け。"
        "★**まだ答え合わせが済んでいない予想・期待（未解決のもの）は必ず具体的に残す**"
        "（後の回で当たった/外れたを照らすため）。"
        "既に決着した予想は『当たった/外れた/予想外だった』を一言添えて畳んでよい。"
    )
    return system, stable_prefix, volatile


# ── 人格パス②：物語理解（理解の語り。客観①とは別物・接地固定） ──────────
UNDERSTANDING_SYSTEM = (
    "あなたは下記の人格として映像作品を見ている。今回の話を、あなたが何に注目し"
    "どう重みづけるかという“あなた自身のレンズ”で『理解』として語り直す。"
    "プロット要約ではなく、あなたにとってこの話は何の話だったか・何が核で何が背景か・"
    "人物関係や因果をどう読み解いたかを語る。感想（心の動き）はここでは抑える（次段で書く）。"
    "ただし“何が起きたか”という事実は曲げない＝客観あらすじを動かさない（接地固定）。\n"
    + _GROUNDING
)


def build_understanding(
    persona_text: str,
    history: list[dict],
    script: str,
    current_story: str,
    cast: str | None = None,
) -> tuple[str, str, str]:
    stable_prefix = _persona_prefix(persona_text, history)
    volatile = (
        f"{_cast_block(cast)}"
        "── 今回の話の台本 ──\n"
        f"{script}\n\n"
        "── 今回の話のあらすじ（客観・事実。これを動かすな） ──\n"
        f"{current_story}\n\n"
        "── 指示 ──\n"
        "この話を“あなたの理解”として 400 字程度で語れ。事実（客観あらすじ）は動かさず、"
        "あなたがどこに注目し・何を核と捉え・どう意味づけたかを述べる。"
    )
    return UNDERSTANDING_SYSTEM, stable_prefix, volatile


# ── 人格パス③：感想 / 予想 ─────────────────────────────────────


def build_impression(
    persona_text: str,
    history: list[dict],
    script: str,
    current_story: str,
    current_understanding: str,
    cast: str | None = None,
) -> tuple[str, str, str]:
    system = (
        "あなたは下記の人格として映像作品を見ている。今回の話を見た率直な“感想”（心の動き）を述べる。"
        "あなたの『理解』は別に書いた＝ここでは理解の上に感情を乗せる。"
        "前回までの自分の予想と照らして、当たった/外れた/予想外だった点があれば自然に触れてよい。"
        "ただし「何が起きたか」（あらすじ）は動かさない＝事実は曲げない。誰が誰かは相関に従う。\n"
        + _GROUNDING
    )
    stable_prefix = _persona_prefix(persona_text, history)
    volatile = (
        f"{_cast_block(cast)}"
        "── 今回の話の台本 ──\n"
        f"{script}\n\n"
        "── 今回の話のあらすじ（客観） ──\n"
        f"{current_story}\n\n"
        "── 今回のあなたの理解 ──\n"
        f"{current_understanding}\n\n"
        "── 指示 ──\n"
        "この話を見たあなたの感想を 400 字程度で。理解を踏まえ、心が動いた所・気になった所を、"
        "あなたの言葉で。理解の言い直しでなく“感じたこと”を書く。"
    )
    return system, stable_prefix, volatile


def build_outlook(
    persona_text: str,
    history: list[dict],
    current_story: str,
    current_impression: str,
    cast: str | None = None,
) -> tuple[str, str, str]:
    system = (
        "あなたは下記の人格。今回の話を踏まえ、次回への予想・期待を述べる。"
        "これまでの経験（過去話の予想と結果）をレンズに、根拠のある予想を。"
        "願望と予想は分けて書く。誰が誰かは相関に従い取り違えない。\n" + _GROUNDING
    )
    stable_prefix = _persona_prefix(persona_text, history)
    volatile = (
        f"{_cast_block(cast)}"
        "── 今回の話のあらすじ ──\n"
        f"{current_story}\n\n"
        "── 今回のあなたの感想 ──\n"
        f"{current_impression}\n\n"
        "── 指示 ──\n"
        "次回どうなると思うか（予想）と、何を楽しみにしているか（期待）を"
        "あわせて 400 字程度で。伏線や引っかかっている点があれば挙げる。"
    )
    return system, stable_prefix, volatile
