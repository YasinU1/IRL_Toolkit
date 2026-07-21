# OBS Studio built from source for ARM64 (Oracle Ampere A1 has no official
# OBS Linux ARM64 package). Headless: Xvfb virtual display + x11vnc + noVNC
# for browser access, obs-websocket (in-tree since OBS 28) for control.
#
# Browser sources (CEF) are disabled — CEF is the one dependency that is
# genuinely painful on Linux ARM64. Everything else is stock.

FROM ubuntu:24.04 AS build
ARG OBS_VERSION=31.0.3
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake ninja-build pkg-config git ca-certificates curl \
        # FFmpeg (Ubuntu 24.04 builds ship libsrt support -> Media Source can
        # open srt:// URLs directly)
        libavcodec-dev libavdevice-dev libavfilter-dev libavformat-dev \
        libavutil-dev libswresample-dev libswscale-dev \
        # video/encode
        libx264-dev libv4l-dev libdrm-dev libva-dev \
        # core libs
        libcurl4-openssl-dev libmbedtls-dev libjansson-dev libpci-dev \
        libspeexdsp-dev libudev-dev uthash-dev libglib2.0-dev \
        # audio
        libpulse-dev libasound2-dev \
        # display / GL
        libgl1-mesa-dev libgles2-mesa-dev libglvnd-dev libegl1-mesa-dev \
        libx11-dev libx11-xcb-dev libxcb1-dev libxcb-randr0-dev \
        libxcb-shm0-dev libxcb-xinerama0-dev libxcb-composite0-dev \
        libxcb-xfixes0-dev libxcb-xinput-dev libxcomposite-dev libxinerama-dev libxss-dev \
        # Qt6 UI
        qt6-base-dev qt6-base-private-dev qt6-svg-dev \
        # obs-websocket deps
        libasio-dev libwebsocketpp-dev nlohmann-json3-dev libqrcodegencpp-dev \
        # fonts/text
        libfontconfig-dev libfreetype6-dev \
        # rist/srt output protocols
        librist-dev libsrt-openssl-dev \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --branch ${OBS_VERSION} --depth 1 --recurse-submodules \
        https://github.com/obsproject/obs-studio.git /src

RUN cmake -S /src -B /build -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX=/usr/local \
        -DCMAKE_COMPILE_WARNING_AS_ERROR=OFF \
        -DENABLE_BROWSER=OFF \
        -DENABLE_WEBSOCKET=ON \
        -DENABLE_SCRIPTING=OFF \
        -DENABLE_PIPEWIRE=OFF \
        -DENABLE_WAYLAND=OFF \
        -DENABLE_AJA=OFF \
        -DENABLE_DECKLINK=OFF \
        -DENABLE_VLC=OFF \
        -DENABLE_VST=OFF \
        -DENABLE_JACK=OFF \
        -DENABLE_LIBFDK=OFF \
        -DENABLE_WEBRTC=OFF \
        -DENABLE_NVENC=OFF \
    && cmake --build /build \
    && cmake --install /build

FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        # runtime libs (qt6-base-dev pulls the full Qt runtime incl. the xcb
        # platform plugin -- heavier than strictly needed, but reliable)
        ffmpeg qt6-base-dev qt6-svg-dev libqrcodegencpp1 \
        libx264-164 libmbedtls14t64 libjansson4 libcurl4 libpci3 \
        libspeexdsp1 libudev1 libglib2.0-0 libpulse0 libasound2t64 \
        libgl1 libegl1 libgl1-mesa-dri libglx-mesa0 \
        librist4 libsrt1.5-openssl libv4l-0 \
        libva2 libva-drm2 libva-x11-2 libxcb-xinput0 \
        # audio + misc
        pulseaudio fonts-dejavu-core ca-certificates gettext-base curl \
    && rm -rf /var/lib/apt/lists/*

# KasmVNC: replaces Xvfb+x11vnc+noVNC — it IS the X server, with server-side
# WebP/JPEG encoding and a modern web client. Far better browser quality.
ARG KASMVNC_VERSION=1.4.0
RUN curl -fsSL -o /tmp/kasmvnc.deb \
        https://github.com/kasmtech/KasmVNC/releases/download/v${KASMVNC_VERSION}/kasmvncserver_noble_${KASMVNC_VERSION}_arm64.deb \
    && apt-get update && apt-get install -y --no-install-recommends /tmp/kasmvnc.deb \
    && rm /tmp/kasmvnc.deb && rm -rf /var/lib/apt/lists/*

# Sunshine: Moonlight-protocol desktop streaming (high-quality remote OBS;
# noVNC stays as the zero-dependency fallback).
ARG SUNSHINE_VERSION=2026.516.143833
RUN curl -fsSL -o /tmp/sunshine.deb \
        https://github.com/LizardByte/Sunshine/releases/download/v${SUNSHINE_VERSION}/sunshine-ubuntu-24.04-arm64.deb \
    && apt-get update && apt-get install -y --no-install-recommends /tmp/sunshine.deb \
    && rm /tmp/sunshine.deb && rm -rf /var/lib/apt/lists/*

COPY --from=build /usr/local /usr/local
RUN ldconfig

COPY config/ /opt/obs-config-seed/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV HOME=/config \
    DISPLAY=:99 \
    QT_QPA_PLATFORM=xcb \
    LIBGL_ALWAYS_SOFTWARE=1

# 4455 obs-websocket (internal), 6080 noVNC (proxied by caddy),
# 47984-47990/48010 tcp + 47998-48000 udp Sunshine/Moonlight
EXPOSE 4455 6080 47989 47990

ENTRYPOINT ["/entrypoint.sh"]
