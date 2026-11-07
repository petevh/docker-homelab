FROM python:3.13-alpine
RUN apk add --no-cache qpdf && pip install --no-cache-dir pdfplumber
# --chmod: the NAS share's 770 ACLs would otherwise be baked into the image
COPY --chmod=0555 server.py /app/server.py
COPY --chmod=0444 parse-map.json /app/parse-map.json
EXPOSE 8080
CMD ["python", "-u", "/app/server.py"]
