"""Native Windows folder picker and download-destination validation.

The dialog runs in a short-lived child process (Tk's chooser, which on
Windows is the standard "Select Folder" dialog) so GUI code never runs inside
a Flask request thread. It opens on this PC's desktop only; the browser just
receives the chosen path. Choosing a folder does NOT save it: the caller must
confirm it, then `validate_destination` checks it before it is stored.
"""

import json
import logging
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path

_dialog_lock = threading.Lock()
log = logging.getLogger("folder-picker")

# Windows only lets the foreground app raise windows, so a dialog started by the
# background server can open *behind* the browser. To avoid that, the dialog is
# owned by a tiny, fully transparent, always-on-top window (owned dialogs stay
# above their owner), and the process briefly attaches to the foreground
# window's input thread so SetForegroundWindow is permitted.
_DIALOG_SCRIPT = r"""
import ctypes, json, sys, tkinter
from tkinter import filedialog

root = tkinter.Tk()
root.overrideredirect(True)
root.geometry("1x1+200+200")
root.attributes("-alpha", 0.0)
root.attributes("-topmost", True)
root.update()

try:
    user32 = ctypes.windll.user32
    hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
    fg = user32.GetForegroundWindow()
    fg_thread = user32.GetWindowThreadProcessId(fg, None)
    me = ctypes.windll.kernel32.GetCurrentThreadId()
    user32.AttachThreadInput(me, fg_thread, True)
    user32.SetForegroundWindow(hwnd)
    user32.BringWindowToTop(hwnd)
    user32.AttachThreadInput(me, fg_thread, False)
except Exception:
    pass
root.lift()
root.focus_force()

path = filedialog.askdirectory(parent=root, initialdir=sys.argv[1] or None,
                               title="Choose where downloaded videos are saved",
                               mustexist=False)
root.destroy()
print(json.dumps({"path": path or None}))
"""


class PickerBusy(Exception):
    """A folder dialog is already open."""


class PickerUnavailable(Exception):
    """The native dialog could not be shown."""


def pick_folder(initial_dir: str = "", timeout: int = 600) -> str | None:
    """Show the native folder dialog; return the chosen path or None if cancelled."""
    if os.name != "nt":
        raise PickerUnavailable("The native folder picker is only available on Windows.")
    if not _dialog_lock.acquire(blocking=False):
        log.info("Folder picker requested while one is already open")
        raise PickerBusy("A folder picker is already open on your desktop. Look for it in the "
                         "taskbar (it may be behind the browser), or close it and try again.")
    try:
        log.info("Opening folder picker")
        initial = initial_dir if initial_dir and Path(initial_dir).is_dir() else ""
        try:
            result = subprocess.run([sys.executable, "-I", "-c", _DIALOG_SCRIPT, initial],
                                    capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise PickerUnavailable("The folder picker was left open too long and was closed.")
        except OSError as exc:
            raise PickerUnavailable(f"Could not start the folder picker: {exc}")
        if result.returncode != 0:
            log.error("Folder picker failed: %s", result.stderr.strip()[-500:])
            raise PickerUnavailable("The folder picker failed: " + (result.stderr.strip()[-300:] or "unknown error"))
        try:
            path = json.loads(result.stdout.strip().splitlines()[-1])["path"]
        except (ValueError, IndexError, KeyError):
            log.error("Folder picker returned unexpected output: %r", result.stdout[-300:])
            raise PickerUnavailable("The folder picker returned an unexpected result.")
        log.info("Folder picker closed: %s", path or "cancelled")
        return str(Path(path)) if path else None
    finally:
        _dialog_lock.release()


def validate_destination(raw: str) -> tuple[str | None, str | None]:
    """Check a download folder. Returns (resolved path, None) or (None, error).

    Creates the folder if it is missing and proves it is writable by creating
    and deleting a small test file.
    """
    raw = str(raw or "").strip().strip('"')
    if not raw:
        return None, "Choose a download folder."
    path = Path(raw)
    if not path.is_absolute() or not path.anchor:
        return None, "Download folder must be a full path such as D:\\Videos\\YouTube."
    if os.name == "nt" and not Path(path.anchor).exists():
        return None, f"Drive {path.anchor} is not available."
    if path.exists() and not path.is_dir():
        return None, "That path is a file, not a folder."
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return None, f"Cannot create the folder: {exc.strerror or exc}"
    probe = path / f".ytbulk-write-test-{uuid.uuid4().hex}"
    try:
        probe.write_bytes(b"ok")
        probe.unlink()
    except OSError as exc:
        return None, f"The folder is not writable: {exc.strerror or exc}"
    return str(path.resolve()), None
