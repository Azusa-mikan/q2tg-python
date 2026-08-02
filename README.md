# Q2TG-Python

*我希望这是我最后一个大型项目了。*

> [!WARNING]
> 使用此项目时，应严格遵守《中华人民共和国》相关法律。
>
> 因使用此项目导致的法律纠纷，本人概不负责。

> [!NOTE]
> 本项目所有代码均由 AI 编写。
>
> 若不满，可自行更换其他相关项目。

## 介绍

Q2TG-Python 是一个基于 OneBot 11 与 Telegram Bot API 的双向群消息桥接服务。
它通过 OneBot 反向 WebSocket 接收消息，并将已绑定的 OneBot 群与 Telegram 群连接起来。

## 功能

- OneBot 群与 Telegram 群一对一绑定
- 双向转发文本、图片、图片组、GIF、文件、视频、语音和贴纸
- 保留消息回复关系与来源引用
- 映射两侧消息并支持双向撤回
- HEVC、VP9、WebP 等媒体兼容转换
- 根据文件签名识别 GIF、MP4 等媒体格式，不依赖远端 MIME 或扩展名
- SQLite、MySQL/MariaDB 或 PostgreSQL 持久化群绑定、群设置和消息映射
- 独立配置两侧 HTTP 或 SOCKS5 代理
- 消息发送失败重试和最终失败通知
- 通过 Telegram `/status` 查看内存、队列和最近媒体转换耗时

各消息类型的支持程度、限制与待办事项见 [消息类型支持清单](TODO.md)。

## 工作方式

```text
OneBot 群
    │
    │ OneBot 11 反向 WebSocket
    ▼
Q2TG-Python ─── SQL 数据库 / 媒体处理 ─── Telegram Bot API
    ▲                                      │
    └──────────────────────────────────────┘
                 双向消息转发
```

服务启动后会运行 FastAPI、数据库和媒体处理任务；OneBot WebSocket 通过鉴权后，才会在
该连接存续期间运行 Telegram Bot 与消息消费者。群绑定及消息映射默认保存在
`data/q2tg.db`，也可改用 MySQL/MariaDB 或 PostgreSQL；消息映射默认保留 30 天。

## 环境要求

