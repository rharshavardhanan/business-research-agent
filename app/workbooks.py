"""Workbook and folder management, stored as rows rather than files.

There is no filesystem on serverless hosting, so a workbook is a row in the
`workbooks` table whose `path` may contain slashes. Folders are path prefixes,
not directories: `dental/chennai.xlsx` is one row, and the tree is built by
splitting on `/`.

An empty folder is a row whose path has no `.xlsx` suffix — that is what lets a
folder exist before anything is put in it.
"""

import re

DEFAULT_WORKBOOK = "businesses.xlsx"

# A path is still a key, so it still has to be sane. Without a directory to
# escape this is no longer a filesystem trust boundary, but a path with `..`
# or empty segments produces keys nobody can address.
_SEGMENT = re.compile(r"^[A-Za-z0-9 ._&()\-]+$")

MAX_PATH_LENGTH = 250


class InvalidPath(ValueError):
    """A client-supplied path is not a usable workbook key."""


def validate(path: str, *, require_xlsx: bool = False) -> str:
    """Return the normalised path, or raise InvalidPath."""
    if not path or not path.strip():
        raise InvalidPath("A path is required.")
    candidate = path.strip().replace("\\", "/")

    if candidate.startswith("/"):
        raise InvalidPath("Absolute paths are not allowed.")
    if len(candidate) > MAX_PATH_LENGTH:
        raise InvalidPath(f"Path is too long (limit {MAX_PATH_LENGTH} characters).")

    segments = candidate.split("/")
    if any(s.strip() == "" for s in segments):
        raise InvalidPath("Path has an empty segment.")
    if any(s.strip() in (".", "..") for s in segments):
        raise InvalidPath("Path traversal is not allowed.")
    for segment in segments:
        if not _SEGMENT.match(segment.strip()):
            raise InvalidPath(f"{segment!r} contains characters that are not allowed.")

    normalised = "/".join(s.strip() for s in segments)
    if require_xlsx and not normalised.endswith(".xlsx"):
        raise InvalidPath("A workbook must end in .xlsx")
    return normalised


def is_workbook(path: str) -> bool:
    return path.endswith(".xlsx")


def list_paths(store) -> list[str]:
    with store._conn() as conn:
        rows = conn.execute(
            "SELECT path FROM workbooks WHERE deleted_at IS NULL ORDER BY path"
        ).fetchall()
    return [r["path"] for r in rows]


def tree(store) -> dict:
    """Nest the flat path list into the shape the sidebar renders."""
    root: dict = {"name": "", "path": "", "type": "folder", "children": []}

    for path in list_paths(store):
        segments = path.split("/")
        node = root
        for depth, segment in enumerate(segments):
            partial = "/".join(segments[: depth + 1])
            last = depth == len(segments) - 1

            if last and is_workbook(path):
                node["children"].append(
                    {"name": segment, "path": partial, "type": "workbook"}
                )
                break

            existing = next(
                (c for c in node["children"]
                 if c["type"] == "folder" and c["name"] == segment),
                None,
            )
            if existing is None:
                existing = {"name": segment, "path": partial,
                            "type": "folder", "children": []}
                node["children"].append(existing)
            node = existing

    _sort(root)
    return root


def _sort(node: dict) -> None:
    node["children"].sort(key=lambda c: (c["type"] != "folder", c["name"].lower()))
    for child in node["children"]:
        if child["type"] == "folder":
            _sort(child)


def create_workbook(store, path: str) -> str:
    target = validate(path, require_xlsx=True)
    if target in list_paths(store):
        raise InvalidPath(f"{target!r} already exists.")
    store.ensure_workbook(target)
    return target


def create_folder(store, path: str) -> str:
    target = validate(path)
    if is_workbook(target):
        raise InvalidPath("A folder name must not end in .xlsx")
    if target in list_paths(store):
        raise InvalidPath(f"{target!r} already exists.")
    store.ensure_workbook(target)
    return target


def rename_or_move(store, src: str, dst: str) -> str:
    source = validate(src)
    target = validate(dst, require_xlsx=is_workbook(source))
    existing = list_paths(store)
    if source not in existing:
        raise InvalidPath(f"{source!r} does not exist.")
    if target in existing:
        raise InvalidPath(f"{target!r} already exists.")

    with store._conn() as conn:
        # ON UPDATE CASCADE carries the businesses across, so no second
        # statement is needed to keep the rows with their workbook.
        conn.execute("UPDATE workbooks SET path = %s WHERE path = %s", (target, source))
        if not is_workbook(source):
            # Renaming a folder moves everything filed beneath it.
            conn.execute(
                "UPDATE workbooks SET path = %s || substring(path from %s) "
                "WHERE path LIKE %s",
                (target, len(source) + 1, f"{source}/%"),
            )
        conn.commit()
    return target


def delete_workbook(store, path: str) -> int:
    """Soft-delete a workbook and its rows. Returns how many rows were hidden."""
    target = validate(path, require_xlsx=True)
    if target not in list_paths(store):
        raise InvalidPath(f"{target!r} does not exist.")
    removed = store.delete_rows(target)
    with store._conn() as conn:
        conn.execute(
            "UPDATE workbooks SET deleted_at = now() WHERE path = %s", (target,)
        )
        conn.commit()
    return removed


def delete_folder(store, path: str) -> None:
    """Delete an empty folder. Recursive deletion of lead lists is not offered."""
    target = validate(path)
    paths = list_paths(store)
    if target not in paths:
        raise InvalidPath(f"{target!r} does not exist.")
    if is_workbook(target):
        raise InvalidPath(f"{target!r} is a workbook, not a folder.")
    if any(p.startswith(f"{target}/") for p in paths):
        raise InvalidPath(
            f"{target!r} is not empty. Delete or move what is inside it first."
        )
    with store._conn() as conn:
        conn.execute(
            "UPDATE workbooks SET deleted_at = now() WHERE path = %s", (target,)
        )
        conn.commit()
