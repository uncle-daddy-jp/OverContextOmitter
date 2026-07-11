"""話者同定（②）: 無名の話者クラスタ → キャラ名。

クラスタの素は backend 非依存:
- 字幕あり(ARIB): 字幕の色キー(#RRGGBB)がそのまま1話内の一貫クラスタ。
- 字幕なし(ASR): pyannote 等の diarization 区間を時間重なりで割り当てたクラスタ（①）。

命名は人物相関(cast)を候補名簿に、ローカルLLMが呼びかけ/自己紹介/文脈で
『確信できるクラスタだけ』名付ける（部分被覆・確信優先＝完璧でなくてよい設計）。
"""

from .naming import apply_names, attribute_speakers, name_speakers

__all__ = ["attribute_speakers", "name_speakers", "apply_names"]
