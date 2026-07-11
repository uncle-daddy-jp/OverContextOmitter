# OverContextOmitter (OCO) 使い方ガイド

録画番組/動画を LLM に「見せて」内容を理解させ、**人格を差し込むとその人格固有の
感想・予想・期待**を生む、汎用の **番組理解モジュール**。特定エージェント非依存・Docker。

このガイドは2部構成:
- **A. 運用者向け**（OCO を動かす）: セットアップ・設定・CLI/API/MCP
- **B. 連携者向け**（エージェント/キャラクターAI から OCO を使う）: 消費IF・出力スキーマ・取り込み指針

---

# A. 運用者向け

## A-1. セットアップ

ホストに python も ffmpeg も要らない（全部コンテナに閉じる）。

```bash
cp config.example.toml config.toml      # provider/model/base_url を編集
docker compose build

# テスト（ネット不要・spine検証）
docker compose --profile test run --rm oco-test
```

3つの動かし方（同一イメージ）:
- `oco`     … CLI 都度run（`docker compose run --rm oco <subcommand>`）
- `oco-api` … 常駐 HTTP API（:8910）
- `oco-mcp` … stdio MCP（クライアントが `run --rm -T oco-mcp` で spawn）

全サービスが同じ SQLite（named volume `oco_store`）と `config.toml` を共有する。
録画は `./recordings` に置く（compose で `/recordings` にマウント）。

## A-2. 設定（config.toml）の要点

| キー | 意味 |
|---|---|
| `store_dir` / `db_path` | SQLite の場所（WAL＝ローカルFS必須・named volume） |
| `media_root` | `media_path` はこの配下のみ許可（トラバーサル防止） |
| `speaker_naming` | ② 話者同定（クラスタ→キャラ名・既定OFF） |
| `[llm.text]` | 理解・感想生成のLLM。`provider`(openai_compat/anthropic)・`model`・`base_url`・`api_key_env`・`max_tokens`（=本文ぶん）・`thinking_budget`（-1=完走/0=OFF/N=ソフト上限）・anti-loop sampling |
| `[llm.vision]` | 映像 scene のVLM（未設定なら scene 無効）。Gemma 等のマルチモーダルを流用可 |
| `[scene]` | scene 抽出（`min_gap`/`max_gap`/`max_frames`/`frame_width`） |
| `[cm]` | CM検出。`backend`=none/jl（ロゴNCC・民放のCMフレーム除外）。`max_cm_frac`超で不安定→CM無しフォールバック |
| `[transcript]` | 台本ソース。`backend`=auto（字幕有→ARIB抽出/無→whisper）/aribcaption/whisper。`whisper_model`(small〜large-v3)/`whisper_device`(cpu/cuda) |
| `[api]` | 常駐API。`host`/`port`/`max_workers`（LLM並列度・ローカルGPUは1推奨） |
| `[webhook]` | プッシュ出力。`default_url`/`secret_env`/`retry_max` |
| `[auth]` | `token_env` を設定すると API/MCP が bearer 必須化（既定 無認証＝LAN内信頼） |

**LLMは config 切替でエンドポイントを知らない**。ローカル（vLLM/llama.cpp/Ollama）＝
`openai_compat`、クラウド Claude＝`anthropic`。

## A-3. CLI

```bash
# 第N話を処理（①②③＋生字幕＋人物相関を SQLite に書く）。
# 録画TS を --media に渡すだけ＝字幕は内部で自動抽出（別途 extract は不要）。
docker compose run --rm oco process \
  --title "葬送のフリーレン(第2期)" --ep 29 --media /recordings/ep29.ts \
  [--persona <id>] [--persona-prompt-file path] [--series-id S] [--overwrite]

# 字幕なし録画の ASR フォールバックが要るなら config で backend=auto（下記 C 参照）。
# 既に .ass を持っているなら --media にそれを渡し、映像 scene 用に動画を別指定できる:
docker compose run --rm oco process --title "..." --ep 1 \
  --media /recordings/ep1.ass --video /recordings/ep1.ts   # --video は .ass 入力時のみ意味を持つ

# 取り出し・検索・一覧
docker compose run --rm oco get  --title "フリーレン" --ep 29 [--persona <id>] [--transcript]
docker compose run --rm oco recall --title "フリーレン" 鍵
docker compose run --rm oco list  [--title "フリーレン"]

# 補助（任意）: TS の字幕ストリーム確認 / TS→.ass 抽出だけ実行
docker compose run --rm oco probe   --ts /recordings/ep29.ts
docker compose run --rm oco extract --ts /recordings/ep29.ts --ass /recordings/ep29.ass
```

