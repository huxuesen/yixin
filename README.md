# yixin — 医信 iPhone 网页版

Android-only 的医院工作 App「医信」没有 iOS 版。这个项目把它做成网页版，
iPhone 浏览器打开就能用，不用带两个手机。

只实现自己账号的正常功能，不破解付费、不绕过登录。

## 用法

打开部署地址，工号 + 密码登录，看到医院 / 科室 / 姓名，点「首页」
进入医院 App 原来的页面（治疗执行、工作量统计等都在里面）。

登录状态用 cookie 记 30 天，关掉浏览器重开不用重新登录。

## 架构

浏览器只跟本服务说话；本服务在服务端调医院接口：

- 登录：`GET /msun-his-app-yixin-server/account/login`（工号 + AES 加密密码）
- 治疗列表：`POST /msun-rehabilitation-app-rms/app/patItemExe/findItemExeList`
- 工作量统计：`POST /msun-rehabilitation-app-rms/app/statistics/findTherapyListForStatistics`

完整版（`/h5/`）是反向代理医院的原版 H5（`rmsApp`），把登录拿到的
token 注入 `sessionStorage`，再补一个 `yxplugin` 桥接 shim，
让原版页面在普通浏览器里能正常登录、正常跳转。

密码只用于本次登录，不保存；只保存 token + 用户信息在服务端会话里。

## 关键发现（2026-11-20 实测）

- 请求体必须是紧凑 JSON（`{"a":"b"}`，不能带空格），否则 openresty 直接 405。
- `Authorization` 是每次请求现算的：`base_token.时间戳.random16.MD5(...).selectedSystem?`
  各段 base64、用 `.` 连接；MD5 是 `[base_token, 时间戳, random16, 请求体/查询串, secret, selectedSystem串].join("")`。
  token 里的时间戳会过期，旧 token 调治疗执行返回 9996（统计接口相对宽松）。
- `findItemExeList` 请求体字段：`rmsPatId, exeDeptId(字符串), startDate, endDate,
  exeStatus, userSysId, inPatType, exeStatusLt, exeStatusGt, itemType,
  exeStatuses, pickerTime, deptId, pageNum, pageSize`。
  待执行 `exeStatuses=[0,10,40]`，已执行 `[20]`。
- 登录密码用 APK 里的 `AesUtil` 加密；key/IV/mode 见 `crypto.py`。
- base token 与 auth secret 的来源见 `auth.py`。
- 原版 H5 的菜单接口 `findMenuById` 对代理会话返回空，改发抓到的
  静态菜单（`menu-10052.json`）。
- H5 首页点菜单默认走原生 `openWebView` 桥，普通浏览器里没反应；
  注入 `localStorage.is_development=1` 后走 Vue Router 跳转。
- 10.7MB 主 bundle 用 Brotli 压到 2.6MB 再发（见 `compress_cache.py`）；
  8MB 打印组件（`msun-print-view`）在代理层返回空 JS，不加载。

## 部署

### Docker（推荐）

镜像由 GitHub Actions 自动构建并推送到 Docker Hub（`huxuesen/yixin`），
多架构 `linux/amd64,linux/arm64`，tag 为 `latest` 与日期时间。

```bash
docker run -d --name yixin -p 8080:8080 \
  -e SECRET_KEY=换成一个随机字符串 \
  huxuesen/yixin:latest
```

或用 `docker-compose.yml`：

```bash
docker compose up -d
```

### 直接运行

```bash
pip install -r requirements.txt
SECRET_KEY=换成一个随机字符串 gunicorn -w 2 -b 0.0.0.0:8080 app:app
```

环境变量：

- `SECRET_KEY` — Flask 会话密钥，不设则每次启动随机生成（重启后旧 cookie 失效）
- `HOSPITAL_BASE` — 医院 H5 域名，默认 `https://lysdsrmyy-net.msunhis.com`

## 文件

- `app.py` — Flask 后端（登录/会话/首页）
- `yixin_client.py` — 医院接口客户端
- `crypto.py` — 登录密码 AES 加密
- `auth.py` — 每次请求的 Authorization 生成
- `h5proxy.py` — 原版 H5 反向代理（注入 token/桥接 shim/静态缓存/Brotli/菜单回退/打印组件拦截）
- `compress_cache.py` — 静态缓存预压缩（Brotli/gzip）
- `menu-10052.json` — 抓到的原版菜单（findMenuById 回退用）
- `static/index.html` — 登录页与身份页
- `static/icon.png` — App 图标
- `.github/workflows/docker.yml` — 构建并推送 Docker 镜像到 Docker Hub
