# OverContextOmitter

録画した番組や動画を LLM に「見せて」、内容を理解させるモジュールです。
人格プロンプトを差し込むと、そのAI人格ならではの感想・予想・期待が生まれます。特定のエージェントに依存しない、汎用の **TV番組理解モジュール** です。

理解した内容はそのまま SQLite に保存されるので、取り出して好きな用途に使えます。

- **連続ものに対応** — 過去話のあらすじと感想を踏まえて、続きの話として理解します。前回の予想との「答え合わせ」もします。次回予想を出すのは連続ものだけで、単発（映画・1話完結・情報番組など）では出しません。
- **簡単な人物同定** — 誰のセリフか・名前は何か、を確信できる範囲で推定し、人物相関として毎話引き継ぎます。
- **細部の呼び出し** — 文字列検索（`recall`）で過去話の該当箇所を引けます。第2話で張られた伏線を第6話の視聴時に拾う、といった使い方もできます（本モジュールをどう使うか次第です）。
  セリフや場面の検索（`/lines/search`）は、文字の一致だけでなく「兵士が塹壕に隠れている場面」のような言い回しでも引けます。これは任意の機能で、埋め込みサーバー（`oco-embed`）を立てたときだけ有効です。立てなければ今までどおり文字の一致で探します。
- **映像も少し見る** — セリフだけでなく、無言のシーンを画像から把握して理解に反映します（現在は軽量実装）。

おことわり：「OverContextOmitter」という名前のとおり、**オミット（省略）します**。正確な物語理解を保証するものではなく、人間にもありそうな誤解や解釈違いをします。ゆるい使い方が向いています。

> **使い方の詳細 → [docs/USAGE.md](docs/USAGE.md)**（セットアップ・CLI/API/MCP・出力スキーマ・エージェント連携の指針）

## しくみ

```
録画データ(.ts)＋作品名＋第〇話（＋人格プロンプト）
   │  MCP / HTTP API / CLI のどれかで投入
   ▼
OverContextOmitter   ── LLM 7回前後 ──▶  SQLite(oco.db) に保存
   人物相関 → ①ストーリー(→検証) → ②物語理解 → ③感想 → ③予想（連続ものだけ）
   ＋ 整形済み生字幕（HH:MM:SS 付き・映像説明も時刻の位置に挿入）
   │
   ├─ プッシュ: 処理完了時に webhook へ POST
   └─ プル:    MCP / API / CLI で「作品名 第〇話」を指定して取り出し
```

## 用意するもの

- **自分で録画した .ts ファイル**が必要です（VOD の再生画面などからは使えません）。録画データから字幕を取り出して台本にします。
- 字幕が入っていない録画は、設定（`[transcript] backend = "auto"`）で音声認識（faster-whisper）による字幕作成にフォールバックできます。
- 録画の方法はいろいろありますが、EPGTimer（EDCB）での録画が楽だと思います。使い方・設定は検索するなどしてお探しください。
- あくまで自分で録画したものにお使いください。コピーされたデータを読ませるなどの利用はしないでください。

## 取り出せるもの

| 取り出し | 内容 | persona |
|---|---|---|
| 整形済み生字幕 | `HH:MM:SS` 付き台本（字幕＋映像説明） | 不要（客観） |
| 人物相関 | 登場人物と関係（毎話更新・前話から引き継ぎ） | 不要（客観） |
| ① ストーリー | 客観あらすじ「何が起きたか」 | 不要（客観） |
| ② 物語理解 | 人格レンズを通した“理解の語り”（注目点・重みづけ） | 必須 |
| ③ 感想・予想 | 感想 ＋ 次回への予想・期待（予想は連続ものだけ） | 必須 |

生字幕・人物相関・①は人格に依存しません（客観）。②③は人格込みです。
人物相関は「確信できたところだけ」の簡易な同定で、正確な相関図ではありません。

## Docker クイックスタート

```bash
cp config.example.toml config.toml   # provider/model/base_url を編集
docker compose build

# テスト（ネット不要・spine検証）
docker compose --profile test run --rm oco-test

# 1話を処理（CLI 都度run）。録画TSを渡すと字幕を内部で抽出し、
# ①②③＋生字幕＋人物相関が SQLite に溜まる
docker compose run --rm oco process --title "葬送のフリーレン" --ep 11 --media /recordings/ep11.ts

# 作品名＋話数で取り出し / キーワード検索 / 一覧
docker compose run --rm oco get --title "葬送のフリーレン" --ep 11 --transcript
docker compose run --rm oco recall --title "葬送のフリーレン" 一級魔法使い
docker compose run --rm oco list

# 常駐 API（:8910）
docker compose up -d oco-api
curl "http://localhost:8910/episodes/11?title=葬送のフリーレン&persona_id=default"
curl -X POST localhost:8910/episodes -H 'content-type: application/json' \
  -d '{"title":"葬送のフリーレン","episode":11,"media_path":"/recordings/ep11.ts"}'

# MCP（クライアントが stdio で spawn）
docker compose run --rm -T oco-mcp

# 意味で引く検索（任意・モデルの置き方は docs/USAGE.md の A-1）
docker compose up -d oco-embed
docker compose run --rm oco embed --all   # 処理済みの過去の話を埋め戻す
```

## 意味で引く検索（任意）

