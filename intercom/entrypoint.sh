#!/bin/sh
# Start mediamtx (RTSP server) in the background, wait for it to listen, then the app.
set -e

/usr/local/bin/mediamtx /app/mediamtx.yml &
MTX_PID=$!

# If mediamtx dies, take the container down so it restarts.
trap 'kill $MTX_PID 2>/dev/null' TERM INT

# Wait for the RTSP port to be listening before starting the app (which publishes to it).
for i in $(seq 1 30); do
    if python -c "import socket,sys; s=socket.socket(); s.settimeout(0.5); sys.exit(s.connect_ex(('127.0.0.1',8554)))" 2>/dev/null; then
        break
    fi
    sleep 0.5
done

exec uvicorn main:app --host 0.0.0.0 --port 8000
