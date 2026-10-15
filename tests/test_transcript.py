from oco.config import Config, LLMConfig
from oco.subtitles.ass_parser import Line
from oco.transcript import build_transcript_source
from oco.transcript.aribcaption import AribCaptionSource
from oco.transcript.auto import AutoSource
from oco.transcript.whisper import WhisperSource


def _cfg(tmp_path, backend):
    return Config(store_dir=tmp_path,
                  text=LLMConfig(provider="openai_compat", model="x"),
                  transcript_backend=backend)


def _patch_auto(monkeypatch, *, has_stream, arib_result):
    """auto.py が遅延 import する 3 依存を差し替える。arib_result は list か例外。

    whisper は呼ばれたら固定 1 行を返す（フォールバック検証用のセンチネル）。
    """
    calls = {"whisper": 0}
    monkeypatch.setattr("oco.subtitles.extract.probe_subtitle_streams",
                        lambda _p: ("Subtitle: arib_caption\n" if has_stream else "no subs"))

    class _Arib:
        def lines(self, _p):
            if isinstance(arib_result, Exception):
                raise arib_result
            return arib_result

    class _Whisper:
        def __init__(self, _cfg):
            pass

        def lines(self, _p):
            calls["whisper"] += 1
            return [Line(start=0.0, end=1.0, text="ASRだよ", speaker=None)]

    monkeypatch.setattr("oco.transcript.aribcaption.AribCaptionSource", _Arib)
    monkeypatch.setattr("oco.transcript.whisper.WhisperSource", _Whisper)
    return calls


def test_backend_dispatch(tmp_path):
    assert isinstance(build_transcript_source(_cfg(tmp_path, "aribcaption")), AribCaptionSource)
    assert isinstance(build_transcript_source(_cfg(tmp_path, "whisper")), WhisperSource)
    assert isinstance(build_transcript_source(_cfg(tmp_path, "auto")), AutoSource)


def test_aribcaption_reads_ass(tmp_path):
    p = tmp_path / "x.ass"
    p.write_text(
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,こんにちは\n",
        encoding="utf-8",
    )
    lines = AribCaptionSource().lines(str(p))
    assert len(lines) == 1
    assert lines[0].text == "こんにちは"
    assert lines[0].kind == "line"


def test_auto_uses_aribcaption_when_captions_have_text(tmp_path, monkeypatch):
    calls = _patch_auto(monkeypatch, has_stream=True,
                        arib_result=[Line(start=0.0, end=1.0, text="字幕あり", speaker=None)])
    out = AutoSource(_cfg(tmp_path, "auto")).lines("/x/ep.ts")
    assert [l.text for l in out] == ["字幕あり"]
    assert calls["whisper"] == 0  # 字幕テキストが取れたら ASR は走らせない


def test_auto_falls_back_to_whisper_when_caption_stream_is_empty(tmp_path, monkeypatch):
    # 字幕ストリームはあるが本文0行（局が字幕テキストを載せず管理パケットのみ）→ ASR に落ちる。
    calls = _patch_auto(monkeypatch, has_stream=True, arib_result=[])
    out = AutoSource(_cfg(tmp_path, "auto")).lines("/x/ep.ts")
    assert [l.text for l in out] == ["ASRだよ"]
    assert calls["whisper"] == 1


def test_auto_falls_back_to_whisper_when_extraction_raises(tmp_path, monkeypatch):
    calls = _patch_auto(monkeypatch, has_stream=True, arib_result=RuntimeError("ffmpeg 死"))
    out = AutoSource(_cfg(tmp_path, "auto")).lines("/x/ep.ts")
    assert [l.text for l in out] == ["ASRだよ"]
    assert calls["whisper"] == 1


def test_auto_falls_back_to_whisper_when_captions_are_only_in_cm(tmp_path, monkeypatch):
    # 字幕が CM 区間の中にしか無い（番宣だけ字幕付き）→ 本編の字幕は無い扱いで ASR に落ちる
    from oco.cm import CMRange

    calls = _patch_auto(monkeypatch, has_stream=True,
                        arib_result=[Line(start=302.0, end=304.0, text="番宣", speaker=None)])

    class _CM:
        def ranges(self, _p):
            return [CMRange(start=274.0, end=307.0)]

    monkeypatch.setattr("oco.cm.build_cm_provider", lambda _c: _CM())
    out = AutoSource(_cfg(tmp_path, "auto")).lines("/x/ep.ts")
    assert [l.text for l in out] == ["ASRだよ"]
    assert calls["whisper"] == 1


def test_auto_uses_whisper_when_no_subtitle_stream(tmp_path, monkeypatch):
    calls = _patch_auto(monkeypatch, has_stream=False, arib_result=[])
    out = AutoSource(_cfg(tmp_path, "auto")).lines("/x/ep.ts")
    assert [l.text for l in out] == ["ASRだよ"]
    assert calls["whisper"] == 1


def test_whisper_extract_audio_success(tmp_path, monkeypatch):
    # ffmpeg 成功（十分なサイズの wav を書く）→ (temp_wav, True) を返す。
    import subprocess as _sp

    def fake_run(cmd, **kw):
        out = cmd[-1]  # 抽出先 wav パス
        with open(out, "wb") as f:
            f.write(b"\0" * 4096)  # >1024 bytes = 成功扱い
        return None

    monkeypatch.setattr(_sp, "run", fake_run)
    path, is_temp = WhisperSource._extract_audio("/recordings/ep.ts")
    assert is_temp is True
    assert path.endswith(".wav")
    import os
    assert os.path.exists(path)
    os.remove(path)


def test_whisper_extract_audio_falls_back_on_ffmpeg_failure(tmp_path, monkeypatch):
    # ffmpeg 失敗 → 元メディアパスを (media, False) で返し、temp は残さない。
    import subprocess as _sp

    def boom(cmd, **kw):
        raise _sp.CalledProcessError(1, cmd)

    monkeypatch.setattr(_sp, "run", boom)
    path, is_temp = WhisperSource._extract_audio("/recordings/ep.ts")
    assert (path, is_temp) == ("/recordings/ep.ts", False)
