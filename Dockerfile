FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg gosu ca-certificates && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY static ./static
COPY docker-entrypoint.sh /usr/local/bin/flux-entrypoint
RUN chmod 755 /usr/local/bin/flux-entrypoint && mkdir -p /app/data /app/downloads/.parts \
    && useradd -r -u 10001 flux && chown -R flux:flux /app
ENV FLUX_HOST=0.0.0.0 FLUX_PORT=8000 FLUX_DATA_DIR=/app/data FLUX_DOWNLOAD_DIR=/app/downloads
EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/flux-entrypoint"]
CMD ["python", "-m", "app.main"]
