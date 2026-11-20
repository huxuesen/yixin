#!/bin/sh
# Warm the H5 static cache in the background after the container starts.
#
# The image already carries a pre-warmed cache from build time, but the
# hospital can update its program after the image was built. This startup
# warm fetches the CURRENT assets (discovered from the live index.html),
# so a hospital update is picked up on the next container start without
# rebuilding the image.
#
# It runs in the background and does not block gunicorn: the login page is
# available immediately, and the cache is hot within ~30-60s. Cache writes
# are atomic (temp file + rename), so a request arriving mid-warm can only
# see a complete old file or a complete new one, never a half-written file.
# If the hospital is unreachable, the warm fails silently and the proxy
# falls back to fetching on demand (plus the build-time cache).
(sleep 5; python3 warm_cache.py /root/yixin-web/static-cache >> /var/log/warm_cache.log 2>&1 || true) &
exec "$@"
