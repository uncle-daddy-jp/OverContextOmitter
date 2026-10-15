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


# ── ASR(音声認識)台本の警告 ────────────────────────────────
# whisper 由来の台本は聞き間違い・同音異義語（「トスパン中尉」→「トスパン注意」等）や
# OP歌詞の意味不明列を含み、放っておくと存在しない固有名詞が人物相関・あらすじに定着する。
_ASR_WARNING = (
    "注意: この台本は音声認識で自動作成されており、聞き間違い・同音異義語の誤変換・"
    "歌詞や効果音の誤転写を含みうる。意味の通らない語句を無理に意味づけしない。"
    "固有名詞は文脈から確信できる場合のみ使い、不確かな名前は役割語（隊長、老人 など）で書く。"
)


def _asr_block(asr: bool) -> str:
    """ASR由来のときだけ警告ブロックを返す（字幕由来は空＝従来と完全同一のプロンプト）。

    注入先は必ず volatile 側。3人格パス(②③③)が完全一致で共有する stable_prefix には
    入れない（崩れるとキャッシュミスでコストが線形に増える）。3パスとも volatile の
    同一位置（人物相関ブロックの直後）に同一文字列で入れること。
    """
    return f"{_ASR_WARNING}\n\n" if asr else ""


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


def build_cast(prior_roster: str | None, script: str, asr: bool = False) -> tuple[str, str, str]:
    """人物相関を更新生成。(system, stable_prefix, volatile)。"""
    pr = f"これまでの人物相関（引き継ぎ元）:\n{prior_roster}\n\n" if prior_roster else ""
    volatile = (
        f"{pr}"
        f"{_asr_block(asr)}"
        "── 今回の話の台本 ──\n"
        f"{script}\n\n"
        "── 指示 ──\n"
        "現時点の人物相関を出力せよ。今回の台本に出る人物＋シリーズのレギュラーだけ。"
        "前回限りのゲストで今回出てこない人物は外す。続き物で今回も出る人物は残し関係を更新する。"
        "前回の相関に誤りがあれば台本に基づいて訂正する。"
    )
    return CAST_SYSTEM, "", volatile


# ── 巨大単話transcriptの圧縮：map段チャンク（あらすじ生成・人物把握の素材化） ──
CONDENSE_SYSTEM = (
    "あなたは長い映像作品の台本を、後段のあらすじ生成・人物把握のために圧縮する編集者。"
    "冗長な言い回しだけを削り、後で全体のあらすじと人物相関を書くのに必要な素材は落とさない。"
)


def build_condense_chunk(chunk_script: str, idx: int, total: int,
                         asr: bool = False) -> tuple[str, str, str]:
    """台本の一部(idx/total)を後段用の素材メモへ圧縮。

    あらすじ本文でなく出来事/固有名/人物の役割・言動/数値を時系列で残し冗長な繰り返しだけ削る。
    """
    volatile = (
        f"{_asr_block(asr)}"
        f"── 台本の一部（全{total}分割中の {idx}）──\n"
        f"{chunk_script}\n\n"
        "── 指示 ──\n"
        "この区間で起きた出来事・やりとり・判明した事実、および登場人物とその役割・言動を、"
        "時系列で簡潔にまとめよ。固有名・数値・固有情報は落とさず、冗長な言い回し・繰り返しだけを削る。"
        "あらすじ本文ではなく、後で全体をまとめるための素材メモとして書く。"
    )
    return CONDENSE_SYSTEM, "", volatile


# ── 話者同定（クラスタ→キャラ名・確信優先・確信なければ unknown） ──────
SPEAKER_NAMING_SYSTEM = (
    "あなたは映像作品の話者同定係。各『話者クラスタ』のセリフ例と登場人物リストを見て、"
    "どのクラスタがどのキャラクターかを対応づける。手がかりは呼びかけ（「フリーレン！」）・"
    "自己紹介・口調・文脈。確信できるクラスタだけ名付け、判断できないクラスタは unknown とする"
    "（無理に当てない＝確信優先）。台本にない人物を作らない。"
)


