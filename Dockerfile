FROM --platform=$BUILDPLATFORM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS s6
ARG TARGETARCH
COPY scripts/install_s6.py /install_s6.py
RUN python /install_s6.py --arch "$TARGETARCH" --destination /s6

FROM --platform=$BUILDPLATFORM node:24-alpine@sha256:44fce696f38ba8c987c4db89954051000bbd05ffd8d8659ba15ed4a8602e937f AS web
WORKDIR /web
COPY web/package*.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
LABEL org.opencontainers.image.source="https://github.com/ChowDPa02k/aniopen-sync" \
      org.opencontainers.image.title="ANiOpen Sync" \
      org.opencontainers.image.description="ANi RSS sync with a configuration console and download tracking" \
      org.opencontainers.image.licenses="Apache-2.0"
COPY --from=s6 /s6/ /
COPY --from=ghcr.io/astral-sh/uv:0.10.7@sha256:edd1fd89f3e5b005814cc8f777610445d7b7e3ed05361f9ddfae67bebfe8456a /uv /bin/uv
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 ANIOPEN_DATA_DIR=/data ANIOPEN_WEB_DIR=/app/web/dist
ENV S6_READ_ONLY_ROOT=1 S6_KEEP_ENV=1 S6_VERBOSITY=1 S6_CMD_RECEIVE_SIGNALS=1
COPY pyproject.toml uv.lock README.md LICENSE NOTICE THIRD_PARTY_NOTICES ./
COPY aniopen_sync ./aniopen_sync
RUN uv sync --locked --no-dev --no-editable && useradd --uid 10001 --create-home aniopen && mkdir /data && chown aniopen /data /run && chmod 700 /run && ln -sfn /run /var/run
COPY --from=web /web/dist ./web/dist
USER aniopen
VOLUME /data
EXPOSE 8090
HEALTHCHECK --interval=30s --timeout=3s --start-period=30s --retries=3 CMD ["python", "-m", "aniopen_sync.healthcheck"]
ENTRYPOINT ["/init"]
CMD ["aniopen-sync", "--host", "0.0.0.0", "--port", "8090"]
