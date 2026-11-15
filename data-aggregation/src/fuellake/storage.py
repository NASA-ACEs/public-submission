# fuel-lake — lake storage on any fsspec root (local directory or s3://)
# Licensed under Apache-2.0.
"""``Lake``: all reads and writes under ``LAKE_ROOT`` go through this class.

Paths passed to every method are **lake-relative POSIX paths** (``"catalog/catalog.json"``).
Methods that list (``ls``, ``glob``, ``find``) also return lake-relative paths, never bare names and
never absolute URLs.

For libraries that need a real file (GDAL, netCDF, h5py, zarr writers) use :meth:`Lake.local_path`:
on a local root it yields the real path; on a remote root it downloads (``mode="r"``) or uploads on a
clean exit (``mode="w"``). Directories (e.g. ``data.zarr``) work in both modes.

One writer at a time per lake root. Concurrent writers are not supported and not locked against.
"""

from __future__ import annotations

import contextlib
import json
import os
import posixpath
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import fsspec

from fuellake.config import Settings

LAKE_DIRS = ("raw", "staged", "staged/tables", "features", "catalog", "events", "runs", "inbox/biomass")


def _clean_rel(rel: str) -> str:
    rel = (rel or "").replace("\\", "/").strip("/")
    if not rel:
        return ""
    norm = posixpath.normpath(rel)
    if norm == ".":
        return ""
    if norm.startswith("../") or norm == ".." or norm.startswith("/"):
        raise ValueError(f"path escapes the lake root: {rel!r}")
    return norm


