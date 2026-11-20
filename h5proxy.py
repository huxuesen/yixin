"""Reverse proxy for the hospital's original rmsApp H5.

Why: the H5 has no login page of its own -- the native app logs in and hands
it the token via the native bridge (yxplugin) into sessionStorage. In a plain
browser the bridge does not exist, so we:

  1. Proxy the hospital origin (rmsApp statics + all /msun-* API prefixes)
     through this backend, so the H5 is same-origin with our injection.
  2. Inject a bootstrap script into rmsApp/index.html (before the H5 bundle)
     that defines window.yxplugin (webJsToPhone/jsBridge) and pre-seeds
     sessionStorage, using the token/secret from the already-logged-in
     Flask session. The H5 then runs exactly as in the native app: it calls
     getUserInfo() via the shimmed bridge, App.init() copies token/secret
     into currentUser, and its own interceptor generates the per-request
     Authorization header. This backend never re-implements the H5 APIs.

Authorization form: the H5 sends 5 parts (with Tp) in a plain browser when
all 7 selectedSystem fields are truthy. The 4-part native form is the only
one verified against the server, so we deliberately leave
selectedSystem.userSysName empty -> Tp is not built -> 4-part, identical to
the native app. deptId/userSysId (used in request bodies) stay real.
"""
import hashlib
import json
import os

import requests
from flask import Response, request

from yixin_client import HOSPITAL_BASE

# Disk cache for the H5's large static files (bundle, CSS, Vue, print libs).
# The hospital serves them without usable cache headers, so the browser
# re-downloads the 10.7MB bundle on every open -- over frp on a phone that
# takes a minute and the user refreshes, restarting the download. These
# files are content-hashed (index-CgekLUTu.js), so they are immutable and
# safe to cache forever. Served from qwenpaw disk with a long max-age so
# the browser caches them too.
STATIC_CACHE_DIR = "/root/yixin-web/static-cache"
_STATIC_PREFIXES = ("rmsApp/assets/", "rmsApp/js/", "static-resource/")


def _cache_path(path):
    """Cache file for a static path, or None if not cacheable."""
    if not any(path.startswith(p) for p in _STATIC_PREFIXES):
        return None
    safe = hashlib.sha256(path.encode()).hexdigest()[:16]
    return os.path.join(STATIC_CACHE_DIR, safe + ".bin"), path


def _guess_type(path):
    if path.endswith(".js"):
        return "application/javascript"
    if path.endswith(".css"):
        return "text/css"
    if path.endswith(".png"):
        return "image/png"
    if path.endswith(".jpg") or path.endswith(".jpeg"):
        return "image/jpeg"
    if path.endswith(".json"):
        return "application/json"
    if path.endswith(".woff2"):
        return "font/woff2"
    if path.endswith(".woff"):
        return "font/woff"
    if path.endswith(".svg"):
        return "image/svg+xml"
    return "application/octet-stream"


def _atomic_write(path, data):
    """Write via a temp file + atomic rename, so a concurrent reader (or
    a concurrent warm_cache.py / another gunicorn worker) never sees a
    half-written file. Without this, two writers racing on the same cache
    file could leave a corrupt bundle that the proxy then serves on every
    cache hit."""
    tmp = path + ".tmp.%d" % os.getpid()
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _write_compressed(cache_file, data):
    """Write <file>.br (Brotli q11) and <file>.gz (gzip -9) next to a
    cached asset, when the compressed form is meaningfully smaller.
    Best-effort: failures are swallowed so caching never breaks."""
    if len(data) < 1024:
        return
    try:
        import brotli
        br = brotli.compress(data, quality=11)
        if len(br) < len(data) * 0.9:
            _atomic_write(cache_file + ".br", br)
    except Exception:
        pass
    try:
        import gzip
        gz = gzip.compress(data, compresslevel=9)
        if len(gz) < len(data) * 0.9:
            _atomic_write(cache_file + ".gz", gz)
    except Exception:
        pass


# API/static prefixes the H5 talks to (relative to the hospital origin).
# Found by grepping the H5 bundle for absolute paths. "static-resource" is
# the Vue runtime the bundle dynamically imports -- without it the page
# stays blank. "/api/..." is the hospital's generic API gateway (the H5's
# /api/* calls); our own /api/login etc. are exact routes and win over the
# /api/<path> converter in Flask's routing.
PROXY_PREFIXES = (
    "rmsApp",
    "static-resource",
    "api",
    "msun-rehabilitation-app-rms",
    "msun-middle-base-common",
    "msun-middle-base-config",
    "msun-middle-base-patient",
    "msun-middle-base-staff",
    "msun-his-app-ca",
    "msun-his-app-common",
    "msun-his-app-mzsk",
    "msun-his-app-yixin-server",
    "msun-ai-agent-tcm",
    "msun-middle-open-settlepay",
    "msun-websocket-server",
)

