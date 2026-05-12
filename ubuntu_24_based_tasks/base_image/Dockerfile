# SWE-bench Pro Python tasks — Ubuntu 24.04 base image with all system deps.
#
# This image is built ONCE and reused by the task runner for every Python instance.
# It contains:
#   - The union of apt packages needed across qutebrowser + ansible + openlibrary
#     (the three Python repos in the dataset).
#   - `uv` on /usr/local/bin so it's available to any user.
#   - A non-root `runner` user (the task runner can override).
#
# At task time, the runner clones the repo into the CWD, runs install.sh
# (which creates .venv/), applies the patch, then runs eval.sh.

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends \
    # Core dev/runtime tooling
    bash \
    git \
    curl \
    ca-certificates \
    build-essential \
    pkg-config \
    python3 \
    python3-dev \
    python3-venv \
    python-is-python3 \
    # qutebrowser: Qt/X11 headless test stack
    xvfb \
    dbus-x11 \
    x11-utils \
    xauth \
    libglib2.0-0 \
    libgl1 \
    libegl1 \
    libxkbcommon0 \
    libxkbcommon-x11-0 \
    libdbus-1-3 \
    libnss3 \
    libnspr4 \
    libxss1 \
    libxtst6 \
    libxcomposite1 \
    libxcursor1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxi6 \
    libxrandr2 \
    libxrender1 \
    libxcb1 \
    libxcb-cursor0 \
    libxcb-icccm4 \
    libxcb-image0 \
    libxcb-keysyms1 \
    libxcb-randr0 \
    libxcb-render0 \
    libxcb-render-util0 \
    libxcb-shape0 \
    libxcb-sync1 \
    libxcb-util1 \
    libxcb-xfixes0 \
    libxcb-xinerama0 \
    libxcb-xkb1 \
    libatk-bridge2.0-0 \
    libdrm2 \
    libgtk-3-0 \
    libfontconfig1 \
    libfreetype6 \
    fonts-liberation2 \
    fonts-dejavu-core \
    libasound2t64 \
    # Common Python-build deps used by ansible + openlibrary
    libxml2-dev \
    libxslt1-dev \
    libffi-dev \
    libssl-dev \
    libyaml-dev \
    zlib1g-dev \
    # ansible: integration tests need ssh tooling
    openssh-client \
    sshpass \
    rsync \
    make \
    # openlibrary extras (postgres client, parallel, archive tools, node)
    postgresql-client \
    parallel \
    zip \
    unzip \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

# Install uv via the upstream container image — mirrors the itf-demo production
# runner Dockerfile so behavior matches in production.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# Run as the default `ubuntu` user (uid 1000) that ships with ubuntu:24.04, to
# match the production runner. The task runner mounts /tmp/jobs/<uuid>/workspace.
USER ubuntu
WORKDIR /home/ubuntu
