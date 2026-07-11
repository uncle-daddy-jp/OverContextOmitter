"""映像＝時間グリッドでなく『字幕の空白＝無言で映像が物語る所』を優先して覗く。

frames.py … ffmpeg でフレーム抽出 / 字幕ギャップからフレーム時刻を選抜
enrich.py … 選抜フレームを VLM に説明させ scene 行（line_kind='scene'）にする
"""
