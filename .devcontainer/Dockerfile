FROM mcr.microsoft.com/devcontainers/base:trixie

USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
    build-essential pkg-config libgdal-dev libgeos-dev libproj-dev ca-certificates \
 && rm -rf /var/lib/apt/lists/*

USER vscode
