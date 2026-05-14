from app.services.vad import VADSegmenter


def test_vad_emits_voiced_segment():
    vad = VADSegmenter(rms_threshold=10, silence_ms=60, min_voice_ms=30)
    voice_frame = (1000).to_bytes(2, "little", signed=True) * 480
    silence_frame = (0).to_bytes(2, "little", signed=True) * 480

    emitted = []
    emitted.extend(vad.push(voice_frame * 3))
    emitted.extend(vad.push(silence_frame * 3))

    assert len(emitted) == 1
    assert emitted[0].end_seconds > emitted[0].start_seconds


def test_vad_emits_multiple_voiced_segments():
    vad = VADSegmenter(rms_threshold=10, silence_ms=60, min_voice_ms=30)
    voice_frame = (1000).to_bytes(2, "little", signed=True) * 480
    silence_frame = (0).to_bytes(2, "little", signed=True) * 480

    emitted = []
    emitted.extend(vad.push(voice_frame * 3))
    emitted.extend(vad.push(silence_frame * 3))
    emitted.extend(vad.push(voice_frame * 3))
    emitted.extend(vad.push(silence_frame * 3))

    assert len(emitted) == 2
