"""Offline end-to-end tests with a real local range-enabled file server."""
from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import Database, now
from app.main import create_app
from app.media import choose_format, video_options
from app.security import UnsafeURL, safe_filename, validate_url

PAYLOAD = b"FLUX TEST DOWNLOAD \x00\x01\n" * 8192
BIG_PAYLOAD = bytes(range(256)) * (12 * 1024 * 1024 // 256)


class DownloadHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        return

    def get_content(self):
        return BIG_PAYLOAD if self.path.startswith("/big") else PAYLOAD

    def do_HEAD(self):
        payload = self.get_content()
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", 'attachment; filename="flux-test.bin"')
        self.send_header("Accept-Ranges", "none" if self.path.startswith("/no-range") else "bytes")
        self.end_headers()

    def do_GET(self):
        payload = self.get_content()
        start = 0
        end = len(payload) - 1
        hdr = self.headers.get("Range")
        if hdr and not self.path.startswith("/no-range"):
            byte_request = hdr.split("=")[1]
            start = int(byte_request.split("-")[0])
            if byte_request.split("-", 1)[1]:
                end = min(int(byte_request.split("-", 1)[1]), len(payload) - 1)
        if start >= len(payload):
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{len(payload)}")
            self.end_headers()
            return
        self.send_response(206 if hdr and not self.path.startswith("/no-range") else 200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", 'attachment; filename="flux-test.bin"')
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        if hdr and not self.path.startswith("/no-range"):
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(payload)}")
        self.end_headers()
        try:
            for offset in range(start, end + 1, 65536):
                self.wfile.write(payload[offset:min(offset + 65536, end + 1)])
                if self.path.startswith("/slow"):
                    time.sleep(0.032)
                elif self.path.startswith("/big-slow"):
                    time.sleep(0.02)
        except (BrokenPipeError, ConnectionResetError):
            pass


@pytest.fixture
def http_server(monkeypatch):
    monkeypatch.setattr("app.security.ALLOW_PRIVATE_HOSTS", True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), DownloadHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


@pytest.fixture
def api_client(tmp_path):
    application = create_app(tmp_path / "data" / "jobs.db", tmp_path / "downloads")
    with TestClient(application) as client:
        yield client, tmp_path


def wait_for(client, job_id, expected, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200
        job = response.json()
        if job["status"] in expected:
            return job
        time.sleep(.055)
    pytest.fail(f"Job did not transition to {expected}: {job}")


def test_local_link_blocked_by_default(monkeypatch):
    monkeypatch.setattr("app.security.ALLOW_PRIVATE_HOSTS", False)
    with pytest.raises(UnsafeURL):
        validate_url("http://127.0.0.1/secret")
    with pytest.raises(UnsafeURL):
        validate_url("file:///etc/passwd")
    with pytest.raises(UnsafeURL):
        validate_url("http://localhost/admin")


def test_filename_sanitizer():
    assert safe_filename("../some\\folder:bad?.zip") == "_some_folder_bad_.zip"
    assert safe_filename("../../") == "_"
    assert safe_filename("café.txt") == "café.txt"


def test_format_selection():
    assert choose_format("best") == ("bv*+ba/b/ba", False)
    assert choose_format("audio:mp3") == ("ba/b", True)
    assert "height<=1080" in choose_format("res:1080")[0]
    assert choose_format("fmt:137:v")[0] == "137+ba/137"
    assert choose_format("fmt:22:av")[0] == "22"
    with pytest.raises(ValueError):
        choose_format("fmt:137;rm -rf /:v")


def test_metadata_quality_options():
    info = {"formats": [
        {"format_id": "137", "height": 1080, "fps": 30, "vcodec": "avc1", "acodec": "none", "ext": "mp4"},
        {"format_id": "248", "height": 1080, "fps": 60, "vcodec": "vp9", "acodec": "none", "ext": "webm"},
        {"format_id": "22", "height": 720, "vcodec": "avc1", "acodec": "mp4a", "ext": "mp4"},
    ]}
    choices = video_options(info)
    assert choices[0]["value"] == "best"
    assert any(item["value"] == "res:1080" for item in choices)
    assert any(item["value"] == "audio:source" for item in choices)
    assert any(item["value"] == "fmt:248:v" for item in choices)


def test_probe_and_direct_download(api_client, http_server):
    client, folder = api_client
    inspection = client.post("/api/probe", json={"url": f"{http_server}/file.bin", "kind": "file"})
    assert inspection.status_code == 200
    assert inspection.json()["size"] == len(PAYLOAD)
    assert inspection.json()["title"] == "flux-test.bin"

    created = client.post("/api/jobs", json={"url": f"{http_server}/file.bin", "kind": "file", "title": "flux-test.bin"})
    assert created.status_code == 201
    job = wait_for(client, created.json()["id"], {"completed"})
    assert job["downloaded_bytes"] == len(PAYLOAD)
    assert Path(job["final_path"]).read_bytes() == PAYLOAD
    fetched = client.get(f"/api/jobs/{job['id']}/file")
    assert fetched.content == PAYLOAD
    assert fetched.status_code == 200
    assert client.get("/api/export").status_code == 200
    response = client.delete(f"/api/jobs/{job['id']}")
    assert response.status_code == 200
    assert Path(job["final_path"]).exists(), "History removal should not erase saved file"


def test_pause_and_resume_range(api_client, http_server):
    client, folder = api_client
    resp = client.post("/api/jobs", json={"url": f"{http_server}/slow.bin", "kind": "file"})
    jid = resp.json()["id"]
    wait_for(client, jid, {"downloading", "completed"})
    # Slow server keeps the transfer active while the worker reads.
    paused_request = client.post(f"/api/jobs/{jid}/pause")
    assert paused_request.status_code == 200
    paused = wait_for(client, jid, {"paused", "completed"})
    if paused["status"] == "paused":
        part = folder / "downloads" / ".parts" / f"{jid}.part"
        assert part.exists()
        assert client.post(f"/api/jobs/{jid}/resume").status_code == 200
        done = wait_for(client, jid, {"completed"})
    else:
        done = paused
    assert Path(done["final_path"]).read_bytes() == PAYLOAD


def test_queue_settings_and_batch(api_client, http_server):
    client, _ = api_client
    cfg = client.patch("/api/config", json={"concurrency": 1, "auto_retries": 2, "speed_limit_kbps": 0})
    assert cfg.status_code == 200
    assert cfg.json()["settings"]["concurrency"] == 1
    response = client.post("/api/jobs/batch", json={"urls": [f"{http_server}/a.bin", f"{http_server}/b.bin"], "kind": "file"})
    assert response.status_code == 201
    jobs = response.json()["added"]
    assert len(jobs) == 2
    for job in jobs:
        assert wait_for(client, job["id"], {"completed"})["status"] == "completed"
    assert client.delete("/api/completed").json()["cleared"] == 2
    assert not client.get("/api/jobs").json()["jobs"]


def test_db_restarts_interrupted(tmp_path):
    db_file = tmp_path / "state.db"
    store = Database(db_file)
    stamp = now()
    store.insert_job({"id": "restart", "url": "https://example.com/a.bin", "title": "a", "kind": "file", "status": "downloading", "created_at": stamp, "updated_at": stamp})
    store2 = Database(db_file)
    assert store2.get_job("restart")["status"] == "paused"


def test_segmented_large_file(api_client, http_server):
    client, directory = api_client
    assert client.patch("/api/config", json={"connections_per_download": 4}).status_code == 200
    response = client.post("/api/jobs", json={"url": f"{http_server}/big.bin", "kind": "file"})
    assert response.status_code == 201
    done = wait_for(client, response.json()["id"], {"completed"}, timeout=15)
    assert done["segments"] == 4, "Large range-enabled files should use 4 connections"
    assert done["total_bytes"] == len(BIG_PAYLOAD)
    assert Path(done["final_path"]).read_bytes() == BIG_PAYLOAD
    assert not list((directory / "downloads" / ".parts").glob("*.seg*"))


def test_audio_only_offers_original_best():
    info = {"formats": [{"format_id": "251", "vcodec": "none", "acodec": "opus", "ext": "webm"}]}
    options = video_options(info)
    assert options[0]["value"] == "audio:source"
    assert options[0]["recommended"]


def test_fake_media_probe_and_highest_quality_worker(api_client, http_server, monkeypatch):
    from app import media
    from app import engine

    class FakeYoutubeDL:
        requested_formats = []
        def __init__(self, opts):
            self.opts = opts
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def extract_info(self, url, download=False):
            return {"title": "Sample HD Movie", "extractor_key": "Fake",
                    "thumbnail": "https://example.com/thumb.jpg", "duration": 50,
                    "formats": [
                        {"format_id": "137", "height": 1080, "fps": 30, "ext": "mp4", "vcodec": "avc1", "acodec": "none"},
                        {"format_id": "251", "ext": "webm", "vcodec": "none", "acodec": "opus"},
                    ]}
        def download(self, urls):
            FakeYoutubeDL.requested_formats.append(self.opts["format"])
            filename = Path(self.opts["outtmpl"].replace("%(ext)s", "mkv"))
            filename.write_bytes(b"FAKE MERGED VIDEO")
            for hook in self.opts.get("progress_hooks", []):
                hook({"status": "downloading", "downloaded_bytes": 17, "total_bytes": 17})
                hook({"status": "finished", "downloaded_bytes": 17, "total_bytes": 17})
            return 0
    class FakeModule:
        YoutubeDL = FakeYoutubeDL
    monkeypatch.setattr(media, "yt_dlp", FakeModule)
    monkeypatch.setattr(engine, "yt_dlp", FakeModule)
    client, directory = api_client
    detected = client.post("/api/probe", json={"url": f"{http_server}/video", "kind": "media"})
    assert detected.status_code == 200
    assert detected.json()["quality_options"][0]["value"] == "best"
    created = client.post("/api/jobs", json={"url": f"{http_server}/video", "kind": "media",
                                             "quality": "best", "title": "Sample HD Movie"})
    assert created.status_code == 201
    finished = wait_for(client, created.json()["id"], {"completed"})
    assert Path(finished["final_path"]).read_bytes() == b"FAKE MERGED VIDEO"
    assert FakeYoutubeDL.requested_formats == ["bv*+ba/b/ba"]


def test_two_same_named_files_never_overwrite(api_client, http_server):
    client, _ = api_client
    created = client.post("/api/jobs/batch", json={"urls": [f"{http_server}/same.bin", f"{http_server}/same.bin"], "kind": "file"})
    assert created.status_code == 201
    jobs = [wait_for(client, job["id"], {"completed"}) for job in created.json()["added"]]
    assert len({job["final_path"] for job in jobs}) == 2
    assert all(Path(job["final_path"]).read_bytes() == PAYLOAD for job in jobs)


def test_single_stream_fallback_when_no_ranges(api_client, http_server):
    client, _ = api_client
    created = client.post("/api/jobs", json={"url": f"{http_server}/no-range.bin", "kind": "file"})
    assert created.status_code == 201
    job = wait_for(client, created.json()["id"], {"completed"})
    assert job["segments"] == 0
    assert Path(job["final_path"]).read_bytes() == PAYLOAD


def test_multi_segment_pause_resume(api_client, http_server):
    client, directory = api_client
    created = client.post("/api/jobs", json={"url": f"{http_server}/big-slow.bin", "kind": "file"})
    assert created.status_code == 201
    jid = created.json()["id"]
    wait_for(client, jid, {"downloading"})
    time.sleep(.14)
    assert client.post(f"/api/jobs/{jid}/pause").status_code == 200
    paused = wait_for(client, jid, {"paused", "completed"}, timeout=8)
    if paused["status"] == "paused":
        assert paused["segments"] == 4
        assert list((directory / "downloads" / ".parts").glob("*.seg*"))
        assert client.post(f"/api/jobs/{jid}/resume").status_code == 200
        paused = wait_for(client, jid, {"completed"}, timeout=15)
    assert Path(paused["final_path"]).read_bytes() == BIG_PAYLOAD


def test_folder_picker_create_and_download_to_custom_destination(api_client, http_server):
    client, root = api_client
    base = (root / "downloads").resolve()
    listing = client.get("/api/folders", params={"path": str(base)})
    assert listing.status_code == 200
    assert listing.json()["path"] == str(base)
    assert "roots" in listing.json()
    assert not (root / "downloads" / "Videos").exists()
    created = client.post("/api/folders", json={"parent": str(base), "name": "Videos"})
    assert created.status_code == 201
    target = root / "downloads" / "Videos"
    assert created.json()["path"] == str(target)
    assert any(x["name"] == "Videos" for x in client.get("/api/folders", params={"path": str(base)}).json()["directories"])
    assert client.post("/api/folders/validate", json={"path": str(target)}).status_code == 200

    # A download-level directory must be persisted and honored for real bytes.
    result = client.post("/api/jobs", json={"url": f"{http_server}/file.bin", "kind": "file", "save_dir": str(target)})
    assert result.status_code == 201
    done = wait_for(client, result.json()["id"], {"completed"})
    assert done["save_dir"] == str(target)
    assert Path(done["final_path"]).parent == target
    assert Path(done["final_path"]).read_bytes() == PAYLOAD
    assert client.get(f"/api/jobs/{done['id']}/file").content == PAYLOAD
    assert client.delete(f"/api/jobs/{done['id']}", params={"delete_file": True}).status_code == 200
    assert not Path(done["final_path"]).exists()


def test_default_destination_and_per_job_override(api_client, http_server):
    client, root = api_client
    base = root / "downloads"
    music = base / "Music"
    video = base / "Videos"
    music.mkdir()
    video.mkdir()
    assert client.patch("/api/config", json={"default_save_dir": str(music)}).status_code == 200
    cfg = client.get("/api/config").json()
    assert cfg["download_dir"] == str(music)
    assert cfg["settings"]["default_save_dir"] == str(music)
    a = client.post("/api/jobs", json={"url": f"{http_server}/a.bin", "kind": "file"})
    b = client.post("/api/jobs", json={"url": f"{http_server}/b.bin", "kind": "file", "save_dir": str(video)})
    assert a.status_code == 201 and b.status_code == 201
    assert Path(wait_for(client, a.json()["id"], {"completed"})["final_path"]).parent == music
    assert Path(wait_for(client, b.json()["id"], {"completed"})["final_path"]).parent == video
    batch = client.post("/api/jobs/batch", json={"urls": [f"{http_server}/batch1.bin", f"{http_server}/batch2.bin"],
                                                  "kind": "file", "save_dir": str(music)})
    assert batch.status_code == 201
    for job in batch.json()["added"]:
        assert Path(wait_for(client, job["id"], {"completed"})["final_path"]).parent == music
    # The preference persists across Database reloads.
    assert Database(root / "data" / "jobs.db").get_settings()["default_save_dir"] == str(music)


def test_destination_is_allowlisted_and_cannot_use_partials(api_client, tmp_path, http_server):
    client, root = api_client
    external = root / "private-outside"
    external.mkdir()
    base = root / "downloads"
    link = base / "escape"
    link.symlink_to(external, target_is_directory=True)
    invalid = [str(external), str(link), str(base / ".parts"), "/missing-folder-that-does-not-exist-123"]
    for path in invalid:
        assert client.post("/api/jobs", json={"url": f"{http_server}/file.bin", "kind": "file", "save_dir": path}).status_code == 400
        assert client.patch("/api/config", json={"default_save_dir": path}).status_code == 400
        assert client.post("/api/folders/validate", json={"path": path}).status_code == 400
    assert client.post("/api/folders", json={"parent": str(base), "name": "../escape"}).status_code == 400
    assert client.get("/api/folders", params={"path": str(external)}).status_code == 400
    assert client.get("/api/jobs").json()["jobs"] == []


def test_added_allowed_root_from_environment(tmp_path, monkeypatch):
    extra = tmp_path / "Mounted Drive"
    extra.mkdir()
    monkeypatch.setenv("FLUX_ALLOWED_DOWNLOAD_ROOTS", str(extra))
    app = create_app(tmp_path / "db" / "store.db", tmp_path / "downloads")
    with TestClient(app) as client:
        roots = client.get("/api/folders").json()["roots"]
        assert str(extra) in [root["path"] for root in roots]
        assert client.post("/api/folders/validate", json={"path": str(extra)}).status_code == 200


def test_media_quality_lists_and_exact_audio_options():
    from app.media import audio_mp3_quality, separate_quality_options
    info = {"formats": [
        {"format_id": "v8", "height": 4320, "fps": 60, "vcodec": "av01", "acodec": "none", "ext": "webm"},
        {"format_id": "v4", "height": 2160, "fps": 30, "vcodec": "vp9", "acodec": "none", "ext": "webm"},
        {"format_id": "v720", "height": 720, "vcodec": "avc1", "acodec": "none", "ext": "mp4"},
        {"format_id": "a251", "vcodec": "none", "acodec": "opus", "abr": 160, "ext": "webm"},
        {"format_id": "a140", "vcodec": "none", "acodec": "mp4a", "abr": 128, "ext": "m4a"},
    ]}
    listing = separate_quality_options(info)
    assert listing["video"][0]["value"] == "best"
    assert [x["value"] for x in listing["video"] if x["value"].startswith("res:")] == ["res:4320", "res:2160", "res:720"]
    assert "8K" in listing["video"][1]["label"]
    assert [x["value"] for x in listing["audio"][:3]] == ["audio:source", "audio:fmt:a251", "audio:fmt:a140"]
    assert "160 kbps" in listing["audio"][1]["label"]
    assert "128 kbps" in listing["audio"][2]["label"]
    assert "fmt:v8:v" in [x["value"] for x in listing["video"]]
    assert choose_format("audio:fmt:a140") == ("a140", False)
    assert choose_format("audio:mp3:192") == ("ba/b", True)
    assert audio_mp3_quality("audio:mp3:320") == "320"
    assert audio_mp3_quality("audio:mp3") == "0"
    for bad in ["audio:fmt:../../tmp", "audio:mp3:999", "res:99999", "res:0", "audio:mp3:-2"]:
        with pytest.raises(ValueError):
            choose_format(bad)


def test_audio_only_source_metadata_without_video():
    from app.media import separate_quality_options
    audio = separate_quality_options({"formats": [
        {"format_id": "opus", "vcodec": "none", "acodec": "opus", "abr": 158, "ext": "webm"},
    ]})
    assert audio["video"] == []
    assert audio["audio"][0]["value"] == "audio:source"
    assert audio["audio"][1]["value"] == "audio:fmt:opus"


def test_fake_media_audio_only_source_and_mp3_bitrate(api_client, http_server, monkeypatch):
    from app import engine, media

    class FakeDownloader:
        used = []
        def __init__(self, opts): self.opts = opts
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def download(self, urls):
            FakeDownloader.used.append(self.opts)
            # Simulate the final file after yt-dlp and optional FFmpeg processing.
            ext = "mp3" if any(x.get("preferredcodec") == "mp3" for x in self.opts.get("postprocessors", [])) else "m4a"
            Path(self.opts["outtmpl"].replace("%(ext)s", ext)).write_bytes(b"FAKE AUDIO ONLY")
            return 0
        def extract_info(self, url, download=False):
            return {"title": "Sample podcast", "formats": [
                {"format_id": "140", "vcodec": "none", "acodec": "mp4a", "abr": 128, "ext": "m4a"},
            ]}

    class Mod:
        YoutubeDL = FakeDownloader

    monkeypatch.setattr(media, "yt_dlp", Mod)
    monkeypatch.setattr(engine, "yt_dlp", Mod)
    client, root = api_client
    p = client.post("/api/probe", json={"url": f"{http_server}/audio", "kind": "media"})
    assert p.status_code == 200
    assert p.json()["supports_video"] is False
    assert p.json()["audio_quality_options"][1]["value"] == "audio:fmt:140"
    sub = root / "downloads" / "Podcasts"
    sub.mkdir()
    a = client.post("/api/jobs", json={"url": f"{http_server}/audio", "kind": "media", "quality": "audio:fmt:140", "save_dir": str(sub), "title": "Podcast"})
    assert a.status_code == 201
    done = wait_for(client, a.json()["id"], {"completed"})
    assert done["final_path"].endswith(".m4a")
    assert Path(done["final_path"]).read_bytes() == b"FAKE AUDIO ONLY"
    assert FakeDownloader.used[-1]["format"] == "140"
    assert FakeDownloader.used[-1].get("postprocessors") is None

    b = client.post("/api/jobs", json={"url": f"{http_server}/audio", "kind": "media", "quality": "audio:mp3:192", "title": "Podcast"})
    assert b.status_code == 201
    done2 = wait_for(client, b.json()["id"], {"completed"})
    assert done2["final_path"].endswith(".mp3")
    assert FakeDownloader.used[-1]["postprocessors"][0]["preferredquality"] == "192"
    assert FakeDownloader.used[-1]["postprocessors"][0]["preferredcodec"] == "mp3"


def test_cross_filesystem_copy_is_atomic_when_move_returns_exdev(tmp_path, monkeypatch):
    import errno
    import os
    from app.engine import DownloadManager
    original = os.replace
    source = tmp_path / "part.bin"
    dest = tmp_path / "Final.bin"
    source.write_bytes(b"some bytes")
    def simulate_cross_disk(src, dst):
        if Path(src) == source:
            raise OSError(errno.EXDEV, "Cross-device link")
        return original(src, dst)
    monkeypatch.setattr("app.engine.os.replace", simulate_cross_disk)
    DownloadManager._relocate(source, dest)
    assert dest.read_bytes() == b"some bytes"
    assert not source.exists()
    assert not list(tmp_path.glob(".*.tmp"))
