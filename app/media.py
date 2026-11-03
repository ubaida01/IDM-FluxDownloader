"""Media detection, real quality menus, playlist extraction, and format selection.

yt-dlp is an optional import at runtime, but it is included in requirements.txt.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests

from .config import MAX_PLAYLIST_ITEMS, USER_AGENT
from .security import UnsafeURL, safe_filename, validate_url

try:
    import yt_dlp
except ImportError:  # permits direct-file downloads in minimal environments
    yt_dlp = None

DIRECT_EXTENSIONS = {
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".iso", ".exe", ".msi",
    ".dmg", ".apk", ".deb", ".rpm", ".pdf", ".docx", ".xlsx", ".pptx", ".epub",
    ".txt", ".csv", ".json", ".xml", ".jpg", ".jpeg", ".png", ".webp", ".gif",
    ".mp4", ".mkv", ".webm", ".mov", ".mp3", ".m4a", ".flac", ".wav",
}
MEDIA_DOMAINS = (
    "youtube.com", "youtu.be", "vimeo.com", "dailymotion.com", "tiktok.com",
    "twitch.tv", "soundcloud.com", "facebook.com", "instagram.com", "bilibili.com",
    "reddit.com", "rumble.com", "bandcamp.com", "x.com", "twitter.com",
)


def looks_like_direct_file(url: str) -> bool:
    return Path(urlparse(url).path.lower()).suffix in DIRECT_EXTENSIONS


def known_media_host(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == domain or host.endswith("." + domain) for domain in MEDIA_DOMAINS)


def safe_request(method: str, url: str, **kwargs) -> requests.Response:
    """Follow at most 6 redirects, validating each hop before the next request."""
    current = validate_url(url)
    session = requests.Session()
    session.trust_env = False  # no untrusted proxy environment redirecting to LAN
    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity", **kwargs.pop("headers", {})}
    try:
        for _ in range(7):
            response = session.request(
                method, current, headers=headers, stream=True,
                allow_redirects=False, timeout=(10, 25), **kwargs
            )
            if response.status_code in (301, 302, 303, 307, 308):
                from urllib.parse import urljoin
                new_url = urljoin(current, response.headers.get("Location", ""))
                response.close()
                current = validate_url(new_url)
                continue
            return response
        raise RuntimeError("Too many redirects. Maximum: 6.")
    finally:
        session.close()


def direct_metadata(url: str) -> dict:
    name = safe_filename(unquote(Path(urlparse(url).path).name), "download")
    size = None
    mime = ""
    try:
        response = safe_request("HEAD", url)
        try:
            if response.status_code < 400:
                length = response.headers.get("Content-Length", "")
                size = int(length) if length.isdecimal() else None
                mime = response.headers.get("Content-Type", "").split(";")[0]
                cd = response.headers.get("Content-Disposition", "")
                candidate = filename_from_header(cd)
                if candidate:
                    name = safe_filename(candidate)
        finally:
            response.close()
    except (requests.RequestException, RuntimeError, UnsafeURL):
        # HEAD often isn't supported. The actual GET will report errors precisely.
        pass
    return {"kind": "file", "title": name, "filename": name, "size": size,
            "mime": mime, "quality_options": [{"value": "original", "label": "Original file", "detail": "Full source quality"}],
            "quality": "original", "thumbnail": None, "duration": None}


def filename_from_header(value: str) -> str | None:
    from urllib.parse import unquote
    match = re.search(r"filename\*\s*=\s*(?:UTF-8'')?([^;]+)", value, re.I)
    if match:
        return unquote(match.group(1).strip(' "\''))
    match = re.search(r'filename\s*=\s*(?:"([^"]+)"|([^;]+))', value, re.I)
    return (match.group(1) or match.group(2)).strip() if match else None


def separate_quality_options(info: dict) -> dict[str, list[dict]]:
    """Two explicit lists: actual video resolutions and actual/or converted audio.

    Source entries always come from the extractor; MP3 bitrates are *targets*
    and must never be represented as additional source audio fidelity.
    """
    formats = info.get("formats") or []
    video = [f for f in formats if f.get("vcodec") not in (None, "none") and f.get("height")]
    audio = [f for f in formats if f.get("vcodec") in (None, "none") and f.get("acodec") not in (None, "none")]
    heights = sorted({int(f["height"]) for f in video if 144 <= int(f["height"]) <= 8640}, reverse=True)
    maximum = max(heights, default=int(info.get("height") or 0))
    video_choices: list[dict] = []
    if video or (info.get("vcodec") not in (None, "none") and maximum):
        video_choices.append({
            "value": "best", "label": f"Best available video · {maximum}p max" if maximum else "Best available video",
            "detail": "Best available picture + best available audio, merged when necessary",
            "recommended": True,
        })
        resolution_labels = {4320: "8K UHD", 2160: "4K UHD", 1440: "QHD", 1080: "Full HD", 720: "HD"}
        for height in heights[:22]:
            nick = resolution_labels.get(height)
            video_choices.append({
                "value": f"res:{height}",
                "label": f"{height}p" + (f" · {nick}" if nick else ""),
                "detail": "Highest available stream at or below this resolution, with audio",
                "group": "Resolutions",
            })
        # Individual video tracks — includes frame rate and source encoding.
        seen_ids = set()
        for fmt in sorted(video, key=lambda f: (
                int(f.get("height") or 0), float(f.get("fps") or 0), float(f.get("tbr") or 0)), reverse=True):
            format_id = str(fmt.get("format_id") or "")
            if format_id in seen_ids or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", format_id):
                continue
            seen_ids.add(format_id)
            fps = round(float(fmt.get("fps") or 0))
            h = int(fmt.get("height") or 0)
            codec = str(fmt.get("vcodec") or "unknown").split(".")[0].upper()
            ext = str(fmt.get("ext") or "file").upper()
            has_audio = fmt.get("acodec") not in (None, "none")
            size = fmt.get("filesize") or fmt.get("filesize_approx")
            detail = f"{codec} · {'includes audio' if has_audio else 'best audio added'}"
            if size:
                detail += f" · ~{round(size / 1048576)} MB video"
            video_choices.append({
                "value": f"fmt:{format_id}:{'av' if has_audio else 'v'}",
                "label": f"{h}p · {fps} fps · {ext}" if fps else f"{h}p · {ext}",
                "detail": detail,
                "group": "Specific formats",
            })
            if len(seen_ids) >= 40:
                break

    audio_choices: list[dict] = [{
        "value": "audio:source", "label": "Best original audio · no quality loss",
        "detail": "Preserve the best source stream. Recommended for fidelity.",
        "recommended": True,
    }]
    # Exact audio-only streams (not converted): the measured/source-reported
    # bitrates are shown individually, rather than invented bitrate tiers.
    seen_audio_ids = set()
    for fmt in sorted(audio, key=lambda f: (float(f.get("abr") or f.get("tbr") or 0),
                                                  float(f.get("asr") or 0)), reverse=True):
        format_id = str(fmt.get("format_id") or "")
        if format_id in seen_audio_ids or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", format_id):
            continue
        seen_audio_ids.add(format_id)
        bitrate = fmt.get("abr") or fmt.get("tbr")
        ext = str(fmt.get("ext") or "audio").upper()
        codec = str(fmt.get("acodec") or "unknown").split(".")[0].upper()
        bitrate_label = f"{round(float(bitrate))} kbps" if bitrate else "Bitrate not reported"
        size = fmt.get("filesize") or fmt.get("filesize_approx")
        size_label = f" · ~{round(size / 1048576)} MB" if size else ""
        audio_choices.append({
            "value": f"audio:fmt:{format_id}",
            "label": f"{bitrate_label} · {ext}",
            "detail": f"Original {codec} stream · no transcoding{size_label}",
            "group": "Source audio streams",
        })
        if len(seen_audio_ids) >= 40:
            break
    audio_choices.append({
        "value": "audio:mp3", "label": "MP3 · best VBR",
        "detail": "Convert to high-quality variable-bitrate MP3 (FFmpeg required)",
        "group": "MP3 conversion",
    })
    for kbps in (320, 256, 192, 128, 96, 64):
        audio_choices.append({
            "value": f"audio:mp3:{kbps}", "label": f"MP3 · {kbps} kbps",
            "detail": "Converted output bitrate · cannot improve the source audio",
            "group": "MP3 conversion",
        })
    return {"video": video_choices, "audio": audio_choices}


def video_options(info: dict) -> list[dict]:
    """Backwards-compatible combined list; new UI uses separate quality menus."""
    grouped = separate_quality_options(info)
    return grouped["video"] + grouped["audio"]


def yt_metadata(url: str) -> dict:
    if yt_dlp is None:
        raise RuntimeError("Media support is not installed. Install requirements.txt to enable yt-dlp.")
    opts = {
        "quiet": True, "no_warnings": True, "skip_download": True,
        "noplaylist": True, "socket_timeout": 20,
        "ignoreerrors": False, "retries": 2,
        "extract_flat": False,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise RuntimeError("No downloadable streams were found at this URL.")
    if info.get("_type") in ("playlist", "multi_video"):
        entries = info.get("entries") or []
        info = next((e for e in entries if e), None)
        if info is None:
            raise RuntimeError("This playlist is empty. Try the playlist import option.")
    formats = info.get("formats") or []
    if not formats and not info.get("url"):
        raise RuntimeError("No downloadable formats were returned by this site.")
    # Generic extractor can expose regular files. Still show media options if a video exists.
    quality_lists = separate_quality_options(info)
    options = quality_lists["video"] + quality_lists["audio"]
    return {
        "kind": "media",
        "title": info.get("title") or "Untitled video",
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"),
        "size": info.get("filesize") or info.get("filesize_approx"),
        "uploader": info.get("uploader") or info.get("channel") or info.get("extractor_key"),
        "extractor": info.get("extractor_key"),
        "max_height": max([int(f.get("height") or 0) for f in formats], default=int(info.get("height") or 0)),
        "format_count": len(formats),
        "quality_options": options,
        "video_quality_options": quality_lists["video"],
        "audio_quality_options": quality_lists["audio"],
        "supports_video": bool(quality_lists["video"]),
        "quality": options[0]["value"] if options else "audio:source",
    }


def probe_url(url: str, force_kind: str = "auto") -> dict:
    url = validate_url(url)
    if force_kind == "file" or (force_kind == "auto" and looks_like_direct_file(url) and not known_media_host(url)):
        return direct_metadata(url)
    if force_kind == "media" or known_media_host(url):
        return yt_metadata(url)
    if yt_dlp is not None:
        try:
            return yt_metadata(url)
        except Exception:
            pass
    metadata = direct_metadata(url)
    if metadata.get("mime", "").startswith("text/html"):
        raise RuntimeError("This looks like a web page, not a downloadable file. Select Media if the site is supported.")
    return metadata


def choose_format(quality: str) -> tuple[str, bool]:
    """Return a validated yt-dlp selector and whether MP3 extraction is needed."""
    if quality in ("best", ""):
        return "bv*+ba/b/ba", False
    if quality == "audio:source":
        return "ba/b", False
    if quality == "audio:mp3" or re.fullmatch(r"audio:mp3:(320|256|192|128|96|64)", quality):
        return "ba/b", True
    match = re.fullmatch(r"audio:fmt:([A-Za-z0-9_.-]{1,80})", quality)
    if match:
        return match.group(1), False
    match = re.fullmatch(r"res:([1-9][0-9]{1,4})", quality)
    if match and 144 <= int(match.group(1)) <= 8640:
        res = match.group(1)
        return f"bv*[height<={res}]+ba/b[height<={res}]", False
    match = re.fullmatch(r"fmt:([A-Za-z0-9_.-]{1,80}):(v|av)", quality)
    if match:
        fmt_id, stream_kind = match.groups()
        return (f"{fmt_id}+ba/{fmt_id}" if stream_kind == "v" else fmt_id), False
    raise ValueError("Invalid media quality option.")


def audio_mp3_quality(quality: str) -> str:
    """yt-dlp FFmpeg preferredquality: 0 = best VBR, or target CBR kbps."""
    if quality == "audio:mp3":
        return "0"
    match = re.fullmatch(r"audio:mp3:(320|256|192|128|96|64)", quality)
    if match:
        return match.group(1)
    raise ValueError("Invalid MP3 quality.")


def playlist_entries(url: str, limit: int) -> list[dict]:
    validate_url(url)
    if not yt_dlp:
        raise RuntimeError("Install yt-dlp for playlist support.")
    limit = max(1, min(limit, MAX_PLAYLIST_ITEMS))
    opts = {
        "quiet": True, "no_warnings": True, "extract_flat": "in_playlist",
        "skip_download": True, "noplaylist": False, "playlistend": limit,
        "ignoreerrors": True, "socket_timeout": 20,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise RuntimeError("Playlist could not be read.")
    if info.get("_type") not in ("playlist", "multi_video"):
        raise RuntimeError("This URL does not appear to contain a playlist.")
    output = []
    for entry in (info.get("entries") or [])[:limit]:
        if not entry:
            continue
        target = entry.get("webpage_url") or entry.get("url") or ""
        if not target.startswith(("https://", "http://")):
            video_id = entry.get("id", "")
            if entry.get("ie_key", "").lower().startswith("youtube") and video_id:
                target = f"https://www.youtube.com/watch?v={video_id}"
            else:
                continue
        try:
            validate_url(target)
        except UnsafeURL:
            continue
        output.append({
            "url": target,
            "title": entry.get("title") or "Playlist video",
            "duration": entry.get("duration"),
            "thumbnail": entry.get("thumbnail"),
        })
    return output


def capabilities() -> dict:
    return {"yt_dlp": yt_dlp is not None, "ffmpeg": shutil.which("ffmpeg") is not None}
