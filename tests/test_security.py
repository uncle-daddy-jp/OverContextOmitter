import pytest

from oco.config import Config, LLMConfig
from oco.personas import load_persona
from oco.service import InvalidMediaPath, OcoService


def _cfg(tmp_path) -> Config:
    return Config(
        store_dir=tmp_path,
        text=LLMConfig(provider="openai_compat", model="stub", base_url="http://x/v1"),
        db_path=tmp_path / "oco.db",
        media_root=tmp_path / "recordings",
    )


def test_media_path_must_be_under_root_and_ass(tmp_path):
    (tmp_path / "recordings").mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET", encoding="utf-8")
    svc = OcoService(_cfg(tmp_path))

    # 許可ルート外（任意ファイル読み）→ 拒否
    with pytest.raises(InvalidMediaPath):
        svc.submit(title="W", episode=1, media_path=str(secret), wait=True)
    # トラバーサルで外へ → 拒否
    with pytest.raises(InvalidMediaPath):
        svc.submit(title="W", episode=1,
                   media_path=str(tmp_path / "recordings" / ".." / "secret.txt"), wait=True)
    # ルート内でも .ass 以外 → 拒否
    bad = tmp_path / "recordings" / "ep1.srt"
    bad.write_text("x", encoding="utf-8")
    with pytest.raises(InvalidMediaPath):
        svc.submit(title="W", episode=1, media_path=str(bad), wait=True)
    # ルート内 .ass でも不在 → 拒否
    with pytest.raises(InvalidMediaPath):
        svc.submit(title="W", episode=1,
                   media_path=str(tmp_path / "recordings" / "missing.ass"), wait=True)


def test_persona_id_is_slug_only_no_path_read(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("SECRET", encoding="utf-8")
    # パスや区切りを含む persona_id は拒否（任意 .txt 読みを塞ぐ）
    with pytest.raises(ValueError):
        load_persona(str(secret))
    with pytest.raises(ValueError):
        load_persona("../../etc/passwd")
    # 同梱 slug は読める
    assert load_persona("default")
