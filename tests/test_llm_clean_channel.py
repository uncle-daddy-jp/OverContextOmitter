from oco.llm.openai_compat import _clean_channel


def test_bare_think_leak_is_dropped():
    # 思考が素の </think> 付きで content に漏れたら、手前の思考を捨てて本文だけ残す
    assert _clean_channel("5. CM: ...\nNow let me write...\n</think>\nPayPayのCMでは") == "PayPayのCMでは"
    assert _clean_channel("<think>考え中</think>本文") == "本文"


def test_plain_and_gemma_tags():
    assert _clean_channel("本文だけ") == "本文だけ"
    assert _clean_channel("<|think|>本文") == "本文"