# Hop-by-hop / request headers we do not forward to the hospital.
DROP_REQ_HEADERS = {
    "host", "connection", "keep-alive", "proxy-connection",
    "te", "trailer", "transfer-encoding", "upgrade",
    "content-length",
}
# Response headers we do not forward back to the browser.
DROP_RESP_HEADERS = {
    "connection", "keep-alive", "transfer-encoding",
    "content-encoding", "content-length",
}

_session = requests.Session()
_session.headers.update({
    "User-Agent": ("Mozilla/5.0 (Linux; Android 16; 2211133C Build/BP2A.250605.031.A3; wv) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/143.0.7499.192 "
                   "Mobile Safari/537.36 android yx_internet_doctor_his yx_android_new_platform_his"),
})


def _forward_headers(incoming):
    """Build the headers to send to the hospital for a proxied request."""
    out = {}
    for k, v in incoming.items():
        if k.lower() in DROP_REQ_HEADERS:
            continue
        out[k] = v
    # Make the request look like it comes from the hospital's own H5.
    out["Origin"] = HOSPITAL_BASE
    out["Referer"] = HOSPITAL_BASE + "//rmsApp/"
    out["Host"] = "lysdsrmyy-net.msunhis.com"
    return out


def proxy_to_hospital(path, cookies=None):
    """Forward the current request to the hospital and stream the response.

    cookies: the hospital session cookies from login (HWWAFSESID,
    MOBILE_SIGN, MOBILE_SIGN_TOKEN, GRAY_TOKEN, ...). The browser does not
    have them (they live in this backend's login session), so the proxy
    must attach them -- without them the hospital's openresty answers 405
    on the authenticated config/data endpoints.
    """
    # Skip the 8MB print component (msun-print-view). The H5 loads it at
    # startup via initMsunPrintView() -- but ONLY in a plain browser. The
    # native 医信 App skips it (skipInYixin: the UA contains yx_, so the
    # init list filters it out). Our proxied H5 runs in a plain browser,
    # so it loads the 8MB file on every open even though nobody prints
    # from the phone. Return an empty script instead: the H5 keeps
    # working, only the print/export-PDF function is unavailable.
    # (Same for the medical-workers test-print component.)
    if "msun-print-view/dist/msunPrintView.umd.js" in path or \
            "msun-medical-workers-test-print" in path:
        return Response(b"", status=200, headers={
            "Content-Type": "application/javascript",
            "Cache-Control": "public, max-age=31536000, immutable",
            "Access-Control-Allow-Origin": "*",
        })
    # Static assets: serve from disk cache when available.
    # Serve a pre-compressed copy when the browser accepts it: the main
    # bundle is 10.7MB but Brotli (q11) shrinks it to ~2.6MB, which is
    # what makes the H5 open fast on a phone connection. The browser
    # decompresses transparently; Content-Type stays the original type.
    cache = _cache_path(path) if request.method == "GET" else None
    if cache:
        cache_file, _orig = cache
        if os.path.exists(cache_file):
            enc = request.headers.get("Accept-Encoding", "")
            data = None
            encoding = None
            if "br" in enc and os.path.exists(cache_file + ".br"):
                with open(cache_file + ".br", "rb") as f:
                    data = f.read()
                encoding = "br"
            elif "gzip" in enc and os.path.exists(cache_file + ".gz"):
                with open(cache_file + ".gz", "rb") as f:
                    data = f.read()
                encoding = "gzip"
            if data is None:
                with open(cache_file, "rb") as f:
                    data = f.read()
            # Access-Control-Allow-Origin is REQUIRED: the H5 loads its
            # main bundle as <script type="module" crossorigin>, and a
            # module without a CORS header is blocked from executing
            # (silently -- no window.onerror, app never mounts, no API
            # calls). The hospital sends it; our cache must too.
            resp_headers = {
                "Cache-Control": "public, max-age=31536000, immutable",
                "Content-Type": _guess_type(path),
                "Access-Control-Allow-Origin": "*",
                "Vary": "Accept-Encoding",
            }
            if encoding:
                resp_headers["Content-Encoding"] = encoding
            return Response(data, status=200, headers=resp_headers)
    url = HOSPITAL_BASE + "/" + path
    headers = _forward_headers(request.headers)
    if cookies:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    try:
        r = _session.request(
            method=request.method,
            url=url,
            params=request.args,
            data=request.get_data(),
            headers=headers,
            timeout=60,
            stream=True,
            allow_redirects=False,
        )
    except requests.RequestException as e:
        return Response(json.dumps({"ok": False, "message": f"代理到医院失败: {e}"}),
                        status=502, mimetype="application/json")
    headers = {k: v for k, v in r.headers.items()
               if k.lower() not in DROP_RESP_HEADERS}
    # Save static assets to disk cache for next time (plus pre-compressed
    # Brotli/gzip copies so later requests serve the small form).
    if cache and r.status_code == 200:
        try:
            os.makedirs(STATIC_CACHE_DIR, exist_ok=True)
            content = r.content
            _atomic_write(cache[0], content)
            _write_compressed(cache[0], content)
            headers["Cache-Control"] = "public, max-age=31536000, immutable"
            headers["Access-Control-Allow-Origin"] = "*"
            return Response(content, status=200, headers=headers)
        except Exception:
            pass
    # findMenuById: the hospital returns data: [] (empty menu) for this
    # proxied session even with a valid Authorization, while the native
    # app gets the full 13KB RMS menu. Serve the captured menu JSON
    # (menu-10052.json, the native app's own response for menuId=10052)
    # when the server's data is empty, so the H5 renders its navigation.
    # If the server ever returns a real menu, prefer the server's.
    if "findMenuById" in path:
        try:
            content = r.content
            try:
                parsed = json.loads(content)
                data = parsed.get("data")
            except Exception:
                parsed, data = None, None
            if parsed is not None and not data:
                try:
                    with open(os.path.join(os.path.dirname(__file__),
                                           "menu-10052.json"), "rb") as f:
                        menu_bytes = f.read()
                    headers["Content-Type"] = "application/json;charset=UTF-8"
                    return Response(menu_bytes, status=200, headers=headers)
                except Exception:
                    pass
            return Response(content, status=r.status_code, headers=headers)
        except Exception:
            pass
    return Response(r.iter_content(chunk_size=65536), status=r.status_code,
                    headers=headers, direct_passthrough=True)


