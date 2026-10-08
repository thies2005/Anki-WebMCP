# Anki Worker - Browser-accessible Anki Desktop with KasmVNC and AnkiConnect
FROM ghcr.io/linuxserver/baseimage-kasmvnc:debianbookworm

ARG ANKI_VERSION=24.11

LABEL maintainer="Antigravity"
LABEL org.opencontainers.image.title="Anki Desktop Headless Worker"
LABEL org.opencontainers.image.description="Anki Desktop with KasmVNC web access and pre-configured AnkiConnect"
LABEL org.opencontainers.image.version="${ANKI_VERSION}"

# Environment defaults
ENV TITLE="Anki Desktop" \
    CUSTOM_PORT=3000 \
    CUSTOM_HTTPS_PORT=3001 \
    START_DOCKER=false \
    PUID=1000 \
    PGID=1000 \
    ANKI_VERSION=${ANKI_VERSION}

# Install system dependencies and pinned Anki Desktop release
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        zstd \
        curl \
        jq \
        xdg-utils \
        ca-certificates \
        # Qt6 / X11 / Chromium runtime dependencies
        libxcb-cursor0 \
        libxcb-xinerama0 \
        libxcb-icccm4 \
        libxcb-image0 \
        libxcb-keysyms1 \
        libxcb-render-util0 \
        libxcb-shape0 \
        libnss3 \
        libxcomposite1 \
        libxdamage1 \
        libxrandr2 \
        libgbm1 \
        libasound2 \
        libpango-1.0-0 \
        libpangocairo-1.0-0 \
        libatk1.0-0 \
        libatk-bridge2.0-0 \
        libcups2 \
        libdrm2 \
        libgtk-3-0 \
        libegl1 \
        libgl1 \
        libxkbcommon0 \
        libxkbcommon-x11-0 \
        libatomic1 \
        # Fonts for reliable rendering of flashcards and UI
        fonts-liberation \
        fonts-noto \
        fonts-noto-color-emoji \
    && \
    echo "**** download and install Anki ${ANKI_VERSION} ****" && \
    mkdir -p /tmp/anki-install && \
    curl -fLo /tmp/anki-install/anki.tar.zst \
        "https://github.com/ankitects/anki/releases/download/${ANKI_VERSION}/anki-${ANKI_VERSION}-linux-qt6.tar.zst" && \
    cd /tmp/anki-install && \
    tar --use-compress-program=unzstd -xf anki.tar.zst && \
    cd anki-${ANKI_VERSION}-linux-qt6 && \
    ./install.sh && \
    echo "**** clean up build cache ****" && \
    rm -rf /tmp/anki-install && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* /var/tmp/* /tmp/*

# Pre-stage AnkiConnect add-on (ID 2055492159)
COPY addons/2055492159 /opt/anki-addons/2055492159

# Copy desktop and autostart configurations
COPY root/ /

# Ports: KasmVNC Web UI (3000 HTTP, 3001 HTTPS), AnkiConnect (8765)
EXPOSE 3000 3001 8765

# Healthcheck: KasmVNC web UI is responsive
# KasmVNC enforces HTTP basic auth: the healthcheck must present credentials
# (CUSTOM_USER/PASSWORD env are provided by the baseimage) or it would 401.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -sf -u "${CUSTOM_USER}:${PASSWORD}" http://localhost:3000/ || exit 1

# Persistent user configuration volume
VOLUME /config
