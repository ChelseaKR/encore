# Single OCI image — the whole install is `docker run -v encore-data:/data -p 8321:8321 ...`
# (04-architecture.md §deployment, docs/adr/0005-sqlite-single-container.md).
FROM python:3.12-slim AS build
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

FROM python:3.12-slim
# SEC-28: `python:3.12-slim`'s published layer lags Debian security fixes —
# Trivy flagged CVE-2026-53612/53613/53614/53615 (util-linux mount TOCTOU /
# SUID nosuid-noexec bypass) still present in the base image as of 2026-08-22.
# Upgrading just this source package (rather than a blanket `apt-get upgrade`)
# keeps the fix scoped and the layer small; re-run this if Trivy finds the
# next one.
RUN apt-get update \
    && apt-get install -y --no-install-recommends --only-upgrade \
        util-linux bsdutils libblkid1 libmount1 libsmartcols1 liblastlog2-2 \
    && rm -rf /var/lib/apt/lists/*
# Create /data (the documented volume mountpoint, README §install) owned by the
# app user BEFORE the VOLUME declaration: a named volume initialized from a
# mountpoint that doesn't exist in the image is created root-owned, and the
# non-root `encore` user (uid 10001) can never write to it.
RUN useradd --create-home --uid 10001 encore \
    && mkdir -p /data \
    && chown encore:encore /data
COPY --from=build /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=build /usr/local/bin/encore /usr/local/bin/encore
USER encore
# OBS-01: fixed identity for any future OTel exporter — set once, here, rather
# than re-derived per environment later.
ENV OTEL_SERVICE_NAME=encore
VOLUME ["/data"]
EXPOSE 8321
ENTRYPOINT ["encore"]
# --data-dir points at the mounted volume: the F0 storage layer keeps the SQLite
# database and its Fernet key file there (docs/adr/0005, docs/adr/0008).
CMD ["serve", "--host", "0.0.0.0", "--port", "8321", "--data-dir", "/data"]
