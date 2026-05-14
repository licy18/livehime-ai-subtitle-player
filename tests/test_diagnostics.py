import pytest

from app.core.config import AppConfig, InputConfig, RuntimeConfig
from app.services.diagnostics import run_diagnostics


@pytest.mark.asyncio
async def test_diagnostics_reports_direct_input(tmp_path):
    media = tmp_path / "sample.mp4"
    media.write_bytes(b"not a real video")
    config = AppConfig(
        input=InputConfig(url=str(media), resolver="direct"),
        runtime=RuntimeConfig(
            runtime_dir=str(tmp_path / "runtime"),
            hls_dir=str(tmp_path / "runtime" / "hls"),
            live_terms_db=str(tmp_path / "runtime" / "live_terms.sqlite3"),
            unknown_terms_log=str(tmp_path / "runtime" / "unknown_terms.log"),
        ),
    )

    result = await run_diagnostics(config)

    assert result["input"]["resolution"]["ok"] is True
    assert result["input"]["resolution"]["local_path_exists"] is True
    assert "ffmpeg" in result["tools"]

