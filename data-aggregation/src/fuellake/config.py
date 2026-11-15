# fuel-lake — runtime settings from environment variables (no secrets stored)
# Licensed under Apache-2.0.
"""Settings read from the environment.

Environment variables
---------------------
LAKE_ROOT                 fsspec URL of the lake (local path or ``s3://bucket/prefix``).
                          Default: ``<repo>/../fuel-lake-data``.
FUELLAKE_CONFIG_DIR       directory holding ``lake.yaml``, ``regions/``, ``aois/`` and ``datasets/`` YAML
                          (default ``<repo>/config``).
FUELLAKE_CACHE_DIR        local download cache **outside** the lake for source files much larger than the
                          monitored region (the global Globe-LFMC workbook, nationwide FIRMS archive CSVs).
                          Default ``$XDG_CACHE_HOME/fuel-lake`` or ``~/.cache/fuel-lake``. Only region-filtered
                          subsets of those files are written to ``raw/``.
FUELLAKE_S3_ENDPOINT_URL  S3 endpoint for MinIO-style roots (falls back to ``AWS_ENDPOINT_URL_S3`` /
                          ``AWS_ENDPOINT_URL``, which botocore also reads by itself).
FUELLAKE_S3_ANON          ``1`` for anonymous S3 access.
AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_REGION   read by botocore directly, never by fuel-lake.

Secrets (``EARTHDATA_TOKEN``, ``FIRMS_MAP_KEY``, ``CDSAPI_URL``, ``CDSAPI_KEY``) are never stored on the
``Settings`` object. Call :meth:`Settings.secret` at the moment you need one, and pass any text that may
contain a secret through :func:`redact` before logging or persisting it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

USER_AGENT = "fuel-lake/0.1"

# Names only. Values are read from os.environ on demand and never written anywhere.
SECRET_ENV_VARS: tuple[str, ...] = (
    "EARTHDATA_TOKEN",
    "EARTHDATA_PASSWORD",
    "FIRMS_MAP_KEY",
    "CDSAPI_URL",
    "CDSAPI_KEY",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
)


def repo_root() -> Path:
    """Directory containing ``pyproject.toml`` of this checkout (editable install), else the cwd."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file() and (parent / "src" / "fuellake").is_dir():
            return parent
    return Path.cwd()


def default_lake_root() -> str:
    return str((repo_root().parent / "fuel-lake-data").resolve())


def default_config_dir() -> Path:
    return repo_root() / "config"


def default_cache_dir() -> Path:
    xdg = os.environ.get("XDG_CACHE_HOME")
    return (Path(xdg) if xdg else Path.home() / ".cache") / "fuel-lake"


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Non-secret runtime settings. Build with :meth:`from_env`."""

    lake_root: str
    config_dir: Path
    s3_endpoint_url: str | None = None
    s3_anon: bool = False
    user_agent: str = USER_AGENT
    http_timeout: float = 60.0
    cache_dir: Path | None = None          # see FUELLAKE_CACHE_DIR; None = default_cache_dir()

    @classmethod
    def from_env(cls) -> Settings:
        endpoint = (
            os.environ.get("FUELLAKE_S3_ENDPOINT_URL")
            or os.environ.get("AWS_ENDPOINT_URL_S3")
            or os.environ.get("AWS_ENDPOINT_URL")
            or None
        )
        config_dir = os.environ.get("FUELLAKE_CONFIG_DIR")
        cache_dir = os.environ.get("FUELLAKE_CACHE_DIR")
        return cls(
            lake_root=os.environ.get("LAKE_ROOT") or default_lake_root(),
            config_dir=Path(config_dir) if config_dir else default_config_dir(),
            s3_endpoint_url=endpoint,
            s3_anon=_env_flag("FUELLAKE_S3_ANON"),
            cache_dir=Path(cache_dir).expanduser() if cache_dir else None,
        )

    def cache_path(self, *parts: str) -> Path:
        """A path in the local download cache (outside the lake); parent directories are created."""
        path = (self.cache_dir or default_cache_dir()).joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def storage_options(self, root: str | None = None) -> dict:
        """fsspec storage options for ``root`` (default: the lake root). Contains no credentials."""
        root = root or self.lake_root
        if root.startswith("s3://"):
            opts: dict = {}
            if self.s3_endpoint_url:
                opts["client_kwargs"] = {"endpoint_url": self.s3_endpoint_url}
            if self.s3_anon:
                opts["anon"] = True
            return opts
        return {}

    @staticmethod
    def secret(name: str) -> str | None:
        """Read a secret from the environment at call time. Never log or persist the value."""
        if name not in SECRET_ENV_VARS:
            raise KeyError(f"{name} is not a registered secret env var (add it to SECRET_ENV_VARS)")
        value = os.environ.get(name)
        return value or None

    @staticmethod
    def has_secret(name: str) -> bool:
        return bool(os.environ.get(name))

    def http_headers(self) -> dict[str, str]:
        return {"User-Agent": self.user_agent}


def redact(text: object) -> str:
    """Replace any secret env value found in ``text`` with ``***``."""
    out = str(text)
    for name in SECRET_ENV_VARS:
        value = os.environ.get(name)
        if value and len(value) >= 4:
            out = out.replace(value, "***")
    return out
