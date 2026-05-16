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
