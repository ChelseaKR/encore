# Single OCI image — the whole install is `docker run -v encore-data:/data -p 8321:8321 ...`
# (04-architecture.md §deployment, docs/adr/0005-sqlite-single-container.md).
FROM python:3.12-slim AS build
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

FROM python:3.12-slim
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
# No --data-dir: the CLI doesn't parse it yet (src/encore/cli.py) — there is no
# storage layer to point at until M1. Re-add it here the same PR that wires it.
CMD ["serve", "--host", "0.0.0.0", "--port", "8321"]
