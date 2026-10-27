FROM python:3.12-slim-trixie AS dependencies
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
COPY requirements.txt /tmp/requirements.txt
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --only-binary=:all: -r /tmp/requirements.txt

FROM python:3.12-slim-trixie AS runtime
ARG RDS_CA_BUNDLE_SHA256
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp \
    LANGSMITH_TRACING=false \
    LANGCHAIN_TRACING_V2=false
WORKDIR /srv
COPY --from=dependencies /opt/venv /opt/venv
COPY app/ backend/app/
COPY knowledge/ backend/knowledge/
COPY container/ backend/container/
# Fetch only the official RDS trust-store object and require a reviewed digest.
# The build fails closed when the digest is absent or the bytes change.
RUN test -n "$RDS_CA_BUNDLE_SHA256" \
    && install -d -o root -g root -m 0755 /etc/novamind \
    && RDS_CA_BUNDLE_SHA256="$RDS_CA_BUNDLE_SHA256" \
       python -m backend.container.install_rds_ca_bundle \
       --output /etc/novamind/rds-ca-bundle.pem \
    && chown root:root /etc/novamind/rds-ca-bundle.pem \
    && chmod 0444 /etc/novamind/rds-ca-bundle.pem
# The volume mounts at /srv/state. Docker populates its empty volume with this
# UID/GID-owned child directory, which is the only durable write location.
RUN install -d -o 10001 -g 10001 -m 0750 /srv/state/novamind
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=15s --retries=3 \
    CMD ["python", "-m", "backend.container.healthcheck"]
CMD ["python", "-m", "uvicorn", "backend.container.bootstrap:create_container_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-proxy-headers", "--no-access-log"]
