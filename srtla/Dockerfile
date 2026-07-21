# BELABOX srtla receiver — de-bonds multiple SRTLA connections (wifi + cellular
# from the phone) back into a single standard SRT stream pushed at MediaMTX.
# Plain C, no external deps beyond libc; builds clean on ARM64.

FROM debian:bookworm-slim AS build
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 https://github.com/BELABOX/srtla.git /src
RUN make -C /src

FROM debian:bookworm-slim
COPY --from=build /src/srtla_rec /usr/local/bin/srtla_rec
EXPOSE 5000/udp
# args: <srtla listen port> <forward srt host> <forward srt port>
CMD ["/usr/local/bin/srtla_rec", "5000", "mediamtx", "8890"]