- 部署：Docker Engine、Docker Compose 插件、Telegram Bot token
- 开发：Python 3.13+、[uv](https://docs.astral.sh/uv/)、ffmpeg、ffprobe

### 资源配置

Q2TG-Python 独立运行（OneBot 由其他主机或服务提供）时：

| 配置 | CPU | 物理内存 | Swap | 可用存储 |
| --- | --- | --- | --- | --- |
| 最低配置 | 1 核 | 2 GB | 建议 1 GB | 5 GB |
| 推荐配置 | 2 核 | 4 GB | 1–2 GB | 10 GB |

最低配置适合个人使用、消息量较低且媒体转码不频繁的场景；长期运行、群消息较多或经常
转发视频与贴纸时，建议使用推荐配置。上述可用存储已经包含 SQLite 数据、Docker 镜像、
日志以及项目临时媒体所需空间，无需再为临时目录单独叠加容量。

如果使用仓库提供的 Compose，在同一台主机运行 Q2TG-Python、SnowLuma 和 QQ，整机推荐
配置为：

| CPU | 物理内存 | Swap | 可用存储 |
| --- | --- | --- | --- |
| 4 核 | 8 GB | 2–4 GB | 30 GB |

“可用存储”指拉取镜像和启动服务前磁盘的实际剩余空间，不是磁盘标称总容量。大量群文件、
图片、视频或长期保留 QQ 数据时，应按实际数据增长预留更多空间。

仓库提供的 Compose 已包含 [SnowLuma](https://github.com/SnowLuma/SnowLuma)；使用其他
OneBot 11 实现时，需要自行保证反向 WebSocket 和媒体地址的网络可达性。

> [!NOTE]
> 本项目仅在 SnowLuma 上进行过充分测试。NapCat 等其他 OneBot 11 实现需要使用者自行
> 测试兼容性；本项目不保证连接、消息、媒体、回复或撤回等行为符合预期。

## Docker 部署

Docker Compose 是推荐且唯一面向生产使用的部署方式。`docker-compose.yaml` 包含
Q2TG-Python 与 SnowLuma，运行前需要准备 Docker Engine、Compose 插件和 Telegram Bot。

### 准备配置

```bash
mkdir q2tg-python
cd q2tg-python
curl --fail --location --remote-name \
  https://raw.githubusercontent.com/Azusa-mikan/q2tg-python/main/docker-compose.yaml
```

在同一目录创建 `.env`，供 Compose 执行变量插值：

```dotenv
Q2TG_ONEBOT_TOKEN=replace-with-a-random-token
Q2TG_DATABASE_URL=sqlite:////app/data/q2tg.db
Q2TG_TGBOT_TOKEN=replace-with-telegram-bot-token
Q2TG_TGBOT_ADMIN=123456789
Q2TG_ONEBOT_PROXY_URL=
Q2TG_TGBOT_PROXY_URL=

SNOWLUMA_HOSTNAME=snowluma-device
SHOWLUMA_MAC_ADDRESS=02:42:ac:11:00:02
SNOWLUMA_VNC_PASSWD=replace-with-a-strong-vnc-password
SNOWLUMA_UID=1000
SNOWLUMA_GID=1000
```

| 配置项 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `Q2TG_ONEBOT_TOKEN` | 是 | 无 | OneBot 反向 WebSocket 的 Bearer token |
| `Q2TG_DATABASE_URL` | 否 | `sqlite:////app/data/q2tg.db` | 标准数据库 URL，支持 SQLite、MySQL/MariaDB 和 PostgreSQL |
| `Q2TG_TGBOT_TOKEN` | 是 | 无 | Telegram Bot token |
| `Q2TG_TGBOT_ADMIN` | 是 | 无 | 有权执行 `/bind`、`/unbind` 的 Telegram 用户 ID |
| `Q2TG_ONEBOT_PROXY_URL` | 否 | 空 | OneBot 媒体下载代理 |
| `Q2TG_TGBOT_PROXY_URL` | 否 | 空 | Telegram Bot API 和文件下载代理 |
| `SNOWLUMA_HOSTNAME` | 是 | 无 | 固定的 SnowLuma 容器主机名 |
| `SHOWLUMA_MAC_ADDRESS` | 是 | 无 | 固定的 SnowLuma 容器 MAC 地址 |
| `SNOWLUMA_VNC_PASSWD` | 否 | `vncpasswd` | VNC 密码，部署时应覆盖默认值 |
| `SNOWLUMA_UID` | 否 | `1000` | SnowLuma 数据目录的 UID |
| `SNOWLUMA_GID` | 否 | `1000` | SnowLuma 数据目录的 GID |

`Q2TG_ONEBOT_TOKEN` 可通过 `openssl rand -hex 32` 生成；Telegram Bot token 从
[@BotFather](https://t.me/BotFather) 获取。代理支持 `http://`、`https://`、
`socks5://` 和 `socks5h://`，留空表示直连。程序不读取 `HTTP_PROXY`、
`HTTPS_PROXY` 或 `ALL_PROXY`。

`Q2TG_DATABASE_URL` 必须使用不带驱动名的标准 scheme：

```text
sqlite:////app/data/q2tg.db
mysql://user:password@database-host:3306/q2tg
postgresql://user:password@database-host:5432/q2tg
```

不要配置 `sqlite+aiosqlite://`、`mysql+asyncmy://` 或 `postgresql+asyncpg://`；程序会在
内部自动选择异步驱动。仓库提供的 Compose 不包含 MySQL 或 PostgreSQL 服务，使用外部
数据库时需要自行提供数据库实例，并确保 `q2tg-python` 容器可以访问对应主机和端口。

`SNOWLUMA_HOSTNAME` 推荐使用 `DESKTOP-{随机 5-6 位大写字母或数字}` 的格式，例如
`DESKTOP-A7K2QF`。可生成一个 6 位后缀：

```bash
openssl rand -hex 3 | tr '[:lower:]' '[:upper:]'
```

> [!CAUTION]
> `.env` 包含真实凭据，不应提交到 Git。

> [!IMPORTANT]
> 首次登录后请保持 `SNOWLUMA_HOSTNAME`、`SHOWLUMA_MAC_ADDRESS` 以及 `snowluma-*`
> bind mount 不变。修改设备标识或丢失登录数据可能触发平台安全验证，并导致会话失效。

### 启动

```bash
docker compose pull
docker compose up -d
docker compose ps
```

日志：

```bash
docker compose logs -f q2tg-python
docker compose logs -f snowluma
```

Compose 使用 bind mount 持久化以下目录：

- `./q2tg-data`：默认 SQLite 数据库；使用外部数据库时仍可保留该挂载
- `./snowluma-data`：SnowLuma 数据
- `./snowluma-qq-config`：账号配置
- `./snowluma-qq-data`：账号数据

SnowLuma 暴露的端口：

| 端口 | 用途 |
| --- | --- |
| `5900` | VNC |
| `6081` | Web VNC |
| `5099` | SnowLuma WebUI |
| `3000`、`3001` | SnowLuma 服务端口 |

### 接入 OneBot

通过 VNC 完成账号登录，然后在 SnowLuma 中添加 OneBot 11 反向 WebSocket：

```text
URL:   ws://q2tg-python:8000/ws
Token: Q2TG_ONEBOT_TOKEN 的值
```

两个服务加入同一个 Compose 网络，`q2tg-python` 是容器内 DNS 服务名。Compose 已将
`Q2TG_ONEBOT_MEDIA_URL` 固定为 `http://q2tg-python:8000`，无需在 `.env` 中声明。

OneBot WebSocket 通过鉴权后，Telegram Bot 和消息转发消费者才会开始运行。此时管理员可
在私聊中向 Bot 发送 `/start` 查看桥接步骤，然后将 Bot 加入目标群聊并执行绑定命令。

### 运维

更新：

```bash
docker compose pull
docker compose up -d
```

停止：

```bash
docker compose down
```

> [!WARNING]
> 不要删除上述持久化目录。`docker compose down` 不会删除 bind mount 中的数据。

## OneBot 配置

在 OneBot 实现中添加反向 WebSocket 连接，并使用与 `Q2TG_ONEBOT_TOKEN` 相同的 token。
连接地址和具体配置格式取决于所使用的 OneBot 实现。

当前兼容性基线为 SnowLuma。NapCat 等其他 OneBot 11 实现未经过充分测试，即使能够建立
连接，也可能因消息段解析、媒体下载或 action 行为差异而出现非预期结果，相关兼容性需
自行验证。

需要确保：

- OneBot 实现可以访问 Q2TG-Python 的监听地址
- 鉴权 token 与项目配置一致
- `Q2TG_ONEBOT_MEDIA_URL` 是 OneBot 侧可访问的 HTTP(S) 地址
- 防火墙或反向代理允许对应端口及 WebSocket 连接

### `Q2TG_ONEBOT_MEDIA_URL` 与 OneBot 11 实现

`Q2TG_ONEBOT_MEDIA_URL` 不是 OneBot API 地址，也不是反向 WebSocket 地址。它是
Q2TG-Python 向 OneBot 11 实现提供 Telegram 临时媒体的 HTTP(S) 基础地址。

Telegram 消息包含图片、视频或文件时，Q2TG-Python 会生成类似下面的 OneBot 11 消息段：

```json
{
  "type": "image",
  "data": {
    "file": "http://q2tg-python:8000/media/random-media-id"
  }
}
```

OneBot 11 实现收到消息段后，需要主动访问 `file` URL 下载媒体，再将其发送到 OneBot
群。因此该地址必须从 **OneBot 11 实现所在的网络环境** 中可访问，而不是只要求浏览器
或 Q2TG-Python 自身可以访问。

常见部署方式：

| 部署关系 | `Q2TG_ONEBOT_MEDIA_URL` 示例 |
| --- | --- |
| 使用仓库提供的 Compose，Q2TG-Python 与 SnowLuma 位于同一网络 | `http://q2tg-python:8000` |
| 两者直接运行在同一台宿主机 | `http://127.0.0.1:8000`，前提是 OneBot 实现不在独立容器中 |
| OneBot 11 实现在另一个 Docker 容器中 | Q2TG-Python 的容器服务名和容器端口，且两个容器必须共用网络 |
| OneBot 11 实现在另一台设备上 | Q2TG-Python 宿主机的局域网 IP、域名或反向代理 HTTPS 地址 |

> [!WARNING]
> 在容器中，`127.0.0.1` 指向容器自身。如果 SnowLuma 与 Q2TG-Python 分属两个容器，
> 将该配置写成 `http://127.0.0.1:8000` 会导致 SnowLuma 无法下载媒体。

媒体 URL 使用不可预测的随机 ID，并且仅临时有效。OneBot 11 实现应在收到发送请求后及时
下载，不应长期保存或延迟解析 URL。如果文本可以转发但图片、视频或文件失败，应优先从
OneBot 11 容器或设备中检查该 URL 的 DNS、端口、防火墙和反向代理可达性。

## Telegram 命令

`/start` 会根据私聊或群聊返回不同内容，`/status` 可在两种聊天中使用；其余桥接和管理
命令应在 Telegram 群聊中使用：

| 命令 | 权限 | 说明 |
| --- | --- | --- |
| `/start` | 所有人 | 群聊中显示运行状态；私聊中向配置的管理员显示桥接步骤，其他用户显示管理员联系方式 |
| `/status` | 所有人 | 在私聊或群聊中显示进程 RSS、各消息队列长度及最近 30 次成功媒体转换的平均耗时 |
| `/bind <OneBot 群号>` | 配置的 Bot 管理员 | 绑定当前 Telegram 群与 OneBot 群 |
| `/unbind` | 配置的 Bot 管理员 | 解除当前群的绑定 |
| `/forward [on\|off]` | Telegram 群管理员 | 查询或设置 Telegram 到 OneBot 的转发状态 |
| `/id_show [on\|off]` | Telegram 群管理员 | 查询或设置 OneBot 用户及 @ 对象的数字 ID 显示 |
| `/undo` | 按实现权限检查 | 回复目标消息后撤回两侧对应消息 |

绑定示例：

```text
/bind 123456789
```

每个 OneBot 群和 Telegram 群只能参与一个绑定关系。

## 数据与限制

- 默认 SQLite 数据库位于 `data/q2tg.db`；Docker Compose 中对应 `/app/data/q2tg.db`
- 可通过 `Q2TG_DATABASE_URL` 使用 MySQL/MariaDB 或 PostgreSQL，URL 中必须包含数据库名
- Docker 使用默认 SQLite 时应持久化 `/app/data`；外部数据库应按其自身方案备份和持久化
- 消息映射默认保留 30 天
- 单个下载媒体的大小上限为 20 MB
- 视频、语音和贴纸转换依赖 ffmpeg、ffprobe、Pillow、pilk 与
  [lottie-converter](https://github.com/ed-asriyan/lottie-converter)
- TGS 输入仍受 Telegram Bot API 下载上限限制；转换后的 GIF 不设置额外大小上限
- 使用默认 SQLite 时，删除容器前未持久化 `/app/data` 会丢失群绑定和消息映射

### 数据库迁移

应用每次启动都会通过 Alembic 将数据库自动升级到当前 schema。升级前应备份数据库；不要
让多个 Q2TG-Python 实例同时对同一个数据库执行首次启动或升级。

历史 SQLite 数据库会被自动识别并接入 Alembic，包括旧的三表 schema 和当前四表 schema。
对于 MySQL/MariaDB 或 PostgreSQL，程序只会自动初始化空数据库，或升级已经包含
`alembic_version` 标记的数据库。非空且没有该标记的外部数据库会被拒绝接管，以免误改
其他应用的表。

## 常见问题

### Bot 无法连接

检查 OneBot 反向 WebSocket 地址、端口和 token 是否一致，并确认网络允许 WebSocket
连接。Docker 部署还需要确认端口已发布。

### Telegram 消息无法转发

确认当前 Telegram 群已经通过 `/bind` 绑定，并使用 `/forward` 检查转发开关。同时检查
Telegram Bot 是否有读取和发送群消息所需的权限。

### 图片或视频转发失败

检查媒体是否超过 20 MB、`Q2TG_ONEBOT_MEDIA_URL` 是否可访问，以及系统中的 ffmpeg 和
ffprobe 是否可用。

### Docker 容器无法写入 SQLite 数据库

命名 volume 通常不需要额外处理。使用宿主机目录时，请确保挂载目录允许 UID/GID
`10001` 写入。

## 安全提示

- 不要公开 `.env`、Bot token 或 OneBot token
- 建议仅向可信网络开放服务端口
- 使用公网地址时建议通过 HTTPS 和可信反向代理提供服务
- 定期备份 SQLite 的 `data/q2tg.db`，或按外部数据库的备份方案保护数据
- token 泄露后应立即撤销并重新生成

## 本地开发与测试

> [!IMPORTANT]
> 不推荐使用本地方式部署。本节仅供开发、调试和测试使用，正式运行请使用 Docker。

复制完整的开发配置模板并按需修改：

```bash
cp .env.example .env
```

```dotenv
Q2TG_APP_PORT=8000
# Q2TG_DATABASE_URL=sqlite:////absolute/path/to/q2tg.db
Q2TG_ONEBOT_TOKEN=replace-with-onebot-token
Q2TG_ONEBOT_MEDIA_URL=http://127.0.0.1:8000
Q2TG_ONEBOT_PROXY_URL=
Q2TG_TGBOT_TOKEN=replace-with-telegram-bot-token
Q2TG_TGBOT_ADMIN=123456789
Q2TG_TGBOT_PROXY_URL=
```

`Q2TG_APP_PORT` 是本地 HTTP 服务和 OneBot 反向 WebSocket 的监听端口，默认值为
`8000`。`Q2TG_ONEBOT_MEDIA_URL` 必须是 OneBot 侧能够访问的本服务 HTTP(S) 地址。
进程中的同名环境变量优先于 `.env`。数据库 URL 使用标准 scheme，程序内部会分别选择
`aiosqlite`、`asyncmy` 或 `asyncpg`：

```text
sqlite:////absolute/path/to/q2tg.db
mysql://user:password@host:3306/q2tg
postgresql://user:password@host:5432/q2tg
```

本地运行时，TGS 动态贴纸转换要求已安装 Docker，Docker daemon 正在运行，并且当前用户
有权执行 `docker run`。项目会自动调用固定版本的 `lottie-converter` 镜像，不使用
`sudo`。项目自身的 Docker 镜像内置转换工具，不会在容器中再次启动 Docker。

使用 `docker-compose-debug.yaml` 启动本地构建的镜像时，容器以 UID `10001` 读写项目的
`data` 目录。本地进程与 debug 容器需要轮流使用同一个数据目录时，先停止两边的 q2tg
实例，再恢复本地用户所有权，并通过 ACL 授予容器用户对现有及以后新建内容的读写权限：

```bash
sudo chown -R "$(id -u):$(id -g)" ./data
sudo chmod -R u+rwX ./data
sudo setfacl -R -m u:10001:rwX ./data
sudo setfacl -m d:u:10001:rwX ./data
```

不要同时运行本地和容器中的 q2tg 实例。SQLite WAL 模式要求进程不仅能够修改
`q2tg.db`，还能够在 `data` 目录创建和修改 `q2tg.db-wal`、`q2tg.db-shm`。权限修复后
必须重启应用，使其重新打开数据库连接。可使用以下命令检查数据库及辅助文件权限：

```bash
stat -c '%U:%G %A %n' ./data ./data/q2tg.db*
getfacl ./data ./data/q2tg.db
```

如果只在本地运行，不需要设置 UID `10001` 的 ACL。如果只在 Docker 中运行，也不需要
恢复为本地用户所有。系统没有 `setfacl` 时，Debian/Ubuntu 可通过
`sudo apt install acl` 安装。

安装锁定依赖和开发工具（包括 Pyright 与 Ruff）：

```bash
uv sync --locked
```

启动服务：

```bash
uv run --locked python main.py
```

服务默认监听 `0.0.0.0:8000`。可通过以下接口检查运行状态：

```text
GET /healthz
```

正常响应：

```json
{"status":"ok"}
```

运行单元测试：

```bash
uv run --locked python -W error::ResourceWarning -m unittest discover -s tests
```

`tests/test_database_integration.py` 默认跳过。需要验证 MySQL/MariaDB 或 PostgreSQL 时，
提供一个允许测试创建和删除 Q2TG 业务表的独立空数据库：

```bash
Q2TG_TEST_DATABASE_URL=mysql://user:password@127.0.0.1:3306/q2tg_test \
  uv run --locked python -m unittest tests.test_database_integration

Q2TG_TEST_DATABASE_URL=postgresql://user:password@127.0.0.1:5432/q2tg_test \
  uv run --locked python -m unittest tests.test_database_integration
```

> [!WARNING]
> 集成测试启动前会删除该数据库中的 Q2TG 业务表和 `alembic_version`。不要指向生产数据库
> 或包含其他重要数据的数据库。

运行静态检查：

```bash
uv run --locked pyright main.py src tests
uv run --locked ruff check .
uv run --locked python -m compileall -q main.py src tests
```

## 许可证

本项目基于 [GNU General Public License v3.0 or later](LICENSE) 开源。
