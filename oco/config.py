"""設定読み込み。TOML + 環境変数（APIキー）。

コードはエンドポイントを知らない＝ここで provider/model/base_url を解決する。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class LLMConfig:
    provider: str               # "openai_compat" | "anthropic"
    model: str
    max_tokens: int = 12000
    base_url: str | None = None         # openai_compat のみ
    api_key: str | None = None          # 環境変数から解決済みの実キー
    # sampling（openai_compat のみ。anthropic には送らない）
    temperature: float | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None
    # 思考(thinking)制御（llama.cpp の Gemma 系 thinking 挙動・実機実測に基づく）
    #   None = 制御しない（max_tokens を総量として従来通り）
    #   0    = thinking OFF（enable_thinking=false）
    #   -1   = 完走モード（budget送らず・max_tokens=本文+headroom・漏れ無し）
    #   N>0  = ソフト上限（thinking_budget_tokens=N・max_tokens=本文+N）
    #   ※ N>0 は max_tokens を「本文ぶん」と解釈し budget を加算する
    thinking_budget: int | None = None
    completion_headroom: int = 2480     # 完走モードの本文余裕

    @classmethod
    def from_dict(cls, d: dict) -> "LLMConfig":
        api_key = None
        if env_name := d.get("api_key_env"):
            api_key = os.environ.get(env_name) or None
        return cls(
            provider=d["provider"],
            model=d["model"],
            max_tokens=d.get("max_tokens", 12000),
            base_url=d.get("base_url"),
            api_key=api_key,
            temperature=d.get("temperature"),
            frequency_penalty=d.get("frequency_penalty"),
            presence_penalty=d.get("presence_penalty"),
            thinking_budget=d.get("thinking_budget"),
            completion_headroom=d.get("completion_headroom", 2480),
        )


@dataclass
class CMConfig:
    backend: str = "none"               # "none" | "jl"（ロゴ検出・自前・引退/ロールバック用） | "chapter"（Amatsukazeチャプターsidecar読取・推奨）
    # JL（ロゴ持続エッジ）検出パラメータ
    fps: float = 0.5                    # 解析サンプリング間隔（0.5=2秒ごと）
    frame_w: int = 384
    frame_h: int = 216
    edge_threshold: float = 8.0        # 平均フレーム勾配でロゴ有無を見る最小強度
    logo_frac: float = 0.5             # ロゴ画素＝平均勾配が最大×これ以上（テンプレ画素）
    min_logo_px: int = 10              # ロゴ画素がこれ未満＝ロゴ無し→CM判定しない（安全）
    presence_threshold: float = 0.35   # フレーム勾配とロゴテンプレのNCCがこれ未満＝ロゴ消失
    min_cm_sec: float = 12.0           # この長さ以上のロゴ消失連続＝CM
    merge_gap_sec: float = 4.0         # 近接CMレンジの結合間隔
    max_cm_frac: float = 0.4           # CM検出が尺のこの割合を超えたら不安定と判断しCM無しに

    @classmethod
    def from_dict(cls, d: dict) -> "CMConfig":
        return cls(
            backend=d.get("backend", "none"),
            fps=d.get("fps", 0.5),
            frame_w=d.get("frame_w", 384),
            frame_h=d.get("frame_h", 216),
            edge_threshold=d.get("edge_threshold", 8.0),
            logo_frac=d.get("logo_frac", 0.5),
            min_logo_px=d.get("min_logo_px", 10),
            presence_threshold=d.get("presence_threshold", 0.35),
            min_cm_sec=d.get("min_cm_sec", 12.0),
            merge_gap_sec=d.get("merge_gap_sec", 4.0),
            max_cm_frac=d.get("max_cm_frac", 0.4),
        )


@dataclass
class ApiConfig:
    host: str = "0.0.0.0"
    port: int = 8910
    max_workers: int = 1                # LLM並列度。ローカルLLM1台なら1推奨(多重OOM対策)

    @classmethod
    def from_dict(cls, d: dict) -> "ApiConfig":
        return cls(host=d.get("host", "0.0.0.0"), port=d.get("port", 8910),
                   max_workers=d.get("max_workers", 1))


@dataclass
class WebhookConfig:
    default_url: str = ""               # 既定プッシュ先。リクエストの webhook_url が優先
    secret: str | None = None           # HMAC署名鍵（環境変数から解決済み）
    retry_max: int = 3

    @classmethod
    def from_dict(cls, d: dict) -> "WebhookConfig":
        secret = None
        if env := d.get("secret_env"):
            secret = os.environ.get(env) or None
        return cls(default_url=d.get("default_url", ""), secret=secret,
                   retry_max=d.get("retry_max", 3))


@dataclass
class Config:
    store_dir: Path
    text: LLMConfig
    vision: LLMConfig | None = None
    cm: CMConfig = field(default_factory=CMConfig)
    db_path: Path | None = None         # 既定 store_dir/oco.db
    block_size: int = 5                 # §4.5 階層ローリング圧縮の1ブロック話数。0=無効（全話ベタ貼り）
    condense_threshold_chars: int = 30000  # 単話transcriptがこの文字数超で圧縮（豊臣28574字=未満で無改変・美人工房40754字が対象）
    condense_chunk_chars: int = 12000      # 圧縮時1チャンク文字数（行境界分割）
    recall_fts: bool = False
    media_root: Path = Path("/recordings")  # media_path はこの配下のみ許可（トラバーサル防止）
    transcript_backend: str = "aribcaption"  # "aribcaption"(既定) | "whisper"(字幕なし・ASR) | "auto"
    speaker_naming: bool = False         # ② 話者クラスタ→キャラ名(LLM・人物相関を候補に・確信のみ)
    # Whisper(ASR) 設定（transcript_backend=whisper/auto のとき）
    whisper_model: str = "small"         # tiny/base/small/medium/large-v3（大きいほど高精度・遅い）
    whisper_device: str = "cpu"          # "cpu" | "cuda"（GPUがあれば）
    whisper_compute_type: str = "int8"   # cpu=int8 / cuda=float16 等
    whisper_language: str = "ja"
    # 映像 scene 抽出（vision 設定がある時のみ有効）
    scene_min_gap: float = 4.0           # 字幕の空白の下限秒（息継ぎ除外）
    scene_max_gap: float = 30.0          # 上限秒（超えるとCM疑いで除外）
    scene_max_frames: int = 16           # 1話で覗くフレーム予算（時間軸に均等配分・超過分はログ）
    scene_frame_width: int = 512         # VLMへ渡す縮小幅（解像度を落とすのが鉄則・高速化）
    # 意味で引く検索（[embed]）。base_url 未設定なら文字 2-gram 検索だけ
    embed_base_url: str | None = None    # llama-server（/v1/embeddings）の URL
    embed_model: str = "eg2"
    api: ApiConfig = field(default_factory=ApiConfig)
    webhook: WebhookConfig = field(default_factory=WebhookConfig)
    auth_token: str | None = None       # API/MCP の任意 bearer（既定 None=無認証・LAN内信頼）

    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> "Config":
        path = Path(path or os.environ.get("OCO_CONFIG", "config.toml"))
        if not path.exists():
            raise FileNotFoundError(
                f"設定ファイルが見つからない: {path}\n"
                "config.example.toml を config.toml にコピーして編集する。"
            )
        with open(path, "rb") as f:
            raw = tomllib.load(f)

        llm = raw.get("llm", {})
        if "text" not in llm:
            raise ValueError("config: [llm.text] が必須")
        vision = LLMConfig.from_dict(llm["vision"]) if "vision" in llm else None

        store_dir = Path(raw.get("store_dir", "/data/store"))
        db_path = Path(raw["db_path"]) if raw.get("db_path") else store_dir / "oco.db"

        auth_token = None
        if env := raw.get("auth", {}).get("token_env"):
            auth_token = os.environ.get(env) or None

        return cls(
            store_dir=store_dir,
            text=LLMConfig.from_dict(llm["text"]),
            vision=vision,
            cm=CMConfig.from_dict(raw.get("cm", {})),
            db_path=db_path,
            block_size=raw.get("core", {}).get("block_size", raw.get("block_size", 5)),
            condense_threshold_chars=raw.get("core", {}).get("condense_threshold_chars", raw.get("condense_threshold_chars", 30000)),
            condense_chunk_chars=raw.get("core", {}).get("condense_chunk_chars", raw.get("condense_chunk_chars", 12000)),
            recall_fts=raw.get("recall_fts", False),
            media_root=Path(raw.get("media_root", "/recordings")),
            transcript_backend=raw.get("transcript", {}).get("backend", "aribcaption"),
            speaker_naming=raw.get("speaker_naming", False),
            whisper_model=raw.get("transcript", {}).get("whisper_model", "small"),
            whisper_device=raw.get("transcript", {}).get("whisper_device", "cpu"),
            whisper_compute_type=raw.get("transcript", {}).get("whisper_compute_type", "int8"),
            whisper_language=raw.get("transcript", {}).get("whisper_language", "ja"),
            scene_min_gap=raw.get("scene", {}).get("min_gap", 4.0),
            scene_max_gap=raw.get("scene", {}).get("max_gap", 30.0),
            scene_max_frames=raw.get("scene", {}).get("max_frames", 12),
            scene_frame_width=raw.get("scene", {}).get("frame_width", 768),
            embed_base_url=raw.get("embed", {}).get("base_url") or None,
            embed_model=raw.get("embed", {}).get("model", "eg2"),
            api=ApiConfig.from_dict(raw.get("api", {})),
            webhook=WebhookConfig.from_dict(raw.get("webhook", {})),
            auth_token=auth_token,
        )
