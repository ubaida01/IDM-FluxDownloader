"""Persistent queue, resumable direct downloads and yt-dlp media workers."""
from __future__ import annotations

import errno
import os
import re
import shutil
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import requests

from .config import DOWNLOAD_DIR, MAX_WORKERS
from .db import Database, now
from .media import audio_mp3_quality, choose_format, filename_from_header, probe_url, safe_request, yt_dlp
from .security import safe_filename, unique_destination, validate_url
from .segmented import transfer_segmented, clear_segments
from .storage import DownloadFolders


class DownloadStopped(Exception):
    pass


@dataclass
class Control:
    action: str | None = None
    delete_record: bool = False
    delete_file: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)

    def set_action(self, action: str, delete_record: bool = False, delete_file: bool = False) -> None:
        with self.lock:
            self.action = action
            self.delete_record |= delete_record
            self.delete_file |= delete_file

    def snapshot(self) -> tuple[str | None, bool, bool]:
        with self.lock:
            return self.action, self.delete_record, self.delete_file


def _stop_if_requested(control: Control) -> None:
    if control.snapshot()[0] in ("pause", "cancel"):
        raise DownloadStopped()


def _filename_from_url(url: str) -> str:
    return safe_filename(unquote(Path(urlparse(url).path).name), "download")


