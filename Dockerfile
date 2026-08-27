# Single OCI image — the whole install is `docker run -v encore-data:/data -p 8321:8321 ...`
# (04-architecture.md §deployment, docs/adr/0005-sqlite-single-container.md).
FROM python:3.12-slim AS build
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

FROM python:3.12-slim
# SEC-28: take Debian's published security updates at build time.
#
# `python:3.12-slim` is rebuilt on its own cadence, so between a Debian security
# upload and the next base-image push the tag ships packages Debian has already
# fixed. Trivy runs with `--ignore-unfixed`, so exactly those packages — fix
# available, fix not in the image — are what turns the Container CVE scan red,
# on a change nobody made. Upgrading here closes that window.
#
# Deliberately not a hand-listed set of packages. The window moves: the scan
# first went red on util-linux (CVE-2026-53612/53613/53614/53615), and three
# days later the base image had picked util-linux up on its own while openssl
# CVE-2026-14456 had taken its place. A list pinned to the CVE of the week is
# stale before it merges; "whatever Debian has fixed" is not.
#
# This does mean the runtime layer is not byte-reproducible across time. That
# was already true of a floating `python:3.12-slim` tag, and the reproducibility
# the project actually pins is the Python layer (`uv.lock`, `--frozen`), which
# this does not touch.
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get clean \
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
