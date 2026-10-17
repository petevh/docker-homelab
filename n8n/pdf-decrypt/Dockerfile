FROM python:3.13-alpine
RUN apk add --no-cache qpdf
# --chmod: the NAS share's 770 ACLs would otherwise be baked into the image
COPY --chmod=0555 server.py /app/server.py
EXPOSE 8080
CMD ["python", "-u", "/app/server.py"]
