"""yixin-web: 医信 (RMS 康复) read-only web front-end for iPhone.

Backend-for-frontend: the browser talks only to this service; this service
calls the hospital APIs server-side (handles AES login, fresh per-request
Authorization, compact JSON). The password is used once at login and never
stored; only the session token + user context are kept server-side.

Run: docker compose up -d   (qwenpaw, port 8080)
Access (Tailscale): http://<qwenpaw-tailscale-ip>:8080  (or http://qwenpaw:8080
from a device on the tailnet)
"""
import datetime
import json
import os
import secrets

from flask import Flask, jsonify, redirect, request, send_from_directory, session

import yixin_client as yc
from h5proxy import PROXY_PREFIXES, boot_from_session, proxy_to_hospital, serve_rmsapp_index

app = Flask(__name__, static_folder="static", static_url_path="")
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=datetime.timedelta(days=30),
)

HOSPITAL = os.environ.get("HOSPITAL_BASE", yc.HOSPITAL_BASE)


def _sess():
    s = session.get("yx")
    if not s:
        return None
    return yc.Session(s["token"], s["secret"], s["user"], s["system"],
                      s["cookies"])


def _save(sess):
    session["yx"] = {"token": sess.token, "secret": sess.secret,
                     "user": sess.user, "system": sess.system,
                     "cookies": sess.cookies}
    session.permanent = True


@app.route("/")
def index():
    # index.html is the app shell and changes often: never let the
    # browser cache it, or users keep seeing the old layout after a
    # deploy (the old file was cached up to 12h by Flask's default).
    resp = send_from_directory(app.static_folder, "index.html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/api/login", methods=["POST"])
def api_login():
    body = request.get_json(force=True, silent=True) or {}
    user = (body.get("userName") or "").strip()
    pw = body.get("password") or ""
    if not user or not pw:
        return jsonify({"ok": False, "message": "请输入工号和密码"}), 400
    try:
        sess = yc.login(user, pw)
    except yc.ApiError as e:
        return jsonify({"ok": False, "message": f"登录失败 ({e.code}) {e.message}"}), 401
    except Exception as e:  # noqa: BLE001 - surface as login failure
        return jsonify({"ok": False, "message": f"登录异常: {e}"}), 500
    _save(sess)
    return jsonify({"ok": True,
                    "userName": sess.user.get("userName", ""),
                    "deptName": sess.system.get("deptName", ""),
                    "hospitalName": "临沂市第三人民医院"})


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.pop("yx", None)
    return jsonify({"ok": True})


@app.route("/api/me")
def api_me():
    sess = _sess()
    if not sess:
        return jsonify({"ok": False}), 401
    return jsonify({"ok": True, "userName": sess.user.get("userName", ""),
                    "deptName": sess.system.get("deptName", ""),
                    "hospitalName": "临沂市第三人民医院"})


def _need_login():
    sess = _sess()
    if not sess:
        return None, (jsonify({"ok": False, "message": "请先登录"}), 401)
    return sess, None


@app.route("/api/therapy")
def api_therapy():
    sess, err = _need_login()
    if err:
        return err
    date = request.args.get("date") or datetime.date.today().isoformat()
    tab = request.args.get("tab") or "pending"
    page = int(request.args.get("page") or 1)
    try:
        data = yc.therapy_list(sess, date, pending=(tab != "done"),
                               page_num=page, page_size=10)
    except yc.TokenExpired:
        session.pop("yx", None)
        return jsonify({"ok": False, "message": "登录已过期，请重新登录"}), 401
    except yc.ApiError as e:
        return jsonify({"ok": False, "message": f"{e.code} {e.message}"}), 502
    return jsonify({"ok": True, "total": data.get("total"), "list": data.get("list") or []})


@app.route("/api/stats")
def api_stats():
    sess, err = _need_login()
    if err:
        return err
    date = request.args.get("date") or datetime.date.today().isoformat()
    try:
        data = yc.statistics(sess, date + " 00:00:00", date + " 23:59:59")
    except yc.TokenExpired:
        session.pop("yx", None)
        return jsonify({"ok": False, "message": "登录已过期，请重新登录"}), 401
    except yc.ApiError as e:
        return jsonify({"ok": False, "message": f"{e.code} {e.message}"}), 502
    return jsonify({"ok": True, "data": data})


# ---------------------------------------------------------------------------
# Original H5 (rmsApp) via reverse proxy + token/bridge injection.
# Login first on the custom front-end (/), then open /h5/ for the full
# original app. The H5 calls the hospital APIs itself through this proxy;
# this backend only injects the login identity, it does not re-implement
# the H5's APIs.
# ---------------------------------------------------------------------------

@app.route("/h5/")
def h5_entry():
    """Entry point for the original H5. Requires login."""
    if not _sess():
        return redirect("/")
    return redirect("/rmsApp/index.html")


@app.route("/rmsApp/")
def rmsapp_root():
    return redirect("/rmsApp/index.html")


@app.route("/rmsApp/index.html")
def rmsapp_index():
    sess = _sess()
    if not sess:
        return redirect("/")
    return serve_rmsapp_index(boot_from_session(sess))


def _proxy_cookies():
    """Hospital session cookies for the logged-in user (or None)."""
    sess = _sess()
    return sess.cookies if sess else None


@app.route("/rmsApp/<path:subpath>", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
def rmsapp_assets(subpath):
    return proxy_to_hospital("rmsApp/" + subpath,
                             cookies=_proxy_cookies())


for _prefix in PROXY_PREFIXES:
    if _prefix == "rmsApp":
        continue
    app.add_url_rule(
        f"/{_prefix}/<path:subpath>",
        endpoint=f"proxy_{_prefix}",
        view_func=lambda subpath, _p=_prefix: proxy_to_hospital(
            _p + "/" + subpath, cookies=_proxy_cookies()),
        methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    )


@app.route("/__diag", methods=["POST"])
def diag():
    """Browser-side diagnostics from the proxied H5 (no secrets)."""
    try:
        data = request.get_json(force=True, silent=True) or {}
        with open("/root/yixin-web/diag.log", "a") as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
