from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import AppConfig, load_config, public_config, save_config
from app.models.term import Term
from app.services.diagnostics import run_diagnostics
from app.services.glossary_builder import GlossaryBuilder
from app.services.pipeline import RuntimePipeline
from app.services.streaming import StreamResolver
from app.services.terminology_manager import TerminologyManager
from app.services.translator import create_translator
from app.services.websocket_manager import WebSocketManager


def create_app(config: AppConfig | None = None, config_path: str | Path = "config.yaml") -> FastAPI:
    app_config = config or load_config()
    process_started_at = time.time()
    terminology = TerminologyManager(app_config)
    translator = create_translator(app_config)
    websocket_manager = WebSocketManager()
    pipeline = RuntimePipeline(app_config, terminology, translator, websocket_manager)
    glossary_builder = GlossaryBuilder(translator, app_config)

    app = FastAPI(title="LiveHime AI Subtitle Player", version="0.1.0")
    app.state.config = app_config
    app.state.config_path = Path(config_path)
    app.state.terminology = terminology
    app.state.pipeline = pipeline
    app.state.glossary_builder = glossary_builder

    static_dir = Path(__file__).parent / "static"
    template_dir = Path(__file__).parent / "templates"
    Path(app_config.runtime.hls_dir).mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    app.mount("/hls", StaticFiles(directory=app_config.runtime.hls_dir), name="hls")

    @app.middleware("http")
    async def no_cache_runtime_assets(request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path.startswith(("/hls/", "/api/")) or path in {"/player", "/static/js/player.js"}:
            response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    @app.on_event("shutdown")
    async def shutdown() -> None:
        pipeline.stop()

    @app.get("/", response_class=HTMLResponse)
    async def root() -> HTMLResponse:
        return HTMLResponse('<meta http-equiv="refresh" content="0; url=/player">')

    @app.get("/player")
    async def player() -> FileResponse:
        return FileResponse(template_dir / "player.html")

    @app.get("/admin/glossary-builder")
    async def glossary_builder_page() -> FileResponse:
        return FileResponse(template_dir / "glossary_builder.html")

    @app.get("/admin/live-terms")
    async def live_terms_page() -> FileResponse:
        return FileResponse(template_dir / "live_terms.html")

    @app.get("/api/health")
    async def health() -> dict:
        return {"ok": True, "started_at": process_started_at}

    @app.get("/api/status")
    async def status() -> dict:
        return pipeline.status()

    @app.get("/api/diagnostics")
    async def diagnostics() -> dict:
        return await run_diagnostics(app_config)

    @app.post("/api/diagnostics/deepseek-test")
    async def deepseek_test() -> dict:
        return await pipeline.deepseek_test()

    @app.get("/api/time")
    async def server_time() -> dict:
        import time

        return {"monotonic": time.monotonic(), "epoch": time.time()}

    @app.get("/api/config/public")
    async def config_public() -> dict:
        return public_config(app_config)

    @app.get("/api/config")
    async def get_config() -> dict:
        return app_config.model_dump(mode="json")

    @app.post("/api/input/qualities")
    async def input_qualities(payload: dict) -> dict:
        preview_config = app_config.model_copy(deep=True)
        input_payload = payload.get("input", {})
        network_payload = payload.get("network", {})
        if "url" in input_payload:
            preview_config.input.url = input_payload["url"]
        if "resolver" in input_payload:
            preview_config.input.resolver = input_payload["resolver"]
        if "quality" in input_payload:
            preview_config.input.quality = input_payload["quality"]
        if "proxy_enabled" in network_payload:
            preview_config.network.proxy_enabled = bool(network_payload["proxy_enabled"])
        if "proxy_url" in network_payload:
            preview_config.network.proxy_url = network_payload["proxy_url"]
        try:
            return await StreamResolver(preview_config).list_qualities()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.put("/api/config")
    async def put_config(payload: dict) -> dict:
        nonlocal app_config
        try:
            updated = AppConfig.model_validate(payload)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        save_config(updated, app.state.config_path)
        app_config = updated
        app.state.config = updated
        return updated.model_dump(mode="json")

    @app.post("/api/runtime/save-config-and-restart")
    async def save_config_and_restart(payload: dict) -> dict:
        nonlocal app_config
        try:
            updated = AppConfig.model_validate(payload)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        save_config(updated, app.state.config_path)
        app_config = updated
        app.state.config = updated
        pipeline.stop()
        pipeline.hls_buffer.clear_runtime_files()
        _restart_after_response(app.state.config_path)
        return {"ok": True, "message": "Configuration saved. Backend restarting."}

    @app.post("/api/runtime/start-hls")
    async def start_hls() -> dict:
        return await pipeline.start_hls()

    @app.post("/api/runtime/mock-subtitle")
    async def mock_subtitle() -> dict:
        return await pipeline.emit_mock_once()

    @app.post("/api/runtime/start-mock-loop")
    async def start_mock_loop() -> dict:
        pipeline.start_mock_loop()
        return {"ok": True}

    @app.post("/api/runtime/start-asr")
    async def start_asr() -> dict:
        return await pipeline.start_asr()

    @app.post("/api/runtime/stop")
    async def stop_runtime() -> dict:
        pipeline.stop()
        return pipeline.status()

    @app.post("/api/runtime/clear-hls")
    async def clear_hls() -> dict:
        return pipeline.clear_hls()

    @app.get("/api/subtitles/history")
    async def subtitle_history() -> list[dict]:
        return pipeline.history_items()

    @app.post("/api/subtitles/history/clear")
    async def clear_subtitle_history() -> dict:
        pipeline.clear_history()
        return {"ok": True}

    @app.get("/api/terms")
    async def get_terms() -> list[dict]:
        return [term.model_dump(mode="json") for term in terminology.all_terms()]

    @app.post("/api/terms")
    async def add_term(payload: dict) -> dict:
        term = Term.model_validate(payload)
        terminology.add_live_term(term)
        return term.model_dump(mode="json")

    @app.patch("/api/terms/{term_id}")
    async def patch_term(term_id: str, payload: dict) -> dict:
        try:
            term = terminology.update_live_term(term_id, payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Term not found") from exc
        return term.model_dump(mode="json")

    @app.delete("/api/terms/{term_id}")
    async def delete_term(term_id: str) -> dict:
        terminology.delete_live_term(term_id)
        return {"ok": True}

    @app.post("/api/terms/reload")
    async def reload_terms() -> dict:
        terminology.reload()
        return {"ok": True, "terms": len(terminology.all_terms())}

    @app.post("/api/terms/import")
    async def import_terms(payload: dict) -> dict:
        count = 0
        for item in payload.get("terms", []):
            terminology.add_live_term(Term.model_validate(item))
            count += 1
        return {"ok": True, "imported": count}

    @app.post("/api/terms/export")
    async def export_terms() -> dict:
        return {"terms": [term.model_dump(mode="json") for term in terminology.all_terms()]}

    @app.get("/api/terms/unknown")
    async def unknown_terms() -> list[dict]:
        return terminology.unknown_terms()

    @app.post("/api/terms/unknown/{unknown_id}/promote")
    async def promote_unknown(unknown_id: str, payload: dict) -> dict:
        term = Term.model_validate(payload | {"id": payload.get("id", unknown_id)})
        terminology.add_live_term(term)
        return term.model_dump(mode="json")

    @app.post("/api/glossary-builder/jobs")
    async def create_glossary_job(payload: dict) -> dict:
        job = await glossary_builder.create_job(payload)
        return _job_response(job)

    @app.get("/api/glossary-builder/jobs/{job_id}")
    async def get_glossary_job(job_id: str) -> dict:
        try:
            return _job_response(glossary_builder.get_job(job_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Job not found") from exc

    @app.post("/api/glossary-builder/jobs/{job_id}/review")
    async def review_glossary_job(job_id: str, payload: dict) -> dict:
        try:
            return _job_response(glossary_builder.review_job(job_id, payload.get("draft_terms", [])))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Job not found") from exc

    @app.post("/api/glossary-builder/jobs/{job_id}/save")
    async def save_glossary_job(job_id: str, payload: dict) -> dict:
        try:
            path = payload.get("path") or f"glossaries/{app_config.glossary.game_id}.yaml"
            result = glossary_builder.save_job(job_id, path)
            terminology.reload()
            return result
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Job not found") from exc

    @app.websocket("/ws/events")
    async def websocket_events(websocket: WebSocket) -> None:
        await websocket_manager.connect(websocket)
        try:
            await websocket.send_json({"type": "hello", "status": pipeline.status()})
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            websocket_manager.disconnect(websocket)

    return app


def _job_response(job) -> dict:
    return {
        "job_id": job.id,
        "status": job.status,
        "draft_terms": [term.model_dump(mode="json") for term in job.draft_terms],
        "warnings": job.warnings,
        "needs_review": job.status == "completed",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()
    config = load_config(args.config)
    uvicorn.run(create_app(config, args.config), host=config.server.host, port=config.server.port)


def _restart_after_response(config_path: Path) -> None:
    def restart() -> None:
        time.sleep(0.2)
        cwd = Path.cwd()
        resolved_config = config_path if config_path.is_absolute() else cwd / config_path
        if os.name == "nt":
            command = (
                "Start-Sleep -Milliseconds 1200; "
                f"Set-Location -LiteralPath '{_escape_powershell(cwd)}'; "
                f"& '{_escape_powershell(Path(sys.executable))}' -m app.main --config '{_escape_powershell(resolved_config)}'"
            )
            subprocess.Popen(
                ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", command],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        else:
            subprocess.Popen(
                ["sh", "-c", f"sleep 1.2; cd '{cwd}'; '{sys.executable}' -m app.main --config '{resolved_config}'"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        os._exit(0)

    threading.Thread(target=restart, daemon=True).start()


def _escape_powershell(value: Path | str) -> str:
    return str(value).replace("'", "''")


if __name__ == "__main__":
    main()
