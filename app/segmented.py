"""Accelerated multi-connection HTTP download with persistent resumable segments.

Only used when a public HTTP(S) server explicitly confirms byte-range support
and the download is at least 8 MiB. If it cannot reliably honor ranges,
the caller falls back to a regular single-stream download.
"""
from __future__ import annotations

import math
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlparse

import requests

from .db import Database
from .media import filename_from_header, safe_request
from .security import safe_filename

MINIMUM_SEGMENTED_SIZE = 8 * 1024 * 1024


class RangeUnsupported(Exception):
    """Server claimed range support, but declined a specific byte range."""


def inspect_range_support(url: str) -> tuple[int, str] | None:
    try:
        head = safe_request("HEAD", url)
        try:
            if head.status_code >= 400:
                return None
            size = head.headers.get("Content-Length", "")
            if not size.isdecimal() or int(size) < MINIMUM_SEGMENTED_SIZE:
                return None
            if "bytes" not in head.headers.get("Accept-Ranges", "").lower():
                return None
            if head.headers.get("Content-Encoding", "identity").lower() != "identity":
                return None
            if head.headers.get("Content-Type", "").lower().startswith("text/html"):
                return None
            name = filename_from_header(head.headers.get("Content-Disposition", "")) or unquote(Path(urlparse(url).path).name)
        finally:
            head.close()
        probe = safe_request("GET", url, headers={"Range": "bytes=0-0"})
        try:
            if probe.status_code != 206:
                return None
            found = re.fullmatch(r"bytes 0-0/(\d+)", probe.headers.get("Content-Range", ""))
            if not found or int(found.group(1)) != int(size):
                return None
        finally:
            probe.close()
        return int(size), safe_filename(name, "download")
    except (requests.RequestException, RuntimeError, ValueError):
        return None


def clear_segments(parts_dir: Path, job_id: str) -> None:
    for path in parts_dir.glob(f"{job_id}.seg[0-9]*"):
        if path.is_file():
            path.unlink(missing_ok=True)


def transfer_segmented(
    job: dict, db: Database, parts_dir: Path, stop_check: Callable[[], None],
    connections: int, speed_limit_kbps: int = 0,
) -> tuple[str, Path, int] | None:
    """Return (filename, assembled .part path, bytes) or None for single-stream fallback.

    Segment parts remain on pause or transient network failure. Each worker
    validates Content-Range. The concatenation step verifies exact byte counts.
    """
    details = inspect_range_support(job["url"])
    if not details:
        clear_segments(parts_dir, job["id"])
        return None
    total, name = details
    old_n = int(job.get("segments") or 0)
    old_total = int(job.get("total_bytes") or 0)
    if old_n and old_total and old_total != total:
        clear_segments(parts_dir, job["id"])
        old_n = 0
    n = min(max(2, old_n or connections), 8)
    block_size = math.ceil(total / n)
    ranges: list[tuple[int, int, Path]] = []
    for index in range(n):
        start = index * block_size
        end = min((index + 1) * block_size, total) - 1
        path = parts_dir / f"{job['id']}.seg{index}"
        if path.exists() and path.stat().st_size > end - start + 1:
            path.unlink()
        ranges.append((start, end, path))
    initial = sum(path.stat().st_size for _, _, path in ranges if path.is_file())
    db.update_job(job["id"], segments=n, total_bytes=total, filename=name,
                  downloaded_bytes=initial)

    abort = threading.Event()
    progress_lock = threading.Lock()
    tracking = {"bytes": initial, "last_bytes": initial, "last": time.monotonic()}

    def segment(start: int, end: int, path: Path):
        stop_check()
        if abort.is_set():
            return
        existing = path.stat().st_size if path.exists() else 0
        expected = end - start + 1
        if existing == expected:
            return
        request_start = start + existing
        response = safe_request("GET", job["url"], headers={"Range": f"bytes={request_start}-{end}"})
        try:
            if response.status_code != 206:
                raise RangeUnsupported(f"Server did not honor byte range {request_start}-{end}.")
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise RangeUnsupported("Server returned a compressed byte range.")
            expected_header = f"bytes {request_start}-{end}/{total}"
            if response.headers.get("Content-Range") != expected_header:
                raise RangeUnsupported("Server returned an inconsistent Content-Range header.")
            per_connection_limit = speed_limit_kbps * 1024 / n if speed_limit_kbps else 0
            received_this_request = 0
            started = time.monotonic()
            with path.open("ab" if existing else "wb") as handle:
                for chunk in response.iter_content(chunk_size=128 * 1024):
                    stop_check()
                    if abort.is_set():
                        return
                    if not chunk:
                        continue
                    if existing + received_this_request + len(chunk) > expected:
                        raise RangeUnsupported("The server sent more bytes than requested.")
                    handle.write(chunk)
                    received_this_request += len(chunk)
                    if per_connection_limit:
                        while True:
                            delay = received_this_request / per_connection_limit - (time.monotonic() - started)
                            if delay <= 0:
                                break
                            stop_check()
                            if abort.is_set():
                                return
                            time.sleep(min(delay, .25))
                    with progress_lock:
                        tracking["bytes"] += len(chunk)
                        now = time.monotonic()
                        elapsed = now - tracking["last"]
                        if elapsed >= .35:
                            speed = (tracking["bytes"] - tracking["last_bytes"]) / elapsed
                            eta = int(max(0, (total - tracking["bytes"]) / speed)) if speed > 0 else None
                            db.update_job(job["id"], downloaded_bytes=tracking["bytes"],
                                          total_bytes=total, speed=round(speed, 1), eta=eta)
                            tracking["last"] = now
                            tracking["last_bytes"] = tracking["bytes"]
                handle.flush()
                os.fsync(handle.fileno())
            if path.stat().st_size != expected:
                raise RuntimeError("HTTP byte-range transfer ended before all bytes arrived.")
        finally:
            response.close()

    first_error = None
    with ThreadPoolExecutor(max_workers=n, thread_name_prefix="flux-segment") as pool:
        futures = [pool.submit(segment, *args) for args in ranges]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:
                abort.set()
                if first_error is None:
                    first_error = exc
    if first_error:
        if isinstance(first_error, RangeUnsupported):
            # A lying / misconfigured server cannot be safely resumed in pieces.
            clear_segments(parts_dir, job["id"])
            db.update_job(job["id"], segments=0, downloaded_bytes=0)
            return None
        raise first_error
    stop_check()
    assembled = parts_dir / f"{job['id']}.part"
    with assembled.open("wb") as output:
        for start, end, part in ranges:
            with part.open("rb") as src:
                while True:
                    stop_check()
                    data = src.read(1024 * 1024)
                    if not data:
                        break
                    output.write(data)
        output.flush()
        os.fsync(output.fileno())
    if assembled.stat().st_size != total:
        raise RuntimeError("The assembled download does not match the declared file size.")
    db.update_job(job["id"], downloaded_bytes=total, total_bytes=total, speed=0, eta=0)
    clear_segments(parts_dir, job["id"])
    return name, assembled, total
