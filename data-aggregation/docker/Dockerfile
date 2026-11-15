# fuel-lake — container image for the read-only API, the ETL and processing microservices
# Licensed under Apache-2.0.
#
# Build from the repository root:   docker build -f docker/Dockerfile -t fuel-lake:local .
# UNRUN: Docker is not available on the development machine; this file has only been checked statically.
# The rasterio / h5py / netCDF4 wheels bundle GDAL, HDF5 and netCDF, so no apt packages are needed.

FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    FUELLAKE_CONFIG_DIR=/app/config

WORKDIR /app

# Dependencies first (cached layer), then the package. Copy named paths only, as a second line of defence
# behind .dockerignore (which keeps .env, .venv/, tests/ and data files out of the build context).
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY config ./config
RUN uv sync --frozen --no-dev --no-editable

# Unprivileged user; /var/lib/fuellake is a writable home for service outputs (mount a volume there).
RUN useradd --create-home --uid 10001 fuellake \
    && mkdir -p /var/lib/fuellake \
    && chown fuellake:fuellake /var/lib/fuellake
USER fuellake

EXPOSE 8000
CMD ["fuellake", "serve", "--host", "0.0.0.0", "--port", "8000"]
