"""Flux Download Manager API and single-page web application."""
from __future__ import annotations

import csv
import io
import os
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import DATA_DIR, DOWNLOAD_DIR, MAX_PLAYLIST_ITEMS, STATIC_DIR
from .db import Database
from .engine import DownloadManager
from .media import capabilities, choose_format, playlist_entries, probe_url
from .security import UnsafeURL, validate_url


class ProbeRequest(BaseModel):
    url: str = Field(min_length=8, max_length=4096)
    kind: Literal["auto", "media", "file"] = "auto"


class AddRequest(BaseModel):
    url: str = Field(min_length=8, max_length=4096)
    kind: Literal["auto", "media", "file"] = "auto"
    quality: str = "best"
    title: str | None = Field(default=None, max_length=300)
    thumbnail: str | None = Field(default=None, max_length=4096)
    duration: float | None = Field(default=None, ge=0)
    size: int | None = Field(default=None, ge=0)
    priority: int = Field(default=0, ge=-10, le=10)
    save_dir: str | None = Field(default=None, max_length=4096)


class BatchRequest(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=40)
    kind: Literal["auto", "media", "file"] = "auto"
    quality: str = "best"
    save_dir: str | None = Field(default=None, max_length=4096)


class PlaylistRequest(BaseModel):
    url: str = Field(min_length=8, max_length=4096)
    quality: str = "best"
    max_items: int = Field(default=30, ge=1, le=MAX_PLAYLIST_ITEMS)
    save_dir: str | None = Field(default=None, max_length=4096)


class SettingsRequest(BaseModel):
    concurrency: int | None = Field(default=None, ge=1, le=6)
    connections_per_download: int | None = Field(default=None, ge=1, le=8)
    speed_limit_kbps: int | None = Field(default=None, ge=0, le=1000000)
    auto_retries: int | None = Field(default=None, ge=0, le=8)
    default_save_dir: str | None = Field(default=None, max_length=4096)


class FolderValidateRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


class FolderCreateRequest(BaseModel):
    parent: str = Field(min_length=1, max_length=4096)
    name: str = Field(min_length=1, max_length=100)


class PriorityRequest(BaseModel):
    priority: int = Field(ge=-10, le=10)