> Windows の Git Bash でコンテナ内パス（`/recordings/...`）を渡す時は
> **`MSYS_NO_PATHCONV=1`** を付ける（パス変換でWindowsパスに化けるのを防ぐ）。

## A-4. 常駐API（:8910）

```bash
docker compose up -d oco-api
curl "http://localhost:8910/episodes/29?title=葬送のフリーレン(第2期)&persona_id=default"
curl -X POST localhost:8910/episodes -H 'content-type: application/json' \
  -d '{"title":"...","episode":30,"media_path":"/recordings/ep30.ts"}'
```

---

# B. 連携者向け（エージェント/キャラクターAI から使う）

## B-1. データの流れ

```
作品名 + 第〇話 + 媒体(+人格) を投入(MCP/API/CLI)
   → OCO が SQLite に保存（客観1組 + 人格ごとの主観）
   → プッシュ(webhook) で受け取る or プル(MCP/API)で取り出す
```

**入力パラメータ**: `title`(作品名・人間用キー)・`episode`(第〇話)・`media_path`
(コンテナ内・`/recordings`配下の録画TS(.ts)・動画・抽出済み .ass)・`persona_id`(既定"default")・
`persona_prompt`(任意・本文直指定/同persona_idで版管理)・`video_path`(任意・.ass 入力時の映像scene用)。

## B-2. 出力スキーマ（EpisodeBundle）

`get_episode` / webhook が返す中身。**客観（人格非依存）と主観（人格ごと）に分かれる**:

```json
{
  "series_id": "...", "title": "...", "episode": 29, "persona_id": "default",
  "transcript":   "00:21:21 （フリーレン）...",   // 整形済み生字幕(HH:MM:SS・話者付き・映像scene混合)・客観
  "cast":         "■フリーレン — ...",            // 人物相関(carry-forward)・客観
  "story":        "...",                          // ① 客観あらすじ（接地固定・人格非依存）
  "understanding":"...",                          // ② 人格レンズの物語理解
  "review": { "impression": "...", "outlook": "..." }  // ③ 感想 + 予想・期待（人格）
}
```

| 取り出し | MCP ツール | API |
|---|---|---|
| まとめ（①②③+生字幕+相関） | `oco_get_episode` | `GET /episodes/{ep}?title=&persona_id=` |
| 整形済み生字幕 | `oco_get_transcript` | `GET /episodes/{ep}/transcript` |
| 人物相関 | `oco_get_cast` | `GET /episodes/{ep}/cast` |
| ① ストーリー（客観） | `oco_get_story` | `GET /episodes/{ep}/story` |
| ② 物語理解（人格） | `oco_get_understanding` | `GET /episodes/{ep}/understanding` |
| ③ 感想+予想（人格） | `oco_get_reflection` | `GET /episodes/{ep}/reflection` |
| 横断検索 | `oco_recall` | `GET /recall?title=&keyword=&persona_id=` |
| 作品一覧 | `oco_list_series` | `GET /series` |
| 話一覧 | `oco_list_episodes` | `GET /episodes?title=` |

①と生字幕・相関は **persona引数なし＝人格非依存**（接地の独立性をIFで担保）。②③は persona 必須。

## B-3. 投入（同期/非同期/プッシュ）

**MCP**: `oco_submit_episode(title, episode, media_path, persona_id="default",
persona_prompt=None, aliases=[], series_id=None, video_path=None, webhook_url=None,
wait=True, overwrite=False)`

**API**: `POST /episodes`（同じ body）。

- `wait=true`（既定）: 処理完走して **EpisodeBundle を `result` に入れて返す**
  （`{job_id, state:"done", series_id, title, episode, persona_id, result: {...bundle...}}`）。
- `wait=false`: 即 `{job_id, state:"queued"}` を返し、`oco_get_job_status(job_id)` /
  `GET /jobs/{id}` でポーリング。
