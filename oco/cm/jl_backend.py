"""JL（ロゴ検出ベース）CM 検出 ── 自前実装（Amatsukaze 本体/exe/.lgd は使わない）。

着想は JL（join_logo_scp + logoframe）と同じ「局ロゴの有無で CM を見分ける」。
局ロゴは番組中だけ画面隅に常時表示される**半透明オーバーレイ**で、CM 中は消える。

Amatsukaze 流の肝＝**明るさに不変な“ロゴ形状の相関”で見る**こと：
  1. 動画から低解像度グレーフレームを間引き取得（ffmpeg 1パス）。
  2. **平均フレーム**を作る（背景は動いて平均で均され、常時表示のロゴだけが残る＝
     AmatsukazeGenLogo がフレーム平均でロゴを学習するのと同発想）。その勾配＝ロゴ・テンプレ。
  3. 各フレームの勾配と**ロゴ・テンプレの正規化相関(NCC)**を測る。NCC は明るさ/コントラストに
     不変なので、**暗い番組（ロゴ有）＝パターン一致で高**、**暗いCM（ロゴ無）＝一致せず低**を
     正しく分離できる（“暗いから判定不能”という逃げを使わない＝暗いCMも捕捉）。
  4. ロゴ消失（低NCC）の連続区間 ≥ min_cm_sec を CM とする。

ロゴらしき持続エッジが平均に出ない / CM が無い（NHK 等）なら空を返す＝安全側。
返す CM レンジは テキスト行マスク と 画像フレーム除外 の両方で使われる（CM検出は一元化）。

※ より精密にしたいなら Amatsukaze の .lgd（学習済みロゴ・α込み）読込や、join_logo_scp 流の
  シーンチェンジ併用での境界整形が差し替え可能（将来のフィデリティ向上）。
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from .base import CMRange

logger = logging.getLogger("oco.cm")
_VIDEO_EXTS = {".ts", ".m2ts", ".mp4", ".mkv"}


class JLCMProvider:
    def __init__(self, cfg):
        self._cfg = cfg

    def ranges(self, media_path: str | None) -> list[CMRange]:
        if not media_path:
            return []
        if Path(media_path).suffix.lower() not in _VIDEO_EXTS:
            return []  # JL は動画が要る（.ass 等が来たら何もしない）
        import numpy as np

        cfg = self._cfg
        frames = _decode_gray(media_path, cfg.fps, cfg.frame_w, cfg.frame_h)
        if frames is None or frames.shape[0] < 10:
            return []
        return _logo_cm_ranges(frames, cfg, np)


def _decode_gray(video: str, fps: float, w: int, h: int):
    """ffmpeg 1パスで間引きグレーフレームを (T,H,W) uint8 配列に。失敗時 None。"""
    import numpy as np

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", str(video),
        "-vf", f"fps={fps},scale={w}:{h},format=gray",
        "-f", "rawvideo", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True)
    raw = proc.stdout
    frame_bytes = w * h
    n = len(raw) // frame_bytes
    if n < 10:
        return None
    return np.frombuffer(raw[: n * frame_bytes], dtype=np.uint8).reshape(n, h, w)


def _grad(arr, np):
    """|dx|+|dy| 勾配。arr: (...,H,W) float → 同形。"""
    g = np.zeros_like(arr)
    g[..., :, 1:] += np.abs(arr[..., :, 1:] - arr[..., :, :-1])
    g[..., 1:, :] += np.abs(arr[..., 1:, :] - arr[..., :-1, :])
    return g


def _logo_cm_ranges(frames, cfg, np) -> list[CMRange]:
    T, H, W = frames.shape
    f = frames.astype(np.float32)

    # ロゴ探索ゾーン＝上隅（左右）。局ロゴはほぼ画面上隅にある。
    zone = np.zeros((H, W), dtype=bool)
    rr, cc = int(H * 0.30), int(W * 0.28)
    zone[:rr, :cc] = True       # 左上
    zone[:rr, W - cc:] = True   # 右上

    # 平均フレームの勾配＝ロゴ・テンプレ（背景は平均で均され常時ロゴだけ残る）
    tmpl_full = _grad(f.mean(axis=0), np) * zone
    if tmpl_full.max() < cfg.edge_threshold:
        return []  # 平均にロゴが浮かばない → ロゴ無し → CM判定しない（安全）
    logo_mask = tmpl_full > (cfg.logo_frac * float(tmpl_full.max()))
    n_logo = int(logo_mask.sum())
    if n_logo < cfg.min_logo_px:
        return []

    # ロゴ画素でのテンプレ（ゼロ平均）
    tmpl = tmpl_full[logo_mask].astype(np.float32)
    tmpl -= tmpl.mean()
    tnorm = float(np.sqrt((tmpl ** 2).sum())) + 1e-6

    # 各フレーム勾配 × テンプレ の正規化相関(NCC)＝明るさ不変のロゴ一致度
    g = _grad(f, np)                                  # (T,H,W)
    G = g.reshape(T, -1)[:, logo_mask.reshape(-1)]    # (T, n_logo)
    G = G - G.mean(axis=1, keepdims=True)
    gnorm = np.sqrt((G ** 2).sum(axis=1)) + 1e-6
    ncc = (G @ tmpl) / (gnorm * tnorm)                # (T,)
    absent = ncc < cfg.presence_threshold             # ロゴ消失（暗CMも含め正しく低NCC）

    # ロゴ消失の連続区間 ≥ min_cm_sec を CM とする
    min_frames = max(1, int(round(cfg.min_cm_sec * cfg.fps)))
    raw_ranges: list[CMRange] = []
    i = 0
    while i < T:
        if absent[i]:
            j = i
            while j < T and absent[j]:
                j += 1
            if (j - i) >= min_frames:
                raw_ranges.append(CMRange(i / cfg.fps, j / cfg.fps))
            i = j
        else:
            i += 1
    merged = _merge(raw_ranges, cfg.merge_gap_sec)

    # CM が尺の大半を占める＝ロゴ検出が不安定（局ロゴが弱い/位置違い等）＝誤マスクの恐れ。
    # 本編セリフを大量に捨てるより、CM無しにフォールバックする方が安全（取りこぼしCMは安い）。
    total_cm = sum(r.end - r.start for r in merged)
    total_dur = T / cfg.fps
    if total_dur > 0 and total_cm > cfg.max_cm_frac * total_dur:
        logger.warning(
            "JL: CM検出が尺の %.0f%%（>%.0f%%）＝ロゴ検出が不安定と判断しCM無しにフォールバック",
            100 * total_cm / total_dur, 100 * cfg.max_cm_frac,
        )
        return []
    return merged


def _merge(ranges: list[CMRange], gap: float) -> list[CMRange]:
    """近接（gap 秒未満）のレンジを結合。"""
    if not ranges:
        return []
    ranges = sorted(ranges, key=lambda r: r.start)
    out = [ranges[0]]
    for r in ranges[1:]:
        if r.start - out[-1].end <= gap:
            out[-1] = CMRange(out[-1].start, max(out[-1].end, r.end))
        else:
            out.append(r)
    return out


class ChapterCMProvider:
    """サイドカーのチャプターファイルから CM レンジを読む軽量版（将来）。"""

    def __init__(self, cfg):
        self._cfg = cfg

    def ranges(self, media_path: str | None) -> list[CMRange]:
        if not media_path:
            return []
        media = Path(media_path)
        for ext in (".chapter", ".txt"):
            sidecar = media.with_suffix(ext)
            if sidecar.exists():
                return self._parse_chapter(sidecar.read_text(encoding="utf-8"))
        return []

    @staticmethod
    def _parse_chapter(_text: str) -> list[CMRange]:
        raise NotImplementedError(
            "チャプター形式は実サイドカーを見て実装する（録画フロー依存）"
        )
