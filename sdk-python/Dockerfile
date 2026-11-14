# Development image for the tradesmart Python SDK: test, lint, type-check and build tooling.
# Used through compose.yaml, which mounts the repository over /repo at run time, so code
# changes need no rebuild. Rebuild (`docker compose build`) only when pyproject.toml changes.
ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPYCACHEPREFIX=/tmp/pycache \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Keep tool caches inside the container so nothing is written into the mounted repo.
    PYTEST_ADDOPTS="-p no:cacheprovider" \
    MYPY_CACHE_DIR=/tmp/mypy_cache \
    RUFF_CACHE_DIR=/tmp/ruff_cache \
    COVERAGE_FILE=/tmp/.coverage

# Tests read shared fixtures from ../specifications, so the SDK lives one level below /repo.
WORKDIR /repo/sdk-python

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install -e ".[dev]"

CMD ["pytest"]
