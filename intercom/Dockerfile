FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates espeak-ng \
    && rm -rf /var/lib/apt/lists/*

# mediamtx — lightweight RTSP server for the H264 passthrough feed
ARG MEDIAMTX_VERSION=1.9.3
RUN ARCH="$(dpkg --print-architecture)" \
    && case "$ARCH" in \
         amd64) MTX_ARCH=linux_amd64 ;; \
         arm64) MTX_ARCH=linux_arm64v8 ;; \
         *) echo "unsupported arch $ARCH" && exit 1 ;; \
       esac \
    && curl -fsSL "https://github.com/bluenviron/mediamtx/releases/download/v${MEDIAMTX_VERSION}/mediamtx_v${MEDIAMTX_VERSION}_${MTX_ARCH}.tar.gz" \
       | tar -xz -C /usr/local/bin mediamtx \
    && chmod +x /usr/local/bin/mediamtx

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Piper TTS voice model for the /say endpoint (en_US, natural neural voice).
ARG PIPER_VOICE=en_US-amy-medium
RUN mkdir -p /app/piper \
    && curl -fsSL "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/medium/${PIPER_VOICE}.onnx" \
       -o "/app/piper/${PIPER_VOICE}.onnx" \
    && curl -fsSL "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/medium/${PIPER_VOICE}.onnx.json" \
       -o "/app/piper/${PIPER_VOICE}.onnx.json"

COPY mediamtx.yml /app/mediamtx.yml
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh
COPY main.py .
COPY dahua_client.py .
COPY talk_ui.html .
COPY hls.min.js .

EXPOSE 8000 8554

CMD ["/app/entrypoint.sh"]
