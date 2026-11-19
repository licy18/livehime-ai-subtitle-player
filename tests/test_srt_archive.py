from app.services.srt_archive import RecordingSubtitles


def test_archive_uses_recording_origin_and_sorts_each_track(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.srt_archive.recording_video_offset", lambda _: 0.05)
    archive = RecordingSubtitles("stream", tmp_path / "program.ts")
    archive.first_hls_video_pts = 8.125

    archive.add_source("second", 11, 12, "次の台詞")
    archive.add_source("first", 8.04, 10, "最初の台詞")
    archive.add_translation("second", "接下来一句")
    archive.add_translation("first", "第一句")
    archive.add_translation("not-from-recording", "测试字幕")
    archive.flush()

    ja = tmp_path / "program.ja.srt"
    zh = tmp_path / "program.zh-CN.srt"
    assert ja.read_text(encoding="utf-8") == (
        "1\n00:00:00,000 --> 00:00:01,925\n最初の台詞\n\n"
        "2\n00:00:02,925 --> 00:00:03,925\n次の台詞\n\n"
    )
    assert zh.read_text(encoding="utf-8") == (
        "1\n00:00:00,000 --> 00:00:01,925\n第一句\n\n"
        "2\n00:00:02,925 --> 00:00:03,925\n接下来一句\n\n"
    )

    zh.write_text("人工精修", encoding="utf-8")
    archive.flush()
    assert zh.read_text(encoding="utf-8") == "人工精修"
