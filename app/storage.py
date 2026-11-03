"""Local-only, allowlisted output folder browsing and destination validation.

A browser cannot grant a Python web server permission to write to an arbitrary
client-side folder. These paths belong to the server machine. Additional disks
or directories can be explicitly mounted / allowlisted via the environment.
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path


class DownloadFolders:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir.expanduser().resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        candidates = [("App downloads", self.base_dir), ("Your home folder", Path.home())]
        for raw in os.getenv("FLUX_ALLOWED_DOWNLOAD_ROOTS", "").split(os.pathsep):
            if raw.strip():
                candidates.append(("Additional location", Path(raw.strip()).expanduser()))
        roots: list[tuple[str, Path]] = []
        for name, candidate in candidates:
            path = candidate.resolve()
            if path.is_dir() and path not in [root for _, root in roots]:
                roots.append((name, path))
        self.roots = roots
        self.protected = (self.base_dir / ".parts").resolve()

    def resolve(self, value: str | None, *, writable: bool = True) -> Path:
        if value is None or value == "":
            return self.base_dir
        if not isinstance(value, str) or len(value) > 4096 or "\0" in value:
            raise ValueError("Invalid download folder path.")
        given = Path(value).expanduser()
        if not given.is_absolute():
            raise ValueError("Select an absolute folder path.")
        try:
            path = given.resolve(strict=True)
        except (OSError, RuntimeError):
            raise ValueError("This folder does not exist. Use New folder to create it.") from None
        if not path.is_dir():
            raise ValueError("The selected path is not a folder.")
        if not any(path == root or root in path.parents for _, root in self.roots):
            raise ValueError("This folder is outside the allowed locations. Add its parent to FLUX_ALLOWED_DOWNLOAD_ROOTS.")
        if path == self.protected or self.protected in path.parents:
            raise ValueError("The app's temporary download folder cannot be selected.")
        if writable:
            try:
                # Test real write capability, not just os.access (unreliable under ACLs).
                with tempfile.TemporaryFile(dir=path):
                    pass
            except OSError:
                raise ValueError("The selected folder is not writable by the download engine.") from None
        return path

    def browse(self, value: str | None = None) -> dict:
        current = self.resolve(value, writable=False)
        parent = current.parent
        try:
            parent_path = str(self.resolve(str(parent), writable=False)) if parent != current else None
        except ValueError:
            parent_path = None
        directories = []
        try:
            children = sorted(current.iterdir(), key=lambda child: child.name.casefold())
        except OSError:
            raise ValueError("Cannot read this folder.") from None
        for child in children:
            # Hide hidden/system directories and the app's internal temporary files.
            if len(directories) >= 250:
                break
            if child.name.startswith("."):
                continue
            try:
                folder = self.resolve(str(child), writable=False)
            except (ValueError, OSError):
                continue
            if folder == current:
                continue
            directories.append({"name": child.name, "path": str(folder)})
        return {
            "path": str(current), "parent": parent_path,
            "roots": [{"name": name, "path": str(root)} for name, root in self.roots],
            "directories": directories,
        }

    def create(self, parent: str, name: str) -> dict:
        base = self.resolve(parent)
        name = name.strip() if isinstance(name, str) else ""
        if (not name or name in (".", "..") or name.startswith(".")
                or name.endswith((".", " ")) or len(name) > 100
                or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)):
            raise ValueError("Use a folder name without slashes, hidden prefixes or special characters.")
        folder = base / name
        try:
            folder.mkdir()
        except FileExistsError:
            raise ValueError("A folder with that name already exists.") from None
        except OSError:
            raise ValueError("Could not create the folder here.") from None
        return {"path": str(self.resolve(str(folder)))}