- 既処理（overwrite=false かつ①②③揃い）は再生成せず `skipped:true` で即返す。

**プッシュ**: `webhook_url` を渡す（or config `[webhook].default_url`）と、完了時（成功/失敗とも）
1回 POST。ペイロード:
```json
{ "event":"episode.processed", "job_id":"...", "state":"done",
  "series_id":"...", "title":"...", "episode":29, "persona_id":"default",
  "outputs": { "transcript":"...", "story":"...", "understanding":"...",
               "review": {"impression":"...","outlook":"..."} },
  "error": null, "ts": 0 }
```
webhook は**通知**であってプルは常時可能。配送 at-least-once＝consumer側で `job_id` 冪等処理。

## B-4. エージェント/キャラクターAI への取り込み指針（設計メモ）

- **人格分離**: consumer は自分の `persona_id` で投入する。OCO は同じ話の客観
  （transcript/cast/story）を1組、主観（understanding/impression/outlook）を persona ごとに
  別管理。別人格を差せば別の `review` が並ぶ。
- **何を consumer の記憶に流すか**:
  - `review`（感想+予想）＝ **人格の反応**＝エージェント自身の記憶へ。
  - `story`/`cast`＝客観の事実。必要なら知識として。`transcript`＝原文の根。
- **★ source タグで人格希釈を防止**: 視聴由来の記憶には source タグを付け、人格の自動学習・
  合成プロセスから除外/減衰できるようにする（大量視聴で人格が薄まるのを防ぐ）。
- **冪等**: webhook は job_id で冪等に。同一話の二重取り込みを避ける。

## B-5. 認証

既定 **無認証（LAN内信頼）**。`config [auth].token_env` に環境変数名を設定すると、API/MCP が
`Authorization: Bearer <token>` を要求する（独立トークン推奨）。

---

# C. 主要概念（理解のため）

- **字幕あり/なし**: 既定 backend は `aribcaption`＝録画TSを渡すと字幕を内部で.ass抽出する
  （字幕付き放送はこれで足りる）。字幕テキストが載っていない録画も自動でASRへ回したいなら
  `[transcript] backend = "auto"` にする＝字幕放送（ARIB）→.ass抽出、字幕なし→faster-whisper(ASR)を
  抽出できたテキスト行数で自動判別する。
- **連続もの/オムニバス**: フラグ無し。モデルが今回の台本を見て**地続きか毎回判断**する。
  連続なら前回の予想と照らす（答え合わせ）・人物相関を引き継ぐ。独立回（1話完結）なら前回を
  無理に持ち込まず、今回出ないゲストは相関から外す。
- **話者同定（②）**: `speaker_naming=true` で有効。クラスタ有（ARIB色キー）→人物相関を候補に
  LLMがクラスタ命名（呼びかけ手がかり）。クラスタ無（ASR）→テキストだけ行ごと推定。
  確信のみ・不明は付けない（部分被覆＝完璧でなくてよい）。声紋による回またぎ同一性（③）は別タスク。
- **CM（JL）**: `backend=jl` で局ロゴをNCC（明るさ不変）検出→CMレンジ。テキスト行マスク＋
  映像フレーム除外の両方に使う。NHK等CM無しや検出不安定（>40%）はCM無しにフォールバック。
- **映像 scene**: `[llm.vision]`設定＋動画があると、字幕の空白（無言で映像が語る所）を時間軸均等に
  選んでVLMが説明→`〔映像〕`として台本に時刻挿入。
- **thinking budget**: ローカル Gemma の思考を `thinking_budget` で制御（小=速い/-1=完走/0=OFF）。
  vision は思考OFF＋低解像度で高速化（挙動の詳細は `oco/llm/openai_compat.py` 冒頭コメント参照）。

# D. 既知の限界
- 話者同定は部分被覆（確信ある所だけ・ASRは~50-60%）。声優/キャラの回またぎ同一性は未実装（③）。
- ASR は CPU だと遅い（GPU があれば `[transcript] whisper_device = "cuda"`）。
- ASRの語誤りは残るが、LLMの接地＋多パスがおおむね吸収する（完璧でなくてよい設計）。
