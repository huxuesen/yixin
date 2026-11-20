"""Per-request Authorization generation (generateAuthorization in rmsApp).

Verified against the HttpCanary capture (2026-11-20): rebuilding the
Authorization with the captured ts/rand/body/token/secret reproduces the
captured header exactly.

- token / secret: server-issued per login, in the login response
  data.isoData[] entry for the rms system (systemCode=rms, sysId=10052).
  token = 36-char UUID, secret = 16-char hex. Both change on re-login.
- Native-app form: 4 parts (selectedSystem omitted, isYixinRuntime true).
  Authorization = b64(token).b64(ts).b64(rand).b64(md5)
  ts = Date.now() ms (13 digits), rand = 16 chars [0-9a-zA-Z],
  md5 = MD5(token + ts + rand + Sp + secret).hexdigest().upper()
  Sp (POST) = the exact compact JSON body string as sent.
  Sp (GET)  = sorted query keys concatenated as key+value, skipping
              null/"" values and empty arrays.
"""
import base64
import hashlib
import json
import random
import string
import time

_ALPHABET = string.digits + string.ascii_lowercase + string.ascii_uppercase


def _b64(s):
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


def _sp(method, query=None, data=None):
    m = (method or "").lower()
    if m in ("get", "delete"):
        if not query:
            return ""
        out = ""
        for k in sorted(query.keys()):
            v = query[k]
            if v is None or v == "":
                continue
            if isinstance(v, list) and len(v) == 0:
                continue
            out += str(k) + str(v)
        return out
    if m in ("post", "put", "patch"):
        if data is None:
            return ""
        if isinstance(data, str):
            return data
        return json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    return ""


def build_authorization(token, secret, method, query=None, data=None,
                        selected_system=None):
    """Build a fresh Authorization header (native-app 4-part form)."""
    ts = str(int(time.time() * 1000))
    rand = "".join(random.choice(_ALPHABET) for _ in range(16))
    sp = _sp(method, query, data)
    # native app omits selectedSystem -> 4 parts
    md5 = hashlib.md5("".join([token, ts, rand, sp, secret]).encode("utf-8")
                      ).hexdigest().upper()
    return ".".join(_b64(p) for p in [token, ts, rand, md5])


def get_base_token(http_session, login_response):
    """Extract (token, secret) for the rms system from the login response."""
    data = (login_response or {}).get("data") or {}
    for entry in data.get("isoData") or []:
        if entry.get("systemCode") == "rms" or str(entry.get("sysId")) == "10052":
            return entry.get("token"), entry.get("secret")
    # fall back to the first entry
    iso = data.get("isoData") or []
    if iso:
        return iso[0].get("token"), iso[0].get("secret")
    return None, None
