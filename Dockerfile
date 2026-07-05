# Single OCI image — the whole install is `docker run -v encore-data:/data -p 8321:8321 ...`
# (04-architecture.md §deployment, docs/adr/0005-sqlite-single-container.md).
FROM python:3.12-slim AS build
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

FROM python:3.12-slim
RUN useradd --create-home --uid 10001 encore
COPY --from=build /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=build /usr/local/bin/encore /usr/local/bin/encore
USER encore
VOLUME ["/data"]
EXPOSE 8321
ENTRYPOINT ["encore"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8321", "--data-dir", "/data"]
