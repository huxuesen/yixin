#!/usr/bin/env python3
"""Pre-compress yixin-web static-cache files with Brotli (+gzip fallback).

For each *.bin in STATIC_CACHE_DIR, write <file>.br (Brotli q11) and
<file>.gz (gzip -9) when the compressed form is meaningfully smaller
(<90% of original). Binary assets that don't compress (images) are
skipped. Idempotent: re-running only recompresses changed/missing files.
"""
import brotli
import gzip
import hashlib
import os
import sys

CACHE_DIR = "/root/yixin-web/static-cache"


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def compress_all(cache_dir=CACHE_DIR):
    n_br = n_gz = n_skip = 0
    for name in sorted(os.listdir(cache_dir)):
        if not name.endswith(".bin"):
            continue
        path = os.path.join(cache_dir, name)
        if not os.path.isfile(path):
            continue
        data = open(path, "rb").read()
        if len(data) < 1024:
            continue
        # Brotli
        br_path = path + ".br"
        need_br = (not os.path.exists(br_path)
                   or _sha(br_path) != _sha(path + ".br.sha")
                   if os.path.exists(path + ".br.sha") else True)
        # Simpler: recompress when the .br is missing or the source is
        # newer than the .br file.
        if not os.path.exists(br_path) or \
                os.path.getmtime(path) > os.path.getmtime(br_path):
            br = brotli.compress(data, quality=11)
            if len(br) < len(data) * 0.9:
                open(br_path, "wb").write(br)
                n_br += 1
            else:
                n_skip += 1
                if os.path.exists(br_path):
                    os.remove(br_path)
        # gzip fallback
        gz_path = path + ".gz"
        if not os.path.exists(gz_path) or \
                os.path.getmtime(path) > os.path.getmtime(gz_path):
            gz = gzip.compress(data, compresslevel=9)
            if len(gz) < len(data) * 0.9:
                open(gz_path, "wb").write(gz)
                n_gz += 1
            else:
                if os.path.exists(gz_path):
                    os.remove(gz_path)
    print(f"brotli: {n_br} written, gzip: {n_gz} written, skipped(non-compressible): {n_skip}")


if __name__ == "__main__":
    compress_all(sys.argv[1] if len(sys.argv) > 1 else CACHE_DIR)
