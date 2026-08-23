"""Workbook and folder management on disk.

Owns the filesystem so `store.py` can stay about rows and `excel.py` about
rendering. Its most important job is `resolve_path`: every path in this module
arrives from a browser and is untrusted until proven to live inside DATA_DIR.
"""

import os
import shutil
from datetime import datetime
from pathlib import Path

from app import excel

DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
TRASH_DIR_NAME = ".trash"

# Never listed, never a valid target: the database is not a workbook, and the
# trash is a backup the user reaches through Finder, not through the app.
RESERVED = {"leads.db", TRASH_DIR_NAME}


class InvalidPath(ValueError):
    """A client-supplied path escaped DATA_DIR or named a reserved entry."""


def resolve_path(
    rel: str, *, must_exist: bool = False, require_xlsx: bool = False
) -> Path:
    """Validate an untrusted relative path and return it resolved inside DATA_DIR.

    This is a trust boundary. `/workbooks/download` streams whatever this
    returns, so without it the endpoint serves any file the process can read.
    """
    if not rel or not rel.strip():
        raise InvalidPath("A path is required.")
    rel = rel.strip().replace("\\", "/")

    if rel.startswith("/") or ":" in rel.split("/")[0]:
        raise InvalidPath("Absolute paths are not allowed.")

    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise InvalidPath("Path traversal is not allowed.")
    if parts[0] in RESERVED:
        raise InvalidPath(f"{parts[0]!r} is reserved.")
    if require_xlsx and not parts[-1].endswith(".xlsx"):
        raise InvalidPath("A workbook must end in .xlsx")

    base = DATA_DIR.resolve()
    base.mkdir(parents=True, exist_ok=True)
    # resolve() follows symlinks before the containment check below, so a
    # symlink pointing outside DATA_DIR is caught here too.
    target = (base / "/".join(parts)).resolve()
    if target != base and base not in target.parents:
        raise InvalidPath("Path escapes the data directory.")
    if must_exist and not target.exists():
        raise InvalidPath(f"{rel!r} does not exist.")
    return target


def tree() -> dict:
    """The workbook tree the sidebar renders. Reserved and dotted names omitted."""
    base = DATA_DIR.resolve()
    base.mkdir(parents=True, exist_ok=True)
    return {"name": "", "path": "", "type": "folder", "children": _children(base, base)}


def _children(folder: Path, base: Path) -> list[dict]:
    out: list[dict] = []
    for entry in sorted(folder.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        if entry.name in RESERVED or entry.name.startswith("."):
            continue
        rel = entry.relative_to(base).as_posix()
        if entry.is_dir():
            out.append(
                {
                    "name": entry.name,
                    "path": rel,
                    "type": "folder",
                    "children": _children(entry, base),
                }
            )
        elif entry.suffix == ".xlsx":
            out.append(
                {
                    "name": entry.name,
                    "path": rel,
                    "type": "workbook",
                    "size": entry.stat().st_size,
                }
            )
    return out


def create_workbook(rel: str) -> str:
    target = resolve_path(rel, require_xlsx=True)
    if target.exists():
        raise InvalidPath(f"{rel!r} already exists.")
    target.parent.mkdir(parents=True, exist_ok=True)
    # Reuse excel.sync so a new workbook gets the same headers, freeze pane,
    # autofilter and widths as one the app has been writing to for months.
    excel.sync([], target)
    return _rel(target)


def create_folder(rel: str) -> str:
    target = resolve_path(rel)
    if target.exists():
        raise InvalidPath(f"{rel!r} already exists.")
    target.mkdir(parents=True)
    return _rel(target)


def rename_or_move(src: str, dst: str) -> str:
    source = resolve_path(src, must_exist=True)
    target = resolve_path(dst, require_xlsx=source.is_file())
    if target.exists():
        raise InvalidPath(f"{dst!r} already exists.")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))
    return _rel(target)


def delete_workbook(rel: str) -> str:
    """Move the workbook to .trash and return its trashed filename.

    Never unlinks. The .xlsx holds every field the database does, so the
    trashed file is a complete backup of a list the user may have hand-edited.
    """
    source = resolve_path(rel, must_exist=True, require_xlsx=True)
    trash = DATA_DIR.resolve() / TRASH_DIR_NAME
    trash.mkdir(parents=True, exist_ok=True)
    name = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{source.name}"
    shutil.move(str(source), str(trash / name))
    return name


def delete_folder(rel: str) -> None:
    """Delete an empty folder. Recursive deletion of lead lists is not offered."""
    target = resolve_path(rel, must_exist=True)
    if not target.is_dir():
        raise InvalidPath(f"{rel!r} is not a folder.")
    if any(target.iterdir()):
        raise InvalidPath(
            f"{rel!r} is not empty. Delete or move what is inside it first."
        )
    target.rmdir()


def _rel(path: Path) -> str:
    return path.relative_to(DATA_DIR.resolve()).as_posix()
