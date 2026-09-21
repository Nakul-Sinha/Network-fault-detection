# NetPulse Local, server SKU.
#
# This is the F17 headless mode: an agent with the localhost web UI and no
# desktop. It suits an always-on box in a small office, which is persona P2 in
# PRD 2.1, and it is also the easiest way to try the agent without installing
# anything on a laptop.
#
# Two things are worth understanding before running it.
#
# The container measures the *container's* view of the network. With the
# default bridge network the gateway is Docker's bridge, not the site router,
# so "your router" in an incident card means the bridge. Run with
# --network host to measure the host's own path, which is what an operator
# almost always wants.
#
# The API binds loopback inside the container. Publishing it with -p exposes
# it on the host, which is fine for 127.0.0.1:8787:8787 and a mistake for
# 0.0.0.0. The compose file does the former.

FROM python:3.12-slim AS build

WORKDIR /src
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY netpulse ./netpulse
RUN python -m pip install --no-cache-dir --upgrade pip build \
    && python -m build --wheel --outdir /dist


FROM python:3.12-slim

LABEL org.opencontainers.image.title="NetPulse Local" \
      org.opencontainers.image.description="Privacy-first predictive network health agent" \
      org.opencontainers.image.source="https://github.com/Nakul-Sinha/Network-fault-detection" \
      org.opencontainers.image.licenses="Apache-2.0"

# iputils-ping and traceroute are what the gateway and path collectors shell
# out to. Without them those two layers report themselves unavailable and the
# agent still runs, which is the graceful degradation PRD 5.3 asks for, but a
# server image may as well be able to measure everything.
RUN apt-get update \
    && apt-get install --no-install-recommends -y iputils-ping traceroute ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /dist/*.whl /tmp/
RUN python -m pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl

# An unprivileged user: nothing here needs root. ICMP through the ping binary
# works because the Debian package is setuid.
RUN useradd --create-home --uid 10001 netpulse
USER netpulse

ENV NETPULSE_HOME=/data \
    NETPULSE_HEADLESS=1 \
    NETPULSE_API__HOST=127.0.0.1 \
    NETPULSE_API__PORT=8787 \
    PYTHONUNBUFFERED=1

VOLUME ["/data"]
EXPOSE 8787

HEALTHCHECK --interval=60s --timeout=20s --start-period=30s --retries=3 \
    CMD netpulse status --json > /dev/null || exit 1

ENTRYPOINT ["netpulse"]
CMD ["run", "--headless"]
