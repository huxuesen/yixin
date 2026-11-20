"""Client for the hospital RMS (rehab) backend behind 医信.

Verified against live captures (2026-11-20):
- Hospital base: https://lysdsrmyy-net.msunhis.com
- Login: GET /msun-his-app-yixin-server/account/login (userName=工号,
  password=AES-encrypted, appType=yx_android_platform_his)
- RMS APIs: POST /msun-rehabilitation-app-rms/app/...
  - statistics/findTherapyListForStatistics {startDate, endDate}
  - patItemExe/findItemExeList {rmsPatId, exeDeptId, startDate, endDate,
    exeStatus, userSysId, inPatType, exeStatusLt, exeStatusGt, itemType,
    exeStatuses, pickerTime, deptId, pageNum, pageSize}
- Gotchas:
  - Request body MUST be compact JSON (no spaces) or openresty returns 405.
  - Authorization is generated per request (see auth_header): it embeds a
    timestamp, so a token captured hours ago is rejected with code 9996
    on findItemExeList (statistics is more lenient). Always generate fresh.
  - The 9996/9990/9991/9992/9994/9997/9999 codes all mean "token invalid".
"""
import hashlib
import json
import random
import string
import time
import urllib.parse

import requests

HOSPITAL_BASE = "https://lysdsrmyy-net.msunhis.com"
RMS_BASE = HOSPITAL_BASE + "/msun-rehabilitation-app-rms"
SERVER_BASE = HOSPITAL_BASE + "/msun-his-app-yixin-server"

UA = ("Mozilla/5.0 (Linux; Android 16; 2211133C Build/BP2A.250605.031.A3; wv) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/143.0.7499.192 "
      "Mobile Safari/537.36 android yx_internet_doctor_his yx_android_new_platform_his")

# ---------------------------------------------------------------------------
# Auth primitives (filled in from APK analysis; see README for the derivation)
# ---------------------------------------------------------------------------
# AES_KEY / AES_IV / AES_MODE: set by crypto.py once extracted from the APK.
# auth_secret / base-token source: set by auth.py once extracted.
from crypto import encrypt_password  # noqa: F401  (AES-encrypt the login password)
from auth import build_authorization, get_base_token  # noqa: F401


class TokenExpired(Exception):
    pass


class ApiError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


TOKEN_CODES = {"9990", "9991", "9992", "9994", "9996", "9997", "9999"}


def _headers(auth, referer=None):
    h = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "User-Agent": UA,
        "Origin": HOSPITAL_BASE,
        "Referer": (referer or HOSPITAL_BASE + "//rmsApp/"),
        "X-Requested-With": "com.msunsoft.platform_internet_isomerism",
        "Pragma": "no-cache",
        "Cache-Control": "no-cache",
        "sec-ch-ua-platform": '"Android"',
        "sec-ch-ua": '"Android WebView";v="143", "Chromium";v="143", "Not A(Brand";v="24"',
        "sec-ch-ua-mobile": "?1",
        "Msun-VP-Mark": "",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Content-Type": "application/json",
    }
    if auth:
        h["Authorization"] = auth
    return h


def _compact(obj):
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class Session:
    """One logged-in user session: token/secret + user/system context."""

    def __init__(self, token, secret, user, system, cookies):
        self.token = token        # 36-char UUID from login isoData (rms)
        self.secret = secret      # 16-char hex from login isoData (rms)
        self.user = user          # dict from findRmsDictUserByUserSysId
        self.system = system      # dict from login isoData (dept etc.)
        self.cookies = cookies    # requests cookiejar dict

    @property
    def user_sys_id(self):
        return str(self.user.get("userSysId", ""))

    @property
    def dept_id(self):
        return str(self.system.get("deptId", self.user.get("deptId", "")))

    @property
    def hospital_id(self):
        return str(self.user.get("hospitalId", ""))

    def auth_for(self, method, url_path, body_obj=None, query=None):
        # native-app form: 4 parts, selectedSystem omitted
        return build_authorization(self.token, self.secret, method,
                                   query=query, data=body_obj)


