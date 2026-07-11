# OverContextOmitter — 番組理解コア
#
# python + ffmpeg(libaribcaption=ARIB字幕デコード) を同梱。
# ホストに python も ffmpeg も要らない（全部コンテナに閉じる＝配布も容易）。
#
# ffmpeg の libaribcaption は 6.1+ で本体マージ済。Debian trixie(13) の ffmpeg 7.x を使う。
FROM python:3.13-slim-trixie

# ffmpeg（ARIB字幕→.ass 抽出に必要）。Debian の ffmpeg は libaribcaption 非搭載なので、
# libaribcaption 入りの BtbN 静的ビルドを使う（arib_caption デコーダが要る）。
# CM検出のJLツールチェーンは別途（cm/jl_backend.py 参照）。
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl xz-utils ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fL -o /tmp/ffmpeg.tar.xz \
        https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz \
    && mkdir -p /tmp/ff && tar -xf /tmp/ffmpeg.tar.xz -C /tmp/ff --strip-components=1 \
    && cp /tmp/ff/bin/ffmpeg /tmp/ff/bin/ffprobe /usr/local/bin/ \
    && rm -rf /tmp/ff /tmp/ffmpeg.tar.xz \
    && ffmpeg -hide_banner -decoders 2>/dev/null | grep -i arib

# 依存解決は uv（高速・ロック可）
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml README.md ./
COPY oco ./oco
# 開発時は -e で編集即反映（compose で oco/ をマウント）。pytest も入れておく。
RUN uv pip install --system -e . pytest

# 既定設定の置き場所（compose で config.toml を上書きマウント）
ENV OCO_CONFIG=/app/config.toml

ENTRYPOINT ["oco"]
CMD ["--help"]
