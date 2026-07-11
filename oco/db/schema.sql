-- OverContextOmitter SQLite スキーマ（1ファイル1DB・グラフ無し・FTS既定OFF）
-- 接続初期化(db/__init__.py connect)で必ず:
--   PRAGMA journal_mode=WAL; busy_timeout=5000; synchronous=NORMAL; foreign_keys=ON;

CREATE TABLE IF NOT EXISTS oco_series (
  series_id  TEXT PRIMARY KEY,             -- 内部スラッグ（URLセーフ）
  title      TEXT NOT NULL UNIQUE,         -- 作品名（表示名・要件5/6）
  aliases    TEXT NOT NULL DEFAULT '[]',   -- JSON配列（別名・解決補助）
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS oco_episodes (   -- 客観（persona非依存・1行/話）
  series_id            TEXT NOT NULL REFERENCES oco_series(series_id) ON DELETE CASCADE,
  episode_number       INTEGER NOT NULL,    -- 第〇話（要件5/6）
  episode_title        TEXT,
  episode_transcript_text TEXT,             -- 要件7: 整形済み生字幕(HH:MM:SS・line/scene混合)
  cast_roster_text     TEXT,                -- 人物相関(carry-forward・客観・取り違え防止の接地)
  story_objective_text TEXT,                -- ① 客観あらすじ（接地固定・捏造禁止）
  story_model          TEXT,                -- 生成モデル名（provenance）
  n_lines              INTEGER,
  n_masked             INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (series_id, episode_number)
);

CREATE TABLE IF NOT EXISTS oco_lines (      -- 全エントリ（recallの素・整形済み生字幕の源）
  series_id      TEXT NOT NULL,
  episode_number INTEGER NOT NULL,
  line_seq       INTEGER NOT NULL,          -- 話内通し番号（CMマスク後の時系列）
  line_kind      TEXT NOT NULL DEFAULT 'line', -- 'line'(字幕) | 'scene'(VLM映像説明・後挿入)
  line_start_sec REAL NOT NULL,
  line_end_sec   REAL NOT NULL,
  line_speaker   TEXT,                       -- 色/名前、不明NULL（sceneはNULL）
  line_text      TEXT NOT NULL,
  PRIMARY KEY (series_id, episode_number, line_seq),
  FOREIGN KEY (series_id, episode_number)
    REFERENCES oco_episodes(series_id, episode_number) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_lines_ep ON oco_lines(series_id, episode_number);

CREATE TABLE IF NOT EXISTS oco_persona_prompts (  -- 人格プロンプト本文（内容ハッシュ世代管理）
  persona_prompt_id INTEGER PRIMARY KEY AUTOINCREMENT,
  persona_id    TEXT NOT NULL,
  prompt_sha256 TEXT NOT NULL,
  prompt_text   TEXT NOT NULL,
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (persona_id, prompt_sha256)
);

CREATE TABLE IF NOT EXISTS oco_persona_views (    -- 主観（横持ち・1行/(作品,話,人格)）
  series_id      TEXT NOT NULL,
  episode_number INTEGER NOT NULL,
  persona_id     TEXT NOT NULL,
  story_understanding_text TEXT,             -- ② 人格レンズの物語理解
  impression_reaction_text TEXT,             -- ③ 感想
  outlook_prediction_text  TEXT,             -- ③ 予想・期待
  persona_prompt_id INTEGER REFERENCES oco_persona_prompts(persona_prompt_id),
  view_model     TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (series_id, episode_number, persona_id),
  FOREIGN KEY (series_id, episode_number)
    REFERENCES oco_episodes(series_id, episode_number) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_views_persona ON oco_persona_views(series_id, persona_id);

CREATE TABLE IF NOT EXISTS oco_blocks (     -- §4.5 客観ブロック要約（persona非依存・圧縮）
  series_id   TEXT NOT NULL REFERENCES oco_series(series_id) ON DELETE CASCADE,
  block_index INTEGER NOT NULL,              -- 通し番号（= ep_hi / block_size）
  ep_lo       INTEGER NOT NULL,
  ep_hi       INTEGER NOT NULL,
  block_size  INTEGER NOT NULL,
  block_story_text TEXT,                     -- 客観ブロックあらすじ（未回収の伏線=開いた糸を保持）
  block_model TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (series_id, block_index)
);

CREATE TABLE IF NOT EXISTS oco_block_views ( -- §4.5 主観ブロックダイジェスト（persona別）
  series_id   TEXT NOT NULL,
  block_index INTEGER NOT NULL,
  persona_id  TEXT NOT NULL,
  block_digest_text TEXT,                     -- 心情＋未解決の予想・期待を持ち越し
  persona_prompt_id INTEGER REFERENCES oco_persona_prompts(persona_prompt_id),
  view_model  TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (series_id, block_index, persona_id),
  FOREIGN KEY (series_id, block_index)
    REFERENCES oco_blocks(series_id, block_index) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS oco_jobs (       -- 非同期ジョブ（同一DB同居）
  job_id         TEXT PRIMARY KEY,
  series_id      TEXT,
  episode_number INTEGER,
  persona_id     TEXT,
  state          TEXT NOT NULL,             -- queued|running|done|error
  phase          TEXT,                      -- parse|cm_mask|story|understanding|impression|outlook|webhook|done
  error          TEXT,
  result_json    TEXT,                      -- 完了時の EpisodeBundle(JSON)
  webhook_url    TEXT,
  webhook_state  TEXT,                      -- NULL|sent|failed
  created_at     TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- FTS は任意（既定OFF）。日本語短語は trigram 指定時のみ意味あり:
-- CREATE VIRTUAL TABLE oco_lines_fts USING fts5(line_text, content='oco_lines', tokenize='trigram');