class DownloadManager:
    def __init__(self, db: Database, download_dir: Path = DOWNLOAD_DIR):
        self.db = db
        self.download_dir = download_dir.resolve()
        self.folders = DownloadFolders(self.download_dir)
        self.parts_dir = self.download_dir / ".parts"
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.parts_dir.mkdir(parents=True, exist_ok=True)
        self.executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="flux")
        self.running: dict[str, tuple[Control, Future]] = {}
        self.lock = threading.RLock()
        self.shutdown = threading.Event()
        self.wakeup = threading.Event()
        self.scheduler: threading.Thread | None = None

    def start(self) -> None:
        if self.scheduler and self.scheduler.is_alive():
            return
        self.scheduler = threading.Thread(target=self._schedule_loop, name="flux-scheduler", daemon=True)
        self.scheduler.start()

    def stop(self) -> None:
        self.shutdown.set()
        self.wakeup.set()
        with self.lock:
            for control, _ in self.running.values():
                control.set_action("pause")
        if self.scheduler:
            self.scheduler.join(timeout=4)
        # A blocked network read may need up to the configured socket timeout.
        self.executor.shutdown(wait=False, cancel_futures=True)

    def add(self, url: str, kind: str = "auto", quality: str = "best", title: str | None = None,
            thumbnail: str | None = None, duration: float | None = None,
            total_bytes: int | None = None, priority: int = 0,
            save_dir: str | None = None) -> dict:
        url = validate_url(url)
        if kind not in ("auto", "file", "media"):
            raise ValueError("Invalid download type.")
        if kind == "media":
            choose_format(quality)
        else:
            quality = "original"
        label = self.quality_label(quality)
        requested_dir = save_dir if save_dir is not None else self.db.get_settings().get("default_save_dir")
        destination = self.folders.resolve(requested_dir)
        job_id = uuid.uuid4().hex
        job = {
            "id": job_id, "url": url, "title": safe_filename(title or _filename_from_url(url), "download") if kind == "file" else (title or _filename_from_url(url)),
            "kind": kind, "status": "queued", "quality": quality, "quality_label": label,
            "priority": max(-10, min(10, int(priority))), "filename": None,
            "final_path": None, "save_dir": str(destination), "total_bytes": max(0, int(total_bytes or 0)),
            "downloaded_bytes": 0, "speed": 0, "eta": None,
            "duration": duration, "thumbnail": thumbnail, "error": None,
            "attempts": 0, "created_at": now(), "updated_at": now(),
        }
        self.db.insert_job(job)
        self.wakeup.set()
        return job

    @staticmethod
    def quality_label(quality: str) -> str:
        if quality == "best":
            return "Best available"
        if quality == "audio:source":
            return "Original audio"
        if quality == "audio:mp3":
            return "Best-quality MP3 (VBR)"
        if quality.startswith("audio:mp3:"):
            return f"MP3 · {quality.split(':')[-1]} kbps"
        if quality.startswith("audio:fmt:"):
            return "Source audio · selected stream"
        if quality.startswith("res:"):
            return f"Up to {quality[4:]}p"
        if quality.startswith("fmt:"):
            return "Selected format"
        return "Original file"

    def pause(self, job_id: str) -> dict:
        with self.lock:
            job = self._required(job_id)
            if job["status"] == "queued":
                self.db.update_job(job_id, status="paused", speed=0, eta=None)
            elif job["status"] in ("downloading", "processing", "pausing"):
                if job_id in self.running:
                    self.running[job_id][0].set_action("pause")
                    self.db.update_job(job_id, status="pausing")
                else:
                    self.db.update_job(job_id, status="paused", speed=0, eta=None)
            elif job["status"] != "paused":
                raise ValueError("Only queued or active downloads can be paused.")
            return self._required(job_id)

    def resume(self, job_id: str) -> dict:
        with self.lock:
            job = self._required(job_id)
            if job["status"] not in ("paused", "failed", "cancelled"):
                raise ValueError("Only paused, failed, or cancelled downloads can be resumed.")
            self.db.update_job(job_id, status="queued", error=None, speed=0, eta=None)
            self.wakeup.set()
            return self._required(job_id)

    def cancel(self, job_id: str) -> dict:
        with self.lock:
            job = self._required(job_id)
            if job["status"] in ("downloading", "processing", "pausing") and job_id in self.running:
                self.running[job_id][0].set_action("cancel")
                self.db.update_job(job_id, status="pausing")
            elif job["status"] in ("queued", "paused", "failed"):
                self.db.update_job(job_id, status="cancelled", speed=0, eta=None)
                self._cleanup_partials(job_id)
            elif job["status"] != "cancelled":
                raise ValueError("This download cannot be cancelled.")
            return self._required(job_id)

    def delete(self, job_id: str, delete_file: bool = False) -> None:
        with self.lock:
            job = self._required(job_id)
            if job_id in self.running:
                control = self.running[job_id][0]
                control.set_action("cancel", delete_record=True, delete_file=delete_file)
                self.db.update_job(job_id, status="pausing")
                return
            self._cleanup_partials(job_id)
            if delete_file:
                self._delete_completed_file(job)
            self.db.delete_job(job_id)

    def update_priority(self, job_id: str, priority: int) -> dict:
        if not -10 <= priority <= 10:
            raise ValueError("Priority must be between -10 and 10.")
        with self.lock:
            self._required(job_id)
            self.db.update_job(job_id, priority=priority)
            self.wakeup.set()
            return self._required(job_id)

    def clear_completed(self, delete_files: bool = False) -> int:
        count = 0
        for job in self.db.list_jobs():
            if job["status"] == "completed":
                self.delete(job["id"], delete_file=delete_files)
                count += 1
        return count

    def _required(self, job_id: str) -> dict:
        job = self.db.get_job(job_id)
        if not job:
            raise KeyError("Download not found.")
        return job

    def _schedule_loop(self) -> None:
        while not self.shutdown.is_set():
            try:
                with self.lock:
                    self.running = {key: entry for key, entry in self.running.items() if not entry[1].done()}
                    settings = self.db.get_settings()
                    available = max(0, min(MAX_WORKERS, settings.get("concurrency", 3)) - len(self.running))
                    if available:
                        for job in self.db.queued(available):
                            if not self.db.set_status_if(job["id"], ("queued",), "downloading", error=None):
                                continue
                            control = Control()
                            future = self.executor.submit(self._run, job["id"], control)
                            self.running[job["id"]] = (control, future)
                self.wakeup.wait(timeout=0.4)
                self.wakeup.clear()
            except Exception as exc:
                print(f"[Flux scheduler] {exc}", flush=True)
                time.sleep(1)

    def _run(self, job_id: str, control: Control) -> None:
        try:
            job = self._required(job_id)
            if job["kind"] == "auto":
                info = probe_url(job["url"])
                _stop_if_requested(control)
                job_kind = info["kind"]
                self.db.update_job(job_id, kind=job_kind, title=info.get("title") or job["title"],
                                   thumbnail=info.get("thumbnail"), duration=info.get("duration"),
                                   total_bytes=info.get("size") or 0,
                                   quality="best" if job_kind == "media" else "original",
                                   quality_label="Best available" if job_kind == "media" else "Original file")
                job = self._required(job_id)
            if job["kind"] == "media":
                self._download_media(job, control)
            else:
                self._download_direct(job, control)
        except DownloadStopped:
            self._finish_stopped(job_id, control)
        except Exception as exc:
            # Some media extractors wrap our stop exception in their own error.
            # A user-requested pause/cancel must not be misclassified as failure.
            if control.snapshot()[0] in ("pause", "cancel"):
                self._finish_stopped(job_id, control)
            else:
                self.db.update_job(job_id, status="failed", speed=0, eta=None, error=str(exc)[:650])
        finally:
            self.wakeup.set()

    def _finish_stopped(self, job_id: str, control: Control) -> None:
        action, delete_record, delete_file = control.snapshot()
        if delete_record:
            job = self.db.get_job(job_id)
            if job:
                self._cleanup_partials(job_id)
                if delete_file:
                    self._delete_completed_file(job)
                self.db.delete_job(job_id)
        elif action == "cancel":
            self._cleanup_partials(job_id)
            self.db.update_job(job_id, status="cancelled", speed=0, eta=None, error=None)
        else:
            self.db.update_job(job_id, status="paused", speed=0, eta=None, error=None)

    def _download_direct(self, job: dict, control: Control) -> None:
        job_id = job["id"]
        part = self.parts_dir / f"{job_id}.part"
        settings = self.db.get_settings()
        retries = max(0, min(8, settings.get("auto_retries", 3)))
        for attempt in range(retries + 1):
            _stop_if_requested(control)
            self.db.update_job(job_id, attempts=attempt + 1)
            try:
                accelerated = None
                if settings.get("connections_per_download", 4) > 1:
                    accelerated = transfer_segmented(
                        self._required(job_id), self.db, self.parts_dir,
                        lambda: _stop_if_requested(control),
                        settings.get("connections_per_download", 4),
                        settings.get("speed_limit_kbps", 0),
                    )
                if accelerated:
                    name, part, amount = accelerated
                else:
                    name, amount = self._transfer_direct(job, part, control)
                _stop_if_requested(control)
                with self.lock:
                    destination = self.folders.resolve(job.get("save_dir"))
                    dest = unique_destination(destination, name)
                    self._relocate(part, dest)
                self.db.update_job(job_id, status="completed", filename=dest.name, final_path=str(dest),
                                   total_bytes=amount, downloaded_bytes=amount, speed=0, eta=0, error=None)
                return
            except DownloadStopped:
                raise
            except Exception:
                if attempt >= retries:
                    raise
                delay = min(2 ** attempt, 6)
                for _ in range(delay * 5):
                    _stop_if_requested(control)
                    time.sleep(0.2)

    def _transfer_direct(self, job: dict, part: Path, control: Control) -> tuple[str, int]:
        job_id = job["id"]
        initial = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={initial}-"} if initial else {}
        response = safe_request("GET", job["url"], headers=headers)
        try:
            if initial and response.status_code == 416:
                length = response.headers.get("Content-Range", "")
                match = re.search(r"\*/(\d+)", length)
                if match and int(match.group(1)) == initial:
                    return safe_filename(job.get("filename") or _filename_from_url(job["url"])), initial
            response.raise_for_status()
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise RuntimeError("The server returned a compressed transfer that cannot safely resume.")
            if response.status_code not in (200, 206):
                raise RuntimeError(f"Unexpected HTTP status: {response.status_code}.")
            resumed = initial > 0 and response.status_code == 206
            content_range = response.headers.get("Content-Range", "")
            if resumed and not content_range.startswith(f"bytes {initial}-"):
                raise RuntimeError("Server returned an incorrect byte range. Cannot safely resume.")
            if not resumed:
                initial = 0
                part.write_bytes(b"")
            disposition = response.headers.get("Content-Disposition", "")
            name = safe_filename(filename_from_header(disposition) or job.get("filename") or _filename_from_url(job["url"]))
            content_type = response.headers.get("Content-Type", "").lower()
            if content_type.startswith("text/html") and not Path(name).suffix.lower() in (".html", ".htm"):
                raise RuntimeError("The URL returned an HTML web page, not a downloadable file.")
            size_header = response.headers.get("Content-Length", "")
            length = int(size_header) if size_header.isdecimal() else 0
            range_match = re.search(r"bytes \d+-\d+/(\d+|\*)", content_range)
            total = (int(range_match.group(1)) if range_match and range_match.group(1).isdigit()
                     else initial + length if length else 0)
            self.db.update_job(job_id, title=name if job["title"] in ("download", _filename_from_url(job["url"])) else job["title"],
                               filename=name, total_bytes=total, downloaded_bytes=initial)
            downloaded = initial
            last_tick = time.monotonic()
            last_bytes = downloaded
            time_started = time.monotonic()
            bytes_received = 0
            limit_kbps = self.db.get_settings().get("speed_limit_kbps", 0)
            with part.open("ab" if resumed else "wb") as file:
                for chunk in response.iter_content(chunk_size=256 * 1024):
                    _stop_if_requested(control)
                    if not chunk:
                        continue
                    file.write(chunk)
                    downloaded += len(chunk)
                    bytes_received += len(chunk)
                    if limit_kbps:
                        required_duration = bytes_received / (limit_kbps * 1024)
                        while True:
                            to_sleep = required_duration - (time.monotonic() - time_started)
                            if to_sleep <= 0:
                                break
                            _stop_if_requested(control)
                            time.sleep(min(to_sleep, .25))
                    t = time.monotonic()
                    if t - last_tick >= 0.35:
                        speed = (downloaded - last_bytes) / max(t - last_tick, 0.001)
                        eta = max(0, int((total - downloaded) / speed)) if total and speed > 0 else None
                        self.db.update_job(job_id, downloaded_bytes=downloaded, total_bytes=total,
                                           speed=round(speed, 1), eta=eta)
                        last_tick, last_bytes = t, downloaded
                file.flush()
                os.fsync(file.fileno())
            if total and total != downloaded:
                raise RuntimeError(f"Download was incomplete: received {downloaded} of {total} bytes.")
            self.db.update_job(job_id, downloaded_bytes=downloaded, total_bytes=downloaded or total)
            return name, downloaded
        finally:
            response.close()

    def _download_media(self, job: dict, control: Control) -> None:
        if yt_dlp is None:
            raise RuntimeError("yt-dlp is not installed. Run: pip install -r requirements.txt")
        job_id = job["id"]
        folder = self.parts_dir / job_id
        folder.mkdir(exist_ok=True)
        selector, mp3 = choose_format(job["quality"])
        audio_source = job["quality"] == "audio:source"
        if audio_source and not shutil.which("ffmpeg"):
            # The best audio-only stream works without FFmpeg. Never fall back
            # to a combined video file for an Audio Only request.
            selector = "ba"
        if mp3 and not shutil.which("ffmpeg"):
            raise RuntimeError("FFmpeg is required to convert audio to MP3.")
        last_progress = [0.0]

        def progress(info: dict) -> None:
            _stop_if_requested(control)
            state = info.get("status")
            if state == "finished":
                self.db.update_job(job_id, status="processing", speed=0, eta=None)
            if state not in ("downloading", "finished"):
                return
            t = time.monotonic()
            if state == "finished" or t - last_progress[0] > 0.4:
                downloaded = info.get("downloaded_bytes") or 0
                total = info.get("total_bytes") or info.get("total_bytes_estimate") or 0
                self.db.update_job(job_id, downloaded_bytes=downloaded, total_bytes=total,
                                   speed=info.get("speed") or 0, eta=info.get("eta"))
                last_progress[0] = t

        def postprocess(info: dict) -> None:
            _stop_if_requested(control)
            if info.get("status") == "started":
                self.db.update_job(job_id, status="processing", speed=0, eta=None)

        opts: dict[str, Any] = {
            "quiet": True, "no_warnings": True, "noplaylist": True,
            "format": selector, "outtmpl": str(folder / (job_id + ".%(ext)s")),
            "merge_output_format": "mkv", "progress_hooks": [progress],
            "postprocessor_hooks": [postprocess], "continuedl": True,
            "concurrent_fragment_downloads": 4,
            "retries": 5, "fragment_retries": 5, "socket_timeout": 25,
            "overwrites": False, "restrictfilenames": True,
            "windowsfilenames": True,
            "ignoreerrors": False, "writethumbnail": False,
        }
        speed_limit_kbps = self.db.get_settings().get("speed_limit_kbps", 0)
        if speed_limit_kbps:
            opts["ratelimit"] = speed_limit_kbps * 1024
        if mp3:
            opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                                       "preferredquality": audio_mp3_quality(job["quality"])}]
        elif audio_source and shutil.which("ffmpeg"):
            # If the site offers only a combined stream, strip its video track.
            # FFmpeg chooses stream-copy where supported; no upscaling occurs.
            opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "best"}]
        _stop_if_requested(control)
        with yt_dlp.YoutubeDL(opts) as ydl:
            result = ydl.download([job["url"]])
        _stop_if_requested(control)
        if result != 0:
            raise RuntimeError("Media download failed. The site may restrict the requested format.")
        # Audio-only requests must not accidentally return an unprocessed
        # full-size video left behind by an extractor/postprocessor.
        allowed = {".mp3"} if mp3 else (
            {".m4a", ".mp3", ".opus", ".ogg", ".flac", ".wav", ".aac", ".webm", ".mp4"}
            if job["quality"].startswith("audio:") else
            {".mp4", ".webm", ".mkv", ".m4a", ".mp3", ".opus", ".ogg", ".flac",
             ".wav", ".mov", ".aac", ".ts", ".avi"}
        )
        candidate_files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in allowed]
        if job["quality"].startswith("audio:"):
            # Prefer audio-only containers over MP4/WebM when both exist.
            audio_native = [p for p in candidate_files if p.suffix.lower() in
                            {".m4a", ".mp3", ".opus", ".ogg", ".flac", ".wav", ".aac"}]
            if audio_native:
                candidate_files = audio_native
        if not candidate_files:
            raise RuntimeError("No completed media file was produced. Check FFmpeg and the site's availability.")
        candidate = max(candidate_files, key=lambda p: p.stat().st_size)
        basename = safe_filename(job["title"], "media")
        if Path(basename).suffix.lower() in (".mp4", ".webm", ".mkv", ".mp3"):
            basename = Path(basename).stem
        size = candidate.stat().st_size
        with self.lock:
            destination = self.folders.resolve(job.get("save_dir"))
            dest = unique_destination(destination, basename + candidate.suffix)
            self._relocate(candidate, dest)
        self._cleanup_partials(job_id)
        self.db.update_job(job_id, status="completed", filename=dest.name, final_path=str(dest),
                           total_bytes=size, downloaded_bytes=size, eta=0, speed=0, error=None)

    @staticmethod
    def _relocate(source: Path, destination: Path) -> None:
        """Finish a transfer safely even when the selected folder is on another disk."""
        try:
            os.replace(source, destination)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            # Copy to a temporary filename on the destination filesystem, then
            # rename atomically so a half-copied cross-volume file isn't exposed.
            temp_path = destination.with_name(f".{destination.name}.flux-{uuid.uuid4().hex}.tmp")
            try:
                with source.open("rb") as inp, temp_path.open("xb") as out:
                    shutil.copyfileobj(inp, out, length=1024 * 1024)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(temp_path, destination)
                source.unlink()
            finally:
                temp_path.unlink(missing_ok=True)

    def _cleanup_partials(self, job_id: str) -> None:
        part = self.parts_dir / f"{job_id}.part"
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
        clear_segments(self.parts_dir, job_id)
        folder = self.parts_dir / job_id
        if folder.is_dir():
            shutil.rmtree(folder, ignore_errors=True)

    def _delete_completed_file(self, job: dict) -> None:
        value = job.get("final_path")
        if not value:
            return
        p = Path(value).resolve()
        expected = Path(job.get("save_dir") or self.download_dir).resolve()
        if p.parent == expected and p.is_file():
            p.unlink(missing_ok=True)