「あのセリフ何だっけ」「兵士が塹壕に隠れている場面あったよね」のような、うろ覚えの言い方で過去の話を引く機能です。
文字の一致（既定）に加えて、埋め込みサーバーを立てると **言い回しが違っても意味が近ければ当たる** ようになります。

```
取り込み（1話ごと）                      検索（/lines/search）
  セリフ行 ─┐ 30秒ごとにまとめる             問い ─→ 同じモデルでベクトル化
  場面の説明 ┼→ 文書 ─→ EmbeddingGemma 2 ─→ SQLite          │
  あらすじ文 ┘            （oco-embed）      oco_vectors ←─ cos 類似で近い順
```

- **何をベクトルにするか**: セリフは 30 秒ごとにまとめた文書、映像の場面説明（scene 行）、客観あらすじの 1 文。
  セリフ 1 行（平均 10 字）は意味を持たないので単体では入れません。
- **逐語の引用は今までどおり**: 問いがセリフにそのまま含まれるときや文字の一致が強いときは、文字検索の結果をそのまま返します。
  意味検索が効くのは「言い方が違う」問いのときです。
- **外れは「見つからない」と返す**: 文字がかぶっただけのセリフ（「呼吸が荒い」→「使いが荒い」）は `found=false` になり、
  参考として近い候補だけを `low_confidence` 付きで返します。返り値の形は従来と同じで、`"search": "semantic"` か `"lexical"` が 1 項目増えます。
- **モデル**: [EmbeddingGemma 2](https://huggingface.co/ggml-org/embeddinggemma-2-GGUF)（Google DeepMind・Apache 2.0・768 次元）。
  文字だけ使うので 310MB の GGUF を CPU で動かせます（メモリ約 400MB）。
- **かかる時間の目安**: 取り込みは 1 話あたり数秒〜10 秒増えます。過去の話の埋め戻しは 1 話 10 秒前後（250 話で約 30 分）。検索 1 回は 0.4 秒ほど増えます。

有効にする手順（詳しくは [docs/USAGE.md](docs/USAGE.md) の A-1）:

```bash
# 1. モデルを置く（./models/ は git 管理外）
#    https://huggingface.co/ggml-org/embeddinggemma-2-GGUF から embeddinggemma-2-Q8_0.gguf
# 2. 埋め込みサーバーを起動（llama.cpp の server イメージ・CPU）
docker compose up -d oco-embed
# 3. config.toml に書いて oco-api を再起動
#    [embed]
#    base_url = "http://oco-embed:8080"
#    model = "eg2"
docker compose restart oco-api
# 4. 処理済みの話を埋め戻す（新しい話は取り込み時に自動で入る）
docker compose run --rm oco embed --all
```

`[embed]` を書かなければ何も変わりません。埋め込みサーバーが落ちているときも文字検索で返します。

## 外部インターフェース

- **MCP**（`oco-mcp`・stdio・FastMCP）: `oco_submit_episode` / `oco_get_episode` /
  `oco_get_transcript` / `oco_get_cast` / `oco_get_story` / `oco_get_understanding` /
  `oco_get_reflection` / `oco_recall` / `oco_search_lines` / `oco_list_series` / `oco_list_episodes` /
  `oco_get_job_status` / `oco_list_blocks` / `oco_get_block`
- **HTTP API**（`oco-api`・:8910・FastAPI）:
  `POST /episodes`（投入）、`GET /episodes/{ep}`（①②③まとめ）、
  `GET /episodes/{ep}/{transcript,cast,story,understanding,reflection}`（個別）、
  `GET /recall`（検索）、`GET /lines/search`（セリフ・場面のあいまい検索・意味検索対応）、`GET /series`・`GET /episodes?title=`（一覧）、`GET /jobs/{id}`（非同期の状況）、
  `GET /blocks`・`GET /blocks/{i}`・`GET /episodes/{ep}/block`（長編の圧縮ブロック）、`GET /healthz`
- **プッシュ**: 処理完了時に webhook へ、取り出しと同一スキーマ＋jobメタを POST
  （config の既定URL or リクエストごとの指定）

## 設計・運用メモ

- **SQLite は1ファイル1DB**（`oco.db`・WAL）。store/jobs 同居。**named volume 必須**
  （Windows の bind mount だと WAL/ロックが壊れることがあります）。
- **同期（wait=true）が既定**。非同期（job_id＋ポーリング）はオプトイン。`oco-api` は **--workers 1** 固定。
- 認証は既定で**無し（LAN内信頼）**。config の `[auth].token_env` を設定すると bearer 必須になります。
- LLM は**プロバイダ抽象**（`openai_compat`＝ローカル/OpenAI互換、`anthropic`＝Claude）。
  毎話の安定プレフィックス（全あらすじ＋感想）が prompt cache に乗るため、話数が増えてもコストは線形に伸びません。
- LLM 呼び出しは基本 **1話あたり5回**（人物相関・①②③）。話者同定・映像シーン・長編の圧縮封印を
  有効にした場合は、その分の呼び出しが加わります。

### まだ（次フェーズ）

- 映像 `look(時刻, 質問)`（任意の時刻を VLM に追加質問できる対話ツール）
- 声紋による話者の回またぎ同一性（現状の話者同定はテキスト手がかりの部分被覆）

## 作者のX
https://x.com/uncle_dadd9
