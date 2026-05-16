from types import SimpleNamespace

from app.services.asr import filter_asr_hotwords, is_garbage_transcript, is_repetitive_hallucination, should_drop_segment


def test_repetitive_hallucination_filter():
    assert is_repetitive_hallucination("忙忙忙忙忙忙忙忙忙忙忙忙忙忙忙忙") is True
    assert is_repetitive_hallucination("이것도 있어야 좋을 것 같은데") is False


def test_tiny_korean_garbage_filter():
    text = "łamboy!!!!!!!!!!!!!ㅠㅠ comparison trillions, 茶, 雜誘, 青 summit, connect, 飲 number"
    assert is_garbage_transcript(text, "ko") is True
    assert is_garbage_transcript("来た!", "ko") is True
    assert is_garbage_transcript("お疲れ様でした", "ko") is True
    assert is_garbage_transcript("안녕 こんにちは", "ko") is True
    assert is_garbage_transcript("高等精灵, 天洞", "ko") is True
    assert is_garbage_transcript("頑睜は僕寝た、船ら、宜しいにね", "ko") is True
    assert is_garbage_transcript("이것도 있어야 좋을 것 같은데", "ko") is False


def test_low_confidence_segment_filter():
    assert should_drop_segment("hello", SimpleNamespace(no_speech_prob=0.96, avg_logprob=-0.1)) is True
    assert should_drop_segment("hello", SimpleNamespace(no_speech_prob=0.1, avg_logprob=-1.4)) is False
    assert should_drop_segment("hello", SimpleNamespace(no_speech_prob=0.1, avg_logprob=-0.1, compression_ratio=4.5)) is True
    assert should_drop_segment("안녕", SimpleNamespace(no_speech_prob=0.1, avg_logprob=-0.2)) is False
    assert should_drop_segment("高等精灵", SimpleNamespace(no_speech_prob=0.1, avg_logprob=-0.2), "ko") is True


def test_korean_asr_hotwords_skip_chinese_terms():
    assert filter_asr_hotwords(["奇波", "蓝色星原", "키보"], "ko") == ["키보"]
    assert filter_asr_hotwords(["Azur Promilia", "奇波"], None) == ["Azur Promilia", "奇波"]