def build_line_attribution(numbered_script: str, cast: str | None,
                           asr: bool = False) -> tuple[str, str, str]:
    """字幕なし(ASR・クラスタ無し)用: 各行の話者をテキストだけで推定。"""
    volatile = (
        f"── 登場人物（候補・人物相関） ──\n{cast or '(なし)'}\n\n"
        f"{_asr_block(asr)}"
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


def build_speaker_naming(labeled_script: str, cast: str | None,
                         asr: bool = False) -> tuple[str, str, str]:
    volatile = (
        f"── 登場人物（候補・人物相関） ──\n{cast or '(なし)'}\n\n"
        f"{_asr_block(asr)}"
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
                    cast: str | None = None, asr: bool = False,
                    corrections: str | None = None) -> tuple[str, str, str]:
    """(system, stable_prefix, volatile) を返す。

    prior_context: (ラベル, 本文) の列（§4.5 作業コンテキスト）。
      封印済みブロックは「第1-5話 まとめ（圧縮）」、進行中の生話は「第6話 あらすじ」等。
    corrections: 検証パス（build_story_check）が挙げた大幅な誤り。渡すと volatile 末尾に
      訂正指示として足す＝書き直し（1回だけ・再検証はしない）。
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
        f"{_asr_block(asr)}"
        "── 今回の話の台本（CMマスク済み） ──\n"
        f"{script}\n\n"
        "── 指示 ──\n"
        "この話のあらすじを 400 字程度で書け。これまでの流れを踏まえ、"
        "この話で起きた出来事・判明した事実を時系列で。前提説明は不要、本題だけ。"
    )
    if corrections:
        volatile += (
            "\n\n前回のあらすじには以下の大幅な誤りが指摘された。台本に基づき訂正して書き直せ:\n"
            f"{corrections}"
        )
    return OBJECTIVE_SYSTEM, stable_prefix, volatile


# ── 検証パス：あらすじ①の「大幅な誤り」だけを拾う（省略は仕様＝指摘しない） ──────
# ★出力は serial判定と同じ「1行目に判定語」方式に揃える。緩い判定（「問題なし」始まりか）
#   だと、あらすじを項目ごとに echo して各項目に「*(検証)*: 台本通り。」と注記する形式で
#   返すモデル（gemma4-26b 実測）を誤りありと誤判定し、毎話1コール無駄に書き直していた。
STORY_CHECK_SYSTEM = (
    "あなたは映像作品のあらすじの検証係。台本とあらすじを突き合わせ、**大幅な誤りだけ**を"
    "見つける。拾うのは次の4種類だけ:\n"
    "(a) 人物の取り違え（誰が何をしたか）\n"
    "(b) 因果関係の逆転\n"
    "(c) 台本に存在しない出来事の捏造\n"
    "(d) 話の主軸となる出来事の欠落・誤解\n"
    "細部の省略・表現の差・些細な違いは指摘しない（あらすじは要約であり、省略はこのシステムの仕様）。\n"
    "出力形式（厳守）: 1行目は「問題なし」か「大幅な誤りあり」のどちらか1語だけ"
    "（記号・説明を付けない）。「大幅な誤りあり」の場合のみ2行目以降に誤りの箇条書き"
    "（各行 - で始める）。あらすじの引用・逐条の確認結果・書き直し案は書かない。"
)


def build_story_check(script: str, story: str, asr: bool = False) -> tuple[str, str, str]:
    """あらすじ①の検証。(system, stable_prefix, volatile)。

    出力形式は厳格な「1行目＝判定語（問題なし / 大幅な誤りあり）＋2行目以降＝箇条書き」。
    呼び側は1行目の完全一致でパースし、「大幅な誤りあり」のときだけ2行目以降を
    build_objective(corrections=...) に渡して1回だけ書き直す（再検証しない＝ループ禁止）。
    どちらの判定語でもない出力は安全側＝書き直さない。
    """
    volatile = (
        f"{_asr_block(asr)}"
        "── 今回の話の台本 ──\n"
        f"{script}\n\n"
        "── 検証対象のあらすじ ──\n"
        f"{story}\n\n"
        "── 指示 ──\n"
        "台本とあらすじを突き合わせ、(a)人物の取り違え (b)因果関係の逆転 "
        "(c)台本に存在しない出来事の捏造 (d)話の主軸となる出来事の欠落・誤解 に当たる"
        "**大幅な誤りだけ**を見つけよ。細部の省略・言い回しの差は指摘しない。\n"
        "出力形式（厳守）:\n"
        "1行目: 「問題なし」か「大幅な誤りあり」のどちらか1語だけ（記号・説明を付けない）\n"
        "2行目以降: 「大幅な誤りあり」のときだけ、誤りを箇条書きで（各行 - で始める）\n"
        "あらすじの引用・逐条の確認結果・書き直し案は書かない。"
    )
    return STORY_CHECK_SYSTEM, "", volatile


# ── 判定パス：単発/連続（次回予想を出すかの分岐） ────────────────────
SERIAL_CHECK_SYSTEM = (
    "あなたは映像作品の回種別の判定係。台本を読み、この回が『次回に物語が続く連続もの』か"
    "『単発』かだけを判定する。あらすじ・感想・予想は書かない。"
)


def build_serial_check(script: str, series_title: str,
                       asr: bool = False) -> tuple[str, str, str]:
    """単発/連続の判定。(system, stable_prefix, volatile)。

    出力形式は厳格な2行（1行目＝「連続」か「単発」の1語 / 2行目＝短い理由）。呼び側は
    1行目の完全一致でパースし、どちらでもなければ安全側＝連続として扱う（予想を出す）。
    """
    volatile = (
        f"作品名: {series_title}\n\n"
        f"{_asr_block(asr)}"
        "── 今回の話の台本 ──\n"
        f"{script}\n\n"
        "── 指示 ──\n"
        "この回は次回に物語が続く連続もの（連続ドラマ/アニメの1話・次回への引きがある）か、"
        "それとも単発（映画・1話完結・最終回・単発特番・情報/通販番組）か。\n"
        "出力形式（厳守）:\n"
        "1行目: 「連続」か「単発」のどちらか1語だけ（記号・説明・句点を付けない）\n"
        "2行目: そう判断した短い理由（1文）"
    )
    return SERIAL_CHECK_SYSTEM, "", volatile


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
    asr: bool = False,
) -> tuple[str, str, str]:
    stable_prefix = _persona_prefix(persona_text, history)
    volatile = (
        f"{_cast_block(cast)}"
        f"{_asr_block(asr)}"
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
    asr: bool = False,
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
        f"{_asr_block(asr)}"
        "── 今回の話の台本 ──\n"
        f"{script}\n\n"
        "── 今回の話のあらすじ（客観） ──\n"
        f"{current_story}\n\n"
        "── 今回のあなたの理解 ──\n"
        f"{current_understanding}\n\n"
        "── 指示 ──\n"
        "この話を見たあなたの感想を 400 字程度で。理解を踏まえ、心が動いた所も、動かなかった所も。"
        "よかった点だけでなく退屈・不満・違和感も正直に、あなたの言葉で。"
        "理解の言い直しでなく“感じたこと”を書く。"
    )
    return system, stable_prefix, volatile


def build_outlook(
    persona_text: str,
    history: list[dict],
    current_story: str,
    current_impression: str,
    cast: str | None = None,
    episode_transcript: str | None = None,
    asr: bool = False,
) -> tuple[str, str, str]:
    # ★一話ずれ対策: outlook は元々 あらすじ(圧縮要約)＋感想 しか見ないため、あらすじの結びが
    #   圧縮的だと(例「山崎へ向かう中国大返しを開始する」)モデルが今回すでに描かれた主題を
    #   「次回の予想」に回す(=感想が一話ぶん後ろにずれる)。番組ごとに結びの言い回しは違い、
    #   末尾だけ渡すのは当てにならない → understanding/impression と同じく今回の話の台本(全体)を
    #   渡し、「今回どこまで描かれたか」をモデル自身に台本全体から見極めさせて接地する。
    #   ※transcript は analysis_transcript（巨大話は map-reduce 済みダイジェスト）＝有界。
    system = (
        "あなたは下記の人格。今回の話を踏まえ、次回への予想・期待を述べる。"
        "これまでの経験（過去話の予想と結果）をレンズに、根拠のある予想を。"
        "願望と予想は分けて書く。誰が誰かは相関に従い取り違えない。\n"
        "★下の台本＝今回すでに描かれた範囲。ここに描かれた出来事は“次回の予想”にしない。"
        "台本全体から今回がどこまで進んだかを見極め、その先で次に何が起きるかを予想する。\n"
        + _GROUNDING
    )
    stable_prefix = _persona_prefix(persona_text, history)
    transcript_block = (
        "── 今回の話の台本（全体・これが今回描かれた範囲） ──\n"
        f"{episode_transcript}\n\n"
        if episode_transcript else ""
    )
    volatile = (
        # ASR警告は3人格パスとも volatile の同一位置（人物相関の直後）に同一文字列で置く
        f"{_cast_block(cast)}"
        f"{_asr_block(asr)}"
        "── 今回の話のあらすじ ──\n"
        f"{current_story}\n\n"
        f"{transcript_block}"
        "── 今回のあなたの感想 ──\n"
        f"{current_impression}\n\n"
        "── 指示 ──\n"
        "次回どうなると思うか（予想）と、何を楽しみにしているか（期待）を"
        "あわせて 400 字程度で。伏線や引っかかっている点があれば挙げる。"
        "既に今回の台本で描かれたことは予想に書かず、今回の到達点の先を予想する。"
    )
    return system, stable_prefix, volatile
