"""Validate public HTTP(S) URLs and sanitize downloaded filenames."""
from __future__ import annotations

import ipaddress
import re
import socket
from pathlib import Path
from urllib.parse import unquote, urlparse

from .config import ALLOW_PRIVATE_HOSTS


class UnsafeURL(ValueError):
    pass


def validate_url(value: str) -> str:
    """Reject local/private addresses to reduce server-side request forgery risk.

    This app is local-only by default. LAN endpoints can be deliberately enabled
    with FLUX_ALLOW_PRIVATE_HOSTS=1; do not expose it on a public network.
    """
    if not isinstance(value, str):
        raise UnsafeURL("Please enter a valid URL.")
    url = value.strip()
    if len(url) > 4096 or any(c in url for c in "\r\n\0\t"):
        raise UnsafeURL("Invalid or excessively long URL.")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise UnsafeURL("Only HTTP and HTTPS links are supported.")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeURL("URLs containing a username or password are not supported.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise UnsafeURL("Invalid port in URL.") from exc
    if port == 0:
        raise UnsafeURL("Invalid port in URL.")
    host = parsed.hostname.strip(".").lower()
    if not host:
        raise UnsafeURL("Please enter a valid URL.")
    if not ALLOW_PRIVATE_HOSTS:
        if host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
            raise UnsafeURL("Local network addresses are blocked by default.")
        try:
            results = socket.getaddrinfo(host, port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise UnsafeURL("Unable to resolve the link's hostname.") from exc
        if not results:
            raise UnsafeURL("Could not resolve the requested host.")
        for result in results:
            ip = ipaddress.ip_address(result[4][0].split("%")[0])
            if not ip.is_global:
                raise UnsafeURL("Private, local and reserved network addresses are blocked.")
    return url


BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]+')


def safe_filename(name: str, fallback: str = "download") -> str:
    name = unquote(str(name or ""))
    name = BAD_NAME.sub("_", name).strip(". \t\n")
    name = re.sub(r"\.{2,}", "_", name)
    name = re.sub(r"_{2,}", "_", name)
    name = re.sub(r"\s+", " ", name)
    if name in ("", ".", ".."):
        name = fallback
    # Cap both filename and extension to avoid path / filesystem edge cases.
    suffix = Path(name).suffix[:18]
    stem = name[: max(1, 165 - len(suffix))] if not suffix else name[:-len(Path(name).suffix)][: 165 - len(suffix)]
    return (stem or fallback) + suffix


def unique_destination(directory: Path, name: str) -> Path:
    candidate = directory / safe_filename(name)
    root, ext = candidate.stem, candidate.suffix
    index = 1
    while candidate.exists():
        candidate = directory / f"{root} ({index}){ext}"
        index += 1
    return candidate
