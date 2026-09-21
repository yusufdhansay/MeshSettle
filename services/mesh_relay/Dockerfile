# MeshSettle mesh relay service.
#
# Build context is the repository root, because the service imports `shared/`.
# The relay is a dumb carrier: it holds no private decryption key and no
# database credentials, only the sender's public key for verification.

FROM python:3.12.9-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

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

RUN groupadd --system --gid 1001 meshsettle \
    && useradd --system --uid 1001 --gid meshsettle --no-create-home meshsettle

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=meshsettle:meshsettle shared/ ./shared/
COPY --chown=meshsettle:meshsettle services/ ./services/

USER meshsettle

EXPOSE 8002

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8002/healthz', timeout=2).status == 200 else 1)"]

CMD ["uvicorn", "services.mesh_relay.app:app", "--host", "0.0.0.0", "--port", "8002"]