def bootstrap_script(boot):
    """JS injected into rmsApp/index.html before the H5 bundle.

    boot: dict with token, secret, deptId, deptCode, deptName, systemId,
    systemName, userSysId, userSysName, userName, hospitalId, hospitalName.
    """
    b = json.dumps(boot, ensure_ascii=False)
    return (
        "<script>\n"
        "(function(){\n"
        "  var BOOT = " + b + ";\n"
        "  // Route menu taps through Vue Router instead of the native\n"
        "  // openWebView bridge. Home.goNewPage() does:\n"
        "  //   localStorage.is_development ? $router.push(xp)\n"
        "  //                            : openWebView(origin+path+'#/'+xp)\n"
        "  // openWebView calls yxplugin.webJsToPhone('yx_openWebview_1'),\n"
        "  // which only exists in the native App -- in a plain browser\n"
        "  // the tap silently does nothing. is_development is read only\n"
        "  // here (single use in the whole bundle), so this is safe.\n"
        "  try { localStorage.setItem('is_development', '1'); } catch (e) {}\n"
        "  // Pre-seed sessionStorage so the H5 finds its identity even\n"
        "  // before the bridge callback runs.\n"
        "  try {\n"
        "    var cur = {token: BOOT.token, secret: BOOT.secret,\n"
        "      userId: BOOT.userSysId, userSysId: BOOT.userSysId,\n"
        "      userName: BOOT.userName, deptId: BOOT.deptId,\n"
        "      deptName: BOOT.deptName, hospitalId: BOOT.hospitalId};\n"
        "    sessionStorage.setItem('currentUser', JSON.stringify(cur));\n"
        "    // selectedSystem: userSysName intentionally empty so the\n"
        "    // H5 interceptor omits Tp -> 4-part Authorization (the\n"
        "    // native-app form, the only one verified against the server).\n"
        "    // deptId/userSysId stay real: the H5 reads them for bodies.\n"
        "    var sel = {deptCode: BOOT.deptCode, deptId: BOOT.deptId,\n"
        "      deptName: BOOT.deptName, systemId: BOOT.systemId,\n"
        "      sysytemId: BOOT.systemId, systemName: BOOT.systemName,\n"
        "      userSysId: BOOT.userSysId, userSysName: ''};\n"
        "    sessionStorage.setItem('selectedSystem', JSON.stringify(sel));\n"
        "    sessionStorage.setItem('loginUser', JSON.stringify(cur));\n"
        "  } catch (e) {}\n"
        "  // Shim the native bridge the H5 uses for getUserInfo().\n"
        "  // In a browser it calls yxplugin.webJsToPhone('yx_userinfo_1')\n"
        "  // and waits up to 1s; we answer synchronously with the login\n"
        "  // response's isoUser (token/secret included).\n"
        "  var isoUser = {token: BOOT.token, secret: BOOT.secret,\n"
        "    userId: BOOT.userSysId, userSysId: BOOT.userSysId,\n"
        "    userName: BOOT.userName, deptId: BOOT.deptId,\n"
        "    deptCode: BOOT.deptCode, deptName: BOOT.deptName,\n"
        "    hospitalId: BOOT.hospitalId, hospitalName: BOOT.hospitalName,\n"
        "    systemCode: 'rms', sysId: BOOT.systemId,\n"
        "    systemName: BOOT.systemName};\n"
        "  function answer(name, ok, fail) {\n"
        "    if (name === 'yx_userinfo_1' || name === 'yx_get_doctor_info_1') {\n"
        "      try { ok({isoUser: isoUser}); } catch (e) {}\n"
        "    } else if (typeof fail === 'function') {\n"
        "      try { fail('not supported in browser'); } catch (e) {}\n"
        "    }\n"
        "  }\n"
        "  // Define yxplugin with a getter-only property so the external\n"
        "  // yixinBridge script (loaded in <head>) cannot overwrite our shim\n"
        "  // by plain assignment. The H5 reads bare `yxplugin`, which\n"
        "  // resolves to window.yxplugin, i.e. to this getter's value.\n"
        "  var shim = {};\n"
        "  shim.webJsToPhone = function (n, d, ok, fail) { answer(n, ok, fail); };\n"
        "  shim.jsBridge = function (n, d, ok, fail) { answer(n, ok, fail); };\n"
        "  try {\n"
        "    Object.defineProperty(window, 'yxplugin', {\n"
        "      configurable: true, enumerable: true, get: function () { return shim; }\n"
        "    });\n"
        "  } catch (e) { window.yxplugin = shim; }\n"
        "  window.webJsToPhone = shim.webJsToPhone;\n"
        "  // Re-seed sessionStorage shortly after load: App.init() rewrites\n"
        "  // currentUser/selectedSystem from the bridge result, and if the\n"
        "  // bridge call ever resolves empty we restore the login identity\n"
        "  // so the H5's authInterceptor still finds a token.\n"
        "  function reseed() {\n"
        "    try {\n"
        "      var c = JSON.parse(sessionStorage.getItem('currentUser') || '{}');\n"
        "      if (!c.token) {\n"
        "        sessionStorage.setItem('currentUser', JSON.stringify({\n"
        "          token: BOOT.token, secret: BOOT.secret,\n"
        "          userId: BOOT.userSysId, userSysId: BOOT.userSysId,\n"
        "          userName: BOOT.userName, deptId: BOOT.deptId,\n"
        "          deptName: BOOT.deptName, hospitalId: BOOT.hospitalId}));\n"
        "      }\n"
        "      var s = JSON.parse(sessionStorage.getItem('selectedSystem') || '{}');\n"
        "      if (!s.deptId) {\n"
        "        sessionStorage.setItem('selectedSystem', JSON.stringify({\n"
        "          deptCode: BOOT.deptCode, deptId: BOOT.deptId,\n"
        "          deptName: BOOT.deptName, systemId: BOOT.systemId,\n"
        "          sysytemId: BOOT.systemId, systemName: BOOT.systemName,\n"
        "          userSysId: BOOT.userSysId, userSysName: ''}));\n"
        "      }\n"
        "    } catch (e) {}\n"
        "  }\n"
        "  setTimeout(reseed, 800);\n"
        "  setTimeout(reseed, 2500);\n"
        "  setTimeout(reseed, 5000);\n"
        "  // ---- diagnostics: report browser-side state to the server ----\n"
        "  // (no token/secret values, only presence flags and lengths)\n"
        "  function diag(kind, extra) {\n"
        "    try {\n"
        "      var c = null, s = null;\n"
        "      try { c = JSON.parse(sessionStorage.getItem('currentUser') || 'null'); } catch (e) {}\n"
        "      try { s = JSON.parse(sessionStorage.getItem('selectedSystem') || 'null'); } catch (e) {}\n"
        "      var payload = {\n"
        "        kind: kind, t: Date.now(), url: location.href,\n"
        "        hash: location.hash,\n"
        "        hasYxplugin: typeof window.yxplugin !== 'undefined',\n"
        "        yxpluginKeys: (typeof window.yxplugin === 'object' && window.yxplugin)\n"
        "          ? Object.keys(window.yxplugin) : [],\n"
        "        bridgeCalled: !!window.__yxBridgeCalled,\n"
        "        curHasToken: !!(c && c.token),\n"
        "        curTokenLen: (c && c.token) ? String(c.token).length : 0,\n"
        "        curHasSecret: !!(c && c.secret),\n"
        "        selHasDept: !!(s && s.deptId),\n"
        "        selDeptId: (s && s.deptId) ? String(s.deptId) : '',\n"
        "        loginUserPresent: !!sessionStorage.getItem('loginUser'),\n"
        "        ssKeys: Object.keys(sessionStorage)\n"
        "      };\n"
        "      if (extra) for (var k in extra) payload[k] = extra[k];\n"
        "      fetch('/__diag', {method: 'POST',\n"
        "        headers: {'Content-Type': 'application/json'},\n"
        "        body: JSON.stringify(payload)}).catch(function () {});\n"
        "    } catch (e) {}\n"
        "  }\n"
        "  // mark when our bridge shim is actually invoked\n"
        "  var _ans = answer;\n"
        "  answer = function (n, ok, fail) { window.__yxBridgeCalled = true; _ans(n, ok, fail); };\n"
        "  shim.webJsToPhone = function (n, d, ok, fail) { answer(n, ok, fail); };\n"
        "  shim.jsBridge = function (n, d, ok, fail) { answer(n, ok, fail); };\n"
        "  setTimeout(function () { diag('load+1s'); }, 1000);\n"
        "  setTimeout(function () { diag('load+4s'); }, 4000);\n"
        "  setTimeout(function () { diag('load+10s'); }, 10000);\n"
        "  window.addEventListener('hashchange', function () { diag('hashchange'); });\n"
        "  window.addEventListener('error', function (e) {\n"
        "    diag('jserror', {msg: String(e.message || ''), src: String(e.filename || ''),\n"
        "      line: e.lineno || 0});\n"
        "  });\n"
        "})();\n"
        "</script>\n"
    )


