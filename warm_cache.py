#!/usr/bin/env python3
"""Pre-warm the H5 static-asset disk cache at Docker build time.

A fresh container starts with an empty cache, so the first H5 open has to
fetch the 10.7MB main bundle from the hospital AND serve it uncompressed
(10.7MB) to the browser -- that is the slow first open. This script
downloads the big static assets once, saves them under the same
sha256(path) cache names h5proxy.py uses, and pre-compresses them
(Brotli q11 + gzip -9), so even the first request serves the small
(2.6MB) Brotli copy from local disk.

The main bundle / CSS filenames carry a content hash, so they are
discovered by parsing rmsApp/index.html at build time instead of being
hardcoded. The framework files (Vue, antd) have stable versioned paths
and are listed explicitly.

Best-effort: any download failure is logged and skipped; the proxy will
fetch the missing file on demand at runtime. Never fail the build.

Usage:
    python3 warm_cache.py [CACHE_DIR] [HOSPITAL_BASE]
    Defaults: CACHE_DIR=/root/yixin-web/static-cache
              HOSPITAL_BASE=https://lysdsrmyy-net.msunhis.com
"""
import hashlib
import os
import re
import sys

import requests

CACHE_DIR = sys.argv[1] if len(sys.argv) > 1 else "/root/yixin-web/static-cache"
HOSPITAL = (sys.argv[2] if len(sys.argv) > 2
            else "https://lysdsrmyy-net.msunhis.com")

# Framework files the bundle loads dynamically (stable versioned paths,
# not referenced from index.html, so they are listed here).
EXTRA_ASSETS = [
    "static-resource/framework/vue@2.7.14/dist/vue.runtime.esm.min.js",
    "static-resource/framework/vue@2.7.14/dist/vue.min.js",
    "static-resource/framework/ant-design-vue@1.7.8/dist/antd.min.js",
    "static-resource/components/msun-print-view/dist/scopedAntd.min.css",
    "rmsApp/js/jsqr.js",
    "rmsApp/js/vconsole.min.js",
]

UA = {"User-Agent": "Mozilla/5.0 (Linux; Android 16; 2211133C Build/UKQ1.231003.002; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/140.0.0.0 Mobile Safari/537.36"}


def cache_file(path):
    return os.path.join(CACHE_DIR, hashlib.sha256(path.encode()).hexdigest()[:16] + ".bin")


def compress(path, data):
    if len(data) < 1024:
        return
    try:
        import brotli
        br = brotli.compress(data, quality=11)
        if len(br) < len(data) * 0.9:
            open(path + ".br", "wb").write(br)
    except Exception as e:
        print(f"  brotli failed for {path}: {e}")
    try:
        import gzip
        gz = gzip.compress(data, compresslevel=9)
        if len(gz) < len(data) * 0.9:
            open(path + ".gz", "wb").write(gz)
    except Exception as e:
        print(f"  gzip failed for {path}: {e}")


def fetch(path, sess):
    url = HOSPITAL.rstrip("/") + "/" + path
    r = sess.get(url, headers=UA, timeout=60)
    if r.status_code != 200:
        print(f"  SKIP {path}: HTTP {r.status_code}")
        return None
    return r.content


def main():
    os.makedirs(CACHE_DIR, exist_ok=True)
    sess = requests.Session()
    paths = list(EXTRA_ASSETS)
    # Discover the hashed bundle / CSS from index.html.
    try:
        idx = sess.get(HOSPITAL.rstrip("/") + "/rmsApp/index.html",
                       headers=UA, timeout=30)
        if idx.status_code == 200:
            for m in re.finditer(r'(?:src|href)="(/[^"]+)"', idx.text):
                p = m.group(1).lstrip("/")
                if p.startswith(("rmsApp/assets/", "rmsApp/js/", "static-resource/")):
                    paths.append(p)
        else:
            print(f"index.html: HTTP {idx.status_code}, using hardcoded list only")
    except Exception as e:
        print(f"index.html fetch failed ({e}), using hardcoded list only")
    seen, done, total = set(), 0, 0
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        try:
            data = fetch(path, sess)
        except Exception as e:
            print(f"  SKIP {path}: {e}")
            continue
        if data is None:
            continue
        cf = cache_file(path)
        open(cf, "wb").write(data)
        compress(cf, data)
        total += len(data)
        done += 1
        print(f"  cached {path} ({len(data)} bytes)")
    print(f"warm_cache: {done} files, {total} bytes -> {CACHE_DIR}")


if __name__ == "__main__":
    main()