class Lake:
    """A data lake rooted at an fsspec URL (local path or ``s3://bucket/prefix``)."""

    def __init__(self, root: str | os.PathLike, storage_options: dict | None = None):
        root = str(root)
        if root.startswith("file://"):
            root = root[len("file://") :]
        self.is_local = "://" not in root
        if self.is_local:
            root = str(Path(root).expanduser().resolve())
            self.fs = fsspec.filesystem("file", auto_mkdir=True)
            self.storage_options: dict = {}
        else:
            protocol = root.split("://", 1)[0]
            self.storage_options = dict(storage_options or {})
            self.fs = fsspec.filesystem(protocol, **self.storage_options)
        self.root: str = root.rstrip("/")
        self._root_stripped = self.fs._strip_protocol(self.root).rstrip("/")

    # ------------------------------------------------------------------ construction
    @classmethod
    def from_env(cls) -> Lake:
        """Lake at ``LAKE_ROOT`` (see :mod:`fuellake.config`). Creates nothing."""
        settings = Settings.from_env()
        return cls(settings.lake_root, storage_options=settings.storage_options())

    def __repr__(self) -> str:
        return f"Lake({self.root!r})"

    # ------------------------------------------------------------------ paths
    def url(self, rel: str) -> str:
        """Absolute fsspec URL (or absolute local path) of a lake-relative path."""
        rel = _clean_rel(rel)
        return f"{self.root}/{rel}" if rel else self.root

    def rel(self, url_or_path: str) -> str:
        """Inverse of :meth:`url`: lake-relative path of an absolute URL/path inside the lake."""
        stripped = self.fs._strip_protocol(str(url_or_path)).rstrip("/")
        if stripped == self._root_stripped:
            return ""
        prefix = self._root_stripped + "/"
        if not stripped.startswith(prefix):
            raise ValueError(f"{url_or_path!r} is not inside lake root {self.root!r}")
        return stripped[len(prefix) :]

    # ------------------------------------------------------------------ basic IO
    def exists(self, rel: str) -> bool:
        return self.fs.exists(self.url(rel))

    def isdir(self, rel: str) -> bool:
        return self.fs.isdir(self.url(rel))

    def read_bytes(self, rel: str) -> bytes:
        return self.fs.cat_file(self.url(rel))

    def write_bytes(self, rel: str, data: bytes) -> None:
        self.fs.pipe_file(self.url(rel), data)

    def read_text(self, rel: str, encoding: str = "utf-8") -> str:
        return self.read_bytes(rel).decode(encoding)

    def write_text(self, rel: str, text: str, encoding: str = "utf-8") -> None:
        self.write_bytes(rel, text.encode(encoding))

    def append_text(self, rel: str, text: str) -> None:
        """Append text. Native append locally; read-modify-write on object stores (single writer)."""
        if self.is_local:
            with self.fs.open(self.url(rel), "a", encoding="utf-8") as f:
                f.write(text)
            return
        old = self.read_text(rel) if self.exists(rel) else ""
        self.write_text(rel, old + text)

    def read_json(self, rel: str) -> Any:
        return json.loads(self.read_bytes(rel))

    def write_json(self, rel: str, obj: Any, indent: int | None = 2) -> None:
        """Write JSON. Refuses NaN/Infinity (``allow_nan=False``); encode nodata as ``"nan"``."""
        text = json.dumps(obj, indent=indent, allow_nan=False, ensure_ascii=False, default=_json_default)
        self.write_text(rel, text + "\n")

    def open(self, rel: str, mode: str = "rb"):
        """fsspec file object (use as a context manager)."""
        return self.fs.open(self.url(rel), mode)

    def rm(self, rel: str, recursive: bool = False) -> None:
        if self.exists(rel):
            self.fs.rm(self.url(rel), recursive=recursive)

    def makedirs(self, rel: str) -> None:
        """Create a directory (local roots only; object stores have no directories)."""
        if self.is_local:
            os.makedirs(self.url(rel), exist_ok=True)

    # ------------------------------------------------------------------ listing
    def ls(self, rel: str) -> list[str]:
        """Direct children of ``rel`` as sorted lake-relative paths. ``[]`` if it does not exist."""
        url = self.url(rel)
        self.fs.invalidate_cache(url)
        if not self.fs.exists(url) or not self.fs.isdir(url):
            return []
        out = []
        for p in self.fs.ls(url, detail=False):
            r = self.rel(p)
            if r and r != _clean_rel(rel):
                out.append(r)
        return sorted(set(out))

    def glob(self, pattern: str) -> list[str]:
        """Sorted lake-relative paths matching a glob pattern such as ``staged/tables/x/*/part-*.parquet``."""
        self.fs.invalidate_cache()
        return sorted(self.rel(p) for p in self.fs.glob(self.url(pattern)))

    def find(self, rel: str) -> list[str]:
        """All files below ``rel`` (recursive) as sorted lake-relative paths."""
        url = self.url(rel)
        self.fs.invalidate_cache(url)
        if not self.fs.exists(url):
            return []
        return sorted(self.rel(p) for p in self.fs.find(url))

    # ------------------------------------------------------------------ local file bridge
    @contextlib.contextmanager
    def local_path(self, rel: str, mode: str = "r") -> Iterator[str]:
        """Context manager yielding a real local filesystem path for ``rel``.

        ``mode="r"``: the path holds the file (or directory) contents.
        ``mode="w"``: write a file or a directory at the yielded path. Local roots write in place (parent
        directories are created). Remote roots write to a temporary location and upload **only if the
        block exits without an exception**; an existing remote directory is replaced.
        """
        if mode not in ("r", "w"):
            raise ValueError("mode must be 'r' or 'w'")
        rel = _clean_rel(rel)
        if self.is_local:
            path = self.url(rel)
            if mode == "w":
                os.makedirs(os.path.dirname(path), exist_ok=True)
            elif not os.path.exists(path):
                raise FileNotFoundError(path)
            yield path
            return

        tmpdir = tempfile.mkdtemp(prefix="fuellake-")
        local = os.path.join(tmpdir, posixpath.basename(rel) or "data")
        try:
            if mode == "r":
                url = self.url(rel)
                if self.fs.isdir(url):
                    for file_rel in self.find(rel):
                        sub = file_rel[len(rel) + 1 :]
                        dst = os.path.join(local, *sub.split("/"))
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        self.fs.get_file(self.url(file_rel), dst)
                else:
                    self.fs.get_file(url, local)
                yield local
            else:
                yield local
                self._upload(local, rel)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def _upload(self, local: str, rel: str) -> None:
        if os.path.isdir(local):
            if self.exists(rel):
                self.fs.rm(self.url(rel), recursive=True)
            for dirpath, _dirs, files in os.walk(local):
                for name in files:
                    src = os.path.join(dirpath, name)
                    sub = os.path.relpath(src, local).replace(os.sep, "/")
                    self.fs.put_file(src, self.url(f"{rel}/{sub}"))
        elif os.path.exists(local):
            self.fs.put_file(local, self.url(rel))
        else:
            raise FileNotFoundError(f"nothing was written to the local path for {rel!r}")
        self.fs.invalidate_cache()

    # ------------------------------------------------------------------ setup
    def init(self) -> list[str]:
        """Create the standard lake directories (local) — returns the list of lake dirs."""
        for d in LAKE_DIRS:
            self.makedirs(d)
        return list(LAKE_DIRS)


def _json_default(obj: Any) -> Any:
    import datetime as _dt

    import numpy as np

    if isinstance(obj, _dt.datetime):
        return obj.isoformat().replace("+00:00", "Z")
    if isinstance(obj, _dt.date):
        return obj.isoformat()
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, (set, tuple)):
        return list(obj)
    if isinstance(obj, Path):
        return str(obj)
    raise TypeError(f"not JSON serialisable: {type(obj).__name__}")