def create_app(db_path: Path | None = None, download_dir: Path | None = None) -> FastAPI:
    db = Database(db_path or DATA_DIR / "flux.db")
    manager = DownloadManager(db, download_dir or DOWNLOAD_DIR)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        manager.start()
        yield
        manager.stop()

    app = FastAPI(title="Flux Download Manager", version="1.1.0", lifespan=lifespan,
                  description="Local download queue with resumable HTTP and maximum available media quality.")
    app.state.manager = manager
    app.state.db = db

    @app.exception_handler(UnsafeURL)
    async def handle_unsafe_url(request, error: UnsafeURL):
        return JSONResponse({"detail": str(error)}, status_code=400)

    @app.exception_handler(ValueError)
    async def handle_value_error(request, error: ValueError):
        return JSONResponse({"detail": str(error)}, status_code=400)

    @app.exception_handler(KeyError)
    async def handle_key_error(request, error: KeyError):
        return JSONResponse({"detail": str(error.args[0]) if error.args else "Not found"}, status_code=404)

    @app.get("/api/health")
    def health():
        return {"ok": True, "name": "Flux Download Manager", "version": "1.1.0", "capabilities": capabilities()}

    @app.post("/api/probe")
    def probe(body: ProbeRequest):
        try:
            return probe_url(body.url, body.kind)
        except UnsafeURL:
            raise
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)[:600])

    @app.get("/api/jobs")
    def list_jobs():
        return {"jobs": db.list_jobs()}

    @app.post("/api/jobs", status_code=201)
    def add_job(body: AddRequest):
        return manager.add(body.url, kind=body.kind, quality=body.quality,
                           title=body.title, thumbnail=body.thumbnail, duration=body.duration,
                           total_bytes=body.size, priority=body.priority, save_dir=body.save_dir)

    @app.post("/api/jobs/batch", status_code=201)
    def add_batch(body: BatchRequest):
        if body.kind == "media":
            choose_format(body.quality)
        manager.folders.resolve(body.save_dir or db.get_settings().get("default_save_dir"))
        # Validate the complete batch so failures won't create half a queue.
        for url in body.urls:
            validate_url(url)
        return {"added": [manager.add(url, kind=body.kind, quality=body.quality,
                                       save_dir=body.save_dir) for url in body.urls]}

    @app.post("/api/playlists/import", status_code=201)
    def import_playlist(body: PlaylistRequest):
        choose_format(body.quality)
        manager.folders.resolve(body.save_dir or db.get_settings().get("default_save_dir"))
        try:
            entries = playlist_entries(body.url, body.max_items)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)[:600])
        if not entries:
            raise HTTPException(status_code=422, detail="No usable video URLs were found in this playlist.")
        queued = [manager.add(entry["url"], kind="media", quality=body.quality,
                              title=entry["title"], thumbnail=entry["thumbnail"],
                              duration=entry["duration"], save_dir=body.save_dir) for entry in entries]
        return {"count": len(queued), "added": queued}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        return manager._required(job_id)

    @app.post("/api/jobs/{job_id}/pause")
    def pause_job(job_id: str):
        return manager.pause(job_id)

    @app.post("/api/jobs/{job_id}/resume")
    def resume_job(job_id: str):
        return manager.resume(job_id)

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str):
        return manager.cancel(job_id)

    @app.patch("/api/jobs/{job_id}/priority")
    def change_priority(job_id: str, body: PriorityRequest):
        return manager.update_priority(job_id, body.priority)

    @app.delete("/api/jobs/{job_id}")
    def remove_job(job_id: str, delete_file: bool = False):
        manager.delete(job_id, delete_file=delete_file)
        return {"removed": True}

    @app.delete("/api/completed")
    def clear_completed(delete_files: bool = False):
        return {"cleared": manager.clear_completed(delete_files)}

    @app.get("/api/jobs/{job_id}/file")
    def download_finished_file(job_id: str):
        job = manager._required(job_id)
        path = Path(job.get("final_path") or "").resolve()
        expected = Path(job.get("save_dir") or manager.download_dir).resolve()
        if (job["status"] != "completed" or not path.is_file()
                or path.parent != expected):
            raise HTTPException(status_code=404, detail="Completed file not found.")
        return FileResponse(path, filename=job.get("filename") or path.name,
                            media_type="application/octet-stream")

    @app.get("/api/export")
    def export_jobs():
        output = io.StringIO()
        writer = csv.writer(output)
        columns = ("title", "url", "kind", "quality_label", "status", "filename", "save_dir", "total_bytes", "created_at")
        writer.writerow(columns)
        for job in db.list_jobs():
            cells = []
            for column in columns:
                value = str(job.get(column, "") or "")
                # Stop exported titles/URLs becoming spreadsheet formulas.
                if value.lstrip().startswith(("=", "+", "-", "@")):
                    value = "'" + value
                cells.append(value)
            writer.writerow(cells)
        content = "\ufeff" + output.getvalue()
        return Response(content=content, media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="flux-downloads.csv"'})

    @app.get("/api/folders")
    def browse_folders(path: str | None = Query(default=None, max_length=4096)):
        return manager.folders.browse(path)

    @app.post("/api/folders/validate")
    def validate_folder(body: FolderValidateRequest):
        return {"path": str(manager.folders.resolve(body.path))}

    @app.post("/api/folders", status_code=201)
    def create_folder(body: FolderCreateRequest):
        return manager.folders.create(body.parent, body.name)

    @app.get("/api/config")
    def config():
        settings = db.get_settings()
        try:
            default_folder = manager.folders.resolve(settings.get("default_save_dir"))
        except ValueError:
            default_folder = manager.download_dir
            settings = db.update_settings({"default_save_dir": str(default_folder)})
        disk = shutil.disk_usage(default_folder)
        return {"settings": settings, "capabilities": capabilities(),
                "download_dir": str(default_folder),
                "base_download_dir": str(manager.download_dir),
                "disk_free": disk.free, "disk_total": disk.total}

    @app.patch("/api/config")
    def update_config(body: SettingsRequest):
        changes = body.model_dump(exclude_none=True)
        if "default_save_dir" in changes:
            changes["default_save_dir"] = str(manager.folders.resolve(changes["default_save_dir"]))
        settings = db.update_settings(changes)
        manager.wakeup.set()
        return {"settings": settings}

    @app.get("/")
    def frontend():
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=os.getenv("FLUX_HOST", "127.0.0.1"),
                port=int(os.getenv("FLUX_PORT", "8000")), reload=False)
