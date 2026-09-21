# MeshSettle sender service.
#
# Build context is the repository root, because the service imports `shared/`.
# Two stages: dependencies are compiled in the builder and only the resulting
# site-packages plus source are carried into the runtime image, so build tools
# never ship to production.

FROM python:3.12.9-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Copy only what is needed to resolve and install the package, so this layer
# is cached unless dependencies or module layout actually change.
COPY pyproject.toml ./
COPY shared/ ./shared/
COPY services/ ./services/

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install .


FROM python:3.12.9-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# Run as an unprivileged user. A packet-handling service has no reason to be
# root, and this limits what a compromise can reach.
RUN groupadd --system --gid 1001 meshsettle \
    && useradd --system --uid 1001 --gid meshsettle --no-create-home meshsettle

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=meshsettle:meshsettle shared/ ./shared/
COPY --chown=meshsettle:meshsettle services/ ./services/

USER meshsettle

EXPOSE 8001

# No curl in the image, so probe with the interpreter that is already here.
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8001/healthz', timeout=2).status == 200 else 1)"]

CMD ["uvicorn", "services.sender.app:app", "--host", "0.0.0.0", "--port", "8001"]
