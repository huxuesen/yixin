FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Pre-warm the H5 static-asset disk cache (main bundle, Vue, antd, CSS).
# A fresh container starts with an empty cache, so the first H5 open has
# to fetch the 10.7MB bundle from the hospital and serve it uncompressed.
# Baking the pre-compressed cache into the image makes even the first open
# serve the 2.6MB Brotli copy from local disk. Best-effort: if the hospital
# is unreachable from the build runner, the build still succeeds and the
# cache populates on first use at runtime.
RUN python3 warm_cache.py /root/yixin-web/static-cache || echo "warm_cache failed (non-fatal), cache will populate at runtime"
# Startup entrypoint: re-warms the cache in the background after the
# container starts (picks up hospital updates made after the image was
# built), then execs gunicorn as PID 1.
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh
ENV PORT=8080
EXPOSE 8080
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:8080", "app:app"]
