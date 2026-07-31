# q2tg-python

配置从项目根目录的 `.env` 读取。首次运行前复制 `.env.example` 并填写：

```dotenv
Q2TG_APP_PORT=8000
Q2TG_ONEBOT_TOKEN=replace-with-onebot-token
Q2TG_ONEBOT_MEDIA_URL=http://127.0.0.1:8000
Q2TG_ONEBOT_PROXY_URL=
Q2TG_TGBOT_TOKEN=replace-with-telegram-bot-token
Q2TG_TGBOT_ADMIN=123456789
Q2TG_TGBOT_PROXY_URL=
```

`Q2TG_APP_PORT` 可省略，默认使用 `8000`。

`Q2TG_ONEBOT_PROXY_URL` 和 `Q2TG_TGBOT_PROXY_URL` 均可选，支持 `http://`、
`https://`、`socks5://` 和 `socks5h://`：

- `Q2TG_ONEBOT_PROXY_URL` 用于 Onebot CDN 媒体下载。
- `Q2TG_TGBOT_PROXY_URL` 用于 Telegram Bot API、long polling 和 Telegram 文件下载。

进程中的同名 `Q2TG_*` 环境变量优先于 `.env`。Docker 可以直接注入全部环境变量，
无需挂载或创建 `.env` 文件。程序不会隐式读取 `HTTP_PROXY`、`HTTPS_PROXY` 或
`ALL_PROXY`；两侧代理仅由上述项目配置控制。

## Docker

镜像基于 Python 3.13 Alpine，构建阶段使用 uv 锁定安装依赖，运行阶段包含
`ffmpeg`、`ffprobe` 和 H.264/HEVC/VP9/GIF 编解码能力检查。所有配置均可通过
Docker 环境变量注入，无需把 `.env` 复制进镜像。

```bash
docker build --pull --progress=plain -t q2tg-python:latest .

docker run --detach \
  --name q2tg \
  --restart unless-stopped \
  --publish 8000:8000 \
  --volume q2tg-data:/app/data \
  --env Q2TG_ONEBOT_TOKEN="replace-with-onebot-token" \
  --env Q2TG_ONEBOT_MEDIA_URL="http://host.example:8000" \
  --env Q2TG_TGBOT_TOKEN="replace-with-telegram-bot-token" \
  --env Q2TG_TGBOT_ADMIN="123456789" \
  q2tg-python:latest
```

`Q2TG_APP_PORT` 默认是 `8000`。如果覆盖容器内监听端口，`--publish` 的容器端口也要
同步调整。`/app/data` 必须使用 volume 持久化，否则删除容器会丢失群绑定和消息映射。
容器使用非 root 用户 `q2tg`（UID/GID `10001`）；使用宿主机目录绑定 `/app/data` 时，
该目录必须允许 UID `10001` 写入。

所有构建命令均为无人值守模式：uv 使用锁文件、禁用进度输出且不下载 Python，apk 使用
`--no-cache`，不会出现安装确认或其他交互式操作。