def serve_rmsapp_index(boot):
    """Fetch the hospital's rmsApp/index.html, inject bootstrap, serve it.

    Injected TWICE: once right after <head> (early, so sessionStorage is
    seeded ASAP) and once right before </body> (late). The late copy matters
    because index.html loads an external bridge script
    (yixinBridge-1.2.7.min.js, blocking, in <head>) which can overwrite
    window.yxplugin; the main H5 bundle is a <script type="module">, which
    only executes after the whole document is parsed, so a plain script
    before </body> still runs before the H5 -- re-asserting our shim after
    the bridge script has loaded.
    """
    try:
        r = _session.get(HOSPITAL_BASE + "/rmsApp/index.html", timeout=30)
    except requests.RequestException as e:
        return Response(json.dumps({"ok": False, "message": f"取 H5 首页失败: {e}"}),
                        status=502, mimetype="application/json")
    html = r.text
    # Remove the external yixinBridge script. It defines a top-level
    # (lexical) `yxplugin` binding which shadows window.yxplugin, so the
    # H5's bare `yxplugin` would resolve to the native bridge (dead in a
    # browser) instead of our shim. Without it, bare `yxplugin` resolves
    # to window.yxplugin, i.e. to our getter below. The bridge is only
    # needed for native features (scan/camera); the data pages do not use it.
    import re as _re
    html = _re.sub(r'<script[^>]*yixinBridge[^>]*></script>\s*', '', html)
    inject = bootstrap_script(boot)
    if "<head>" in html:
        html = html.replace("<head>", "<head>\n" + inject, 1)
    else:
        html = inject + html
    if "</body>" in html:
        html = html.replace("</body>", inject + "</body>", 1)
    else:
        html = html + inject
    return Response(html, status=200, mimetype="text/html")


def boot_from_session(sess):
    """Build the bootstrap dict from a logged-in yixin Session."""
    sysd = sess.system or {}
    user = sess.user or {}
    return {
        "token": sess.token,
        "secret": sess.secret,
        "deptId": str(sysd.get("deptId", "")),
        "deptCode": str(sysd.get("deptCode", "")),
        "deptName": str(sysd.get("deptName", "")),
        "systemId": "10052",
        "systemName": "康复系统",
        "userSysId": str(sysd.get("userSysId", sess.user_sys_id)),
        "userSysName": str(sysd.get("userSysName", user.get("userName", ""))),
        "userName": str(user.get("userName", sysd.get("userSysName", ""))),
        "hospitalId": str(sysd.get("hospitalId", sess.hospital_id)),
        "hospitalName": "临沂市第三人民医院",
    }
