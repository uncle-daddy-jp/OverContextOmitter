"""埋め込み（意味で引く検索用）。llama-server の /v1/embeddings（OpenAI 互換）を呼ぶ。

モデルは EmbeddingGemma 2。文書と問いで決まった接頭辞を付ける（モデルの規定）。
返すベクトルは L2 正規化済み float32（内積＝cos 類似）。
"""

from __future__ import annotations

import logging

import httpx
import numpy as np

log = logging.getLogger(__name__)

_DOC_PREFIX = "title: none | text: "
_QUERY_PREFIX = "task: search result | query: "
_BATCH = 64
# サーバーは 1 本あたり 512 トークンまで（-c 2048 / --parallel 4）。日本語 500 字で約 400 トークン。
# 実データの 30 秒窓は最長 442 字なので普段は切れない（念のための上限）。
_MAX_CHARS = 500


def _post(cfg, inputs: list[str], timeout: float) -> np.ndarray:
    url = cfg.embed_base_url.rstrip("/") + "/v1/embeddings"
    r = httpx.post(url, json={"model": cfg.embed_model, "input": inputs}, timeout=timeout)
    r.raise_for_status()
    data = sorted(r.json()["data"], key=lambda d: d["index"])
    v = np.asarray([d["embedding"] for d in data], dtype=np.float32)
    return v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-12)


def embed_documents(cfg, texts: list[str]) -> np.ndarray:
    """文書をまとめてベクトル化（64 件ずつ送る）。形は (件数, 次元)。"""
    out = [_post(cfg, [_DOC_PREFIX + t[:_MAX_CHARS] for t in texts[i:i + _BATCH]], 120.0)
           for i in range(0, len(texts), _BATCH)]
    return np.concatenate(out) if out else np.zeros((0, 0), dtype=np.float32)


def embed_query(cfg, text: str) -> np.ndarray:
    """問い 1 本をベクトル化。形は (次元,)。"""
    return _post(cfg, [_QUERY_PREFIX + text[:_MAX_CHARS]], 10.0)[0]


def embed_episode(cfg, store, ep: int) -> int:
    """その話の文書（30 秒窓・scene 行・あらすじ文）をベクトル化して保存。書いた件数を返す。"""
    docs = store.vector_docs_for_episode(ep)
    vecs = embed_documents(cfg, [d["doc_text"] for d in docs])
    store.write_vectors(ep, docs, vecs, cfg.embed_model)
    return len(docs)
