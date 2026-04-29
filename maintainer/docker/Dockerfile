# owner: project maintainer
# date: 2026-03-23
# service: Curatarr application container
# purpose: application image for the Curatarr runtime foundation
# docs: maintainer/README.md, maintainer/development/README.md
# deployment notes:
#   - This image currently supports CLI bootstrap and configuration validation.
# history:
#   - 2026-03-10: Initialized generic template image.
#   - 2026-03-23: Adapted the image for the former Searcharr-nxg Python service runtime.
#   - 2026-03-25: Rebased on 11notes/python for the public release path.
#   - 2026-04-29: Reset to Curatarr scaffold baseline.

FROM 11notes/python:3.12

ARG BUILD_NUMBER=local
ARG VERSION=dev-local

LABEL org.opencontainers.image.title="Curatarr"
LABEL org.opencontainers.image.description="Telegram-first media request and library management assistant"
LABEL org.opencontainers.image.version="${VERSION}"
LABEL org.opencontainers.image.source="https://github.com/BrainPonders/curatarr"

ENV APP_HOME=/app
ENV PROJECT_BUILD_NUMBER="${BUILD_NUMBER}"
ENV PROJECT_VERSION="${VERSION}"
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

USER root

WORKDIR /app

COPY README.md ./README.md
COPY pyproject.toml ./pyproject.toml
COPY src ./src
COPY documentation ./documentation

RUN set -eux; \
    python -m pip install --no-cache-dir .

RUN mkdir -p /app/data && chown -R 1000:1000 /app
COPY maintainer/docker/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

USER 1000:1000

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
