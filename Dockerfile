# syntax=docker/dockerfile:1

# --- Stage 1: build a wheel ---------------------------------------------------
FROM python:3.12-slim AS builder
WORKDIR /build

RUN pip install --no-cache-dir hatchling

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .

# --- Stage 2: runtime ---------------------------------------------------------
FROM python:3.12-slim AS runtime
WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Installing the wheel pulls the runtime dependencies and nothing else: pytest,
# mypy, ruff and testcontainers never enter this image.
COPY --from=builder /wheels /wheels
COPY pyproject.toml ./
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

# A batch job has no reason to run as root.
RUN useradd --create-home --uid 10001 etl
USER etl

# There is no server here, so no port and no healthcheck. The container runs to
# completion and its exit code is the result: 0 if at least one source loaded.
ENTRYPOINT ["labour-etl"]
CMD ["run"]
