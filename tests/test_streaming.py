from app.services.hls_media import read_media_playlist
from app.services.streaming import normalize_stream_page_url, parse_streamlink_qualities


def test_normalize_chzzk_channel_page_url_to_live_url():
    assert (
        normalize_stream_page_url("https://chzzk.naver.com/1ab3db7c25c6362885ff9c9e2df3ab41")
        == "https://chzzk.naver.com/live/1ab3db7c25c6362885ff9c9e2df3ab41"
    )


def test_normalize_stream_page_url_keeps_other_urls():
    url = "https://example.com/live/test.m3u8"

    assert normalize_stream_page_url(url) == url


def test_parse_streamlink_qualities_orders_streams():
    payload = {"streams": {"best": {}, "480p": {}, "144p": {}, "worst": {}, "720p60": {}}}

    assert parse_streamlink_qualities(payload) == ["worst", "144p", "480p", "720p60", "best"]


def test_hls_playlist_keeps_sliding_sequence_and_media_dates(tmp_path):
    index = tmp_path / "index.m3u8"
    index.write_text(
        "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:41\n"
        "#EXTINF:1.250,\n#EXT-X-PROGRAM-DATE-TIME:2026-11-19T18:00:00.000+0800\nseg_abcd_000041.ts\n"
        "#EXTINF:0.750,\nseg_abcd_000042.ts\n#EXT-X-ENDLIST\n",
        encoding="utf-8",
    )

    playlist = read_media_playlist(index)

    first, second = playlist.fragments
    assert (first.sequence, second.sequence) == (41, 42)
    assert second.start_epoch - first.start_epoch == 1.25
    assert playlist.duration == 2
    assert playlist.ended
