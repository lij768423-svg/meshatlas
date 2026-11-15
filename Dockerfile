# MeshAtlas in a container. It talks to the host's tailscaled through its socket (mounted read-only below),
# so it needs the tailscale CLI but not its own Tailscale login.
# The tailscale CLI comes from the official image (a static Go binary), so the build only needs a container registry,
# not pkgs.tailscale.com, and picks the right architecture by itself.
ARG TAILSCALE_VERSION=1.104.7
FROM tailscale/tailscale:v${TAILSCALE_VERSION} AS tailscale

FROM python:3.12-slim
COPY --from=tailscale /usr/local/bin/tailscale /usr/local/bin/tailscale
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates openssh-client iputils-ping \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE.md ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --create-home --uid 10001 meshatlas \
 && mkdir -p /data && chown meshatlas /data

USER meshatlas
ENV MESHATLAS_DATA=/data \
    PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8765
# `meshatlas run` writes the address it really listens on (Tailscale IP with --host auto) to <data dir>/healthz-url.
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s CMD python3 -c "import os, urllib.request as u; \
u.urlopen(open(os.path.join(os.environ.get('MESHATLAS_DATA', '/data'), 'healthz-url')).read().strip(), timeout=4)" || exit 1
ENTRYPOINT ["meshatlas"]
CMD ["run", "--host", "0.0.0.0"]
