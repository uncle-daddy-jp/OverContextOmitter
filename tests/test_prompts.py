from oco.prompts import render_transcript


def test_render_hhmmss_scene_and_color_key():
    recs = [
        {"t": 1281, "speaker": "フェルン", "text": "行きましょう", "kind": "line"},
        {"t": 1290, "text": "少女が立っている", "kind": "scene"},
        {"t": 5, "speaker": "#00FFFF", "text": "色キーは前置しない", "kind": "line"},
    ]
    out = render_transcript(recs).splitlines()
    assert out[0] == "00:21:21 （フェルン）行きましょう"   # 名前は前置
    assert out[1] == "00:21:30 〔映像〕少女が立っている"   # scene
    assert out[2] == "00:00:05 色キーは前置しない"          # #色 は前置しない
