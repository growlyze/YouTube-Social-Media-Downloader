"""Server-side folder browsing for the in-page "Choose Download Folder" window.

Browsers never reveal real local paths to web pages, so the page asks this
local backend to list drives and sub-folders. Only folder *names* are
returned (never files or file contents), and the routes using this module are
reachable only from the app itself (127.0.0.1 + Host check + CSRF token).
"""

import ctypes
import os
import re
import string
from pathlib import Path

MAX_ENTRIES = 2000
_INVALID_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}


class BrowseError(ValueError):
    pass


def list_drives() -> list[dict]:
    """Drive roots that exist on this PC, e.g. C:\\ and D:\\."""
    if os.name != "nt":
        return [{"name": "/", "path": "/"}]
    try:
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        letters = [letter for i, letter in enumerate(string.ascii_uppercase) if mask >> i & 1]
    except (AttributeError, OSError):
        letters = [l for l in string.ascii_uppercase if Path(f"{l}:\\").exists()]
    drives = []
    for letter in letters:
        root = f"{letter}:\\"
        try:
            ready = os.path.isdir(root)          # empty card readers / DVD drives are not ready
        except OSError:
            ready = False
        if ready:
            drives.append({"name": f"{letter}:", "path": root})
    return drives


def _check_dir(raw: str) -> Path:
    raw = str(raw or "").strip().strip('"')
    if not raw:
        raise BrowseError("No folder given.")
    path = Path(raw)
    if not path.is_absolute() or not path.anchor:
        raise BrowseError("Folder must be a full path such as D:\\Videos.")
    if not path.is_dir():
        raise BrowseError(f"Folder not found: {raw}")
    return path.resolve()


def list_folder(raw: str) -> dict:
    """Return the sub-folders of `raw` (names and paths only)."""
    path = _check_dir(raw)
    folders, truncated = [], False
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                name = entry.name
                if name.startswith(("$", ".")) or name in ("System Volume Information", "Recovery"):
                    continue
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                if len(folders) >= MAX_ENTRIES:
                    truncated = True
                    break
                folders.append({"name": name, "path": str(path / name)})
    except PermissionError:
        raise BrowseError(f"Windows does not allow listing {path}.")
    except OSError as exc:
        raise BrowseError(f"Cannot open {path}: {exc.strerror or exc}")
    folders.sort(key=lambda f: f["name"].lower())
    parent = str(path.parent) if path.parent != path else None
    return {"path": str(path), "parent": parent, "folders": folders, "truncated": truncated}


def create_folder(parent_raw: str, name: str) -> str:
    """Create `name` inside `parent_raw` and return the new folder's path."""
    parent = _check_dir(parent_raw)
    name = str(name or "").strip()
    if not name or name in (".", "..") or _INVALID_NAME.search(name) or name.endswith((".", " ")):
        raise BrowseError('Folder names cannot be empty, end with a dot/space, or contain < > : " / \\ | ? *')
    if name.upper().split(".")[0] in _RESERVED:
        raise BrowseError(f"'{name}' is a reserved name on Windows.")
    if len(name) > 120:
        raise BrowseError("Folder name is too long.")
    target = parent / name
    try:
        target.mkdir(exist_ok=True)
    except OSError as exc:
        raise BrowseError(f"Cannot create the folder: {exc.strerror or exc}")
    return str(target)