def login(user_name, password, http=None):
    """Log in with 工号 + password. Returns a Session. Raises ApiError."""
    s = http or requests.Session()
    params = {
        "isAuth": "false",
        "showLoadingDialog": "false",
        "zyOs": "Android",
        "zyOsVersion": "16",
        "zyModel": "2211133C",
        "zyBrand": "Xiaomi",
        "appTypeFlag": "server",
        "versionName": "4.56.2",
        "versionCode": "24071500",
        "userName": user_name,
        "password": encrypt_password(password),
        "isDeptGroup": "1",
        "appType": "yx_android_platform_his",
        "apiDomain": SERVER_BASE + "/",
        "fromUrl": "lysdsrmyy.msunhis.com",
    }
    r = s.get(SERVER_BASE + "/account/login", params=params,
              headers=_headers(None), timeout=30)
    d = r.json()
    if not d.get("success"):
        raise ApiError(str(d.get("code")), d.get("message", "登录失败"))
    data = d.get("data") or {}
    # isoData: one entry per system; pick the rms (康复) entry
    iso = data.get("isoData") or []
    dept = {}
    for e in iso:
        if e.get("systemCode") == "rms" or str(e.get("sysId")) == "10052":
            dept = e
            break
    if not dept and iso:
        dept = iso[0]
    # token/secret for this login (change on re-login)
    token, secret = get_base_token(s, d)
    if not token or not secret:
        raise ApiError("no-token", "登录成功但未拿到 token")
    # user dict (deptId, userSysId, hospitalId, flags) -- fetched BEFORE
    # selectedSystem: the app's selectedSystem body needs userGrayId, which
    # is this record's userId. This RMS endpoint REQUIRES the Authorization
    # header (like all RMS calls); without it the server answers 9999 and
    # the user record comes back empty, which also blanks the identity
    # card and leaves userGrayId empty in selectedSystem.
    user_sys_id = str(dept.get("userId", ""))
    user = {}
    try:
        from auth import build_authorization
        auth = build_authorization(token, secret, "GET",
                                   {"userSysId": user_sys_id}, None)
        h = _headers(None)
        h["Authorization"] = auth
        r3 = s.get(RMS_BASE + "/app/rmsDictUser/findRmsDictUserByUserSysId",
                   params={"userSysId": user_sys_id},
                   headers=h, timeout=30)
        user = (r3.json().get("data") or {})
    except Exception:
        pass
    # selectedSystem: the app POSTs account/selectedSystem after login.
    # The FULL body matters: the server uses it to register which system
    # (sysId 10052 = 康复) this session is using, and findMenuById returns
    # the menu for the selected system. A minimal {deptId, hospitalId}
    # body does not register the system, so findMenuById comes back with
    # data: [] (empty menu). Body mirrors the native app's request.
    try:
        s.post(SERVER_BASE + "/account/selectedSystem",
               data=_compact({
                   "zyBrand": "Xiaomi",
                   "userGrayId": str(user.get("userId", "")),
                   "sysId": "10052",
                   "isMz": "0",
                   "deptId": dept.get("deptId"),
                   "zyModel": "2211133C",
                   "userSysId": user_sys_id,
                   "hospitalName": "临沂市第三人民医院",
                   "versionName": "4.56.2",
                   "versionCode": "24071500",
                   "orgId": str(user.get("orgId", dept.get("orgId", "10641"))),
                   "token": token,
                   "zyOsVersion": "16",
                   "hospitalId": dept.get("hospitalId"),
                   "zyManufacturer": "Xiaomi",
                   "appTypeFlag": "server",
                   "zyOs": "Android",
               }),
               headers=_headers(None), timeout=30)
    except Exception:
        pass
    cookies = {c.name: c.value for c in s.cookies}
    # isoData's deptName/hospitalName/userName come back empty from the
    # server, so prefer the RMS user record's values (it has the real
    # deptName 康复医学科 and userName 凌思杰）.
    system = {"deptId": dept.get("deptId"),
              "deptName": dept.get("deptName") or user.get("deptName", ""),
              "deptCode": dept.get("deptCode"),
              "hospitalId": dept.get("hospitalId"),
              "userSysId": user_sys_id,
              "userSysName": user.get("userName", "")}
    return Session(token, secret, user, system, cookies)


def _rms_post(sess, path, body_obj, http=None):
    s = http or requests.Session()
    for k, v in sess.cookies.items():
        s.cookies.set(k, v)
    url = RMS_BASE + path
    auth = sess.auth_for("POST", path, body_obj=body_obj)
    r = s.post(url, data=_compact(body_obj), headers=_headers(auth),
               timeout=30)
    d = r.json()
    if not d.get("success"):
        code = str(d.get("code", ""))
        if code in TOKEN_CODES:
            raise TokenExpired(code, d.get("message", ""))
        raise ApiError(code, d.get("message", "请求失败"))
    return d.get("data") or {}


def therapy_list(sess, date_str, pending=True, page_num=1, page_size=10,
                 http=None):
    """findItemExeList for one day. pending -> exeStatuses [0,10,40];
    done -> [20]. date_str: YYYY-MM-DD."""
    body = {
        "rmsPatId": "-1",
        "exeDeptId": sess.dept_id,
        "startDate": date_str + " 00:00:00",
        "endDate": date_str + " 23:59:59",
        "exeStatus": -1,
        "userSysId": sess.user_sys_id,
        "inPatType": -1,
        "exeStatusLt": -1,
        "exeStatusGt": "-1",
        "itemType": -1,
        "exeStatuses": [0, 10, 40] if pending else [20],
        "pickerTime": date_str,
        "deptId": "-1",
        "pageNum": page_num,
        "pageSize": page_size,
    }
    return _rms_post(sess, "/app/patItemExe/findItemExeList", body, http=http)


def statistics(sess, start_date, end_date, http=None):
    """findTherapyListForStatistics. Dates: YYYY-MM-DD HH:MM:SS."""
    return _rms_post(sess, "/app/statistics/findTherapyListForStatistics",
                     {"startDate": start_date, "endDate": end_date}, http=http)
