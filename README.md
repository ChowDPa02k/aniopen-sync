# ANiOpen Sync

[![CI](https://github.com/ChowDPa02k/aniopen-sync/actions/workflows/ci.yml/badge.svg)](https://github.com/ChowDPa02k/aniopen-sync/actions/workflows/ci.yml)

ANiOpen Sync 定时读取 ANi RSS，把新番剧集提交给 aria2 或 qBittorrent，并在网页中提供配置、手动同步和下载进度。解析器移植自 [Kubespider](https://github.com/opennaslab/kubespider)，保留其季集识别规则。

<img width="1920" height="1080" alt="image" src="https://github.com/user-attachments/assets/13249991-dd21-47bf-92f2-0fdd5493b23f" />

## 功能

- 支持 ANi 的直链 RSS 与 Torrent RSS。直链交给 aria2，Torrent 可使用 aria2 或 qBittorrent。
- 自动处理季号映射、集数偏移、关键词黑名单和 qBittorrent 分类、标签与子分类。
- 在线季集映射实时读取 [ani-tmdb-mapper](https://github.com/ChowDPa02k/ani-tmdb-mapper)，支持手动覆盖和离线缓存。
- 支持秒级 cron、固定间隔和单次执行三种调度方式，可设置时区。
- Web 界面包含**配置中心**、**同步**和**同步记录**三个页面，配置可用表单或 YAML 编辑，保存后立即生效。
- 通过下载器 API 只读跟踪任务进度，下载出错或跟踪中断后可继续观察。
- 可选的 aria2 HTTP 自动重试：失败后按退避间隔重建任务并继续跟踪，默认关闭。
- 去重记录、配置和历史持久化，自动备份，提供健康检查接口。

## 快速开始

需要 Docker Engine（或 Docker Desktop）与 Compose v2。

```sh
git clone https://github.com/ChowDPa02k/aniopen-sync.git
cd aniopen-sync
docker compose up -d
```

首次启动会构建前后端镜像并生成访问令牌。打开 <http://127.0.0.1:8090>，输入日志中的令牌即可登录。忘记令牌时：

```sh
docker compose exec aniopen-sync aniopen-sync token
```

登录后进入「配置中心」，填写下载器地址和凭据，确认时区与调度规则，启用订阅并保存。订阅默认关闭；启用后，下一次同步会处理当前 RSS 中所有尚未见过的条目。

数据保存在 `aniopen-data` 数据卷中。`docker compose down` 停止并删除容器，数据仍然保留。

## 配置

主要配置项：

| 配置 | 说明 |
| --- | --- |
| `source.api_type` | `http`（直链）或 `torrent` |
| `source.rss_link` / `rss_link_torrent` | 两种订阅源对应的 RSS 地址 |
| `source.downloader` | 使用哪个下载器，对应 `downloaders` 中的名称 |
| `source.save_path` | 下载器内的相对保存路径，例如 `TV/ANi` |
| `source.blacklist` | 按标题关键词跳过条目 |
| `downloaders.*.endpoint` | 下载器连接地址 |
| `downloaders.*.download_base_path` | 下载器看到的根目录，例如 `/downloads` |
| `schedule` | 调度方式、时区、cron / interval / date 参数 |

默认调度为 `Asia/Shanghai` 时区每小时的第 6 分钟和第 36 分钟（30 秒）。完整字段和默认值见 [config/example.yaml](config/example.yaml)。

## 下载器连接

容器内的 `127.0.0.1` 指向服务自身，请按下载器的实际位置填写地址：

| 下载器位置 | 地址示例 |
| --- | --- |
| Docker 宿主机 | `http://host.docker.internal:6800/jsonrpc` |
| 同一 Docker 网络 | `http://aria2:6800/jsonrpc`、`http://qbittorrent:8080` |
| 其它机器或反向代理 | `http://192.0.2.10:6800/jsonrpc`、`https://downloads.example/qbit` |

Linux 上使用 `host.docker.internal` 时，需要在 Compose 中为服务添加：

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

如果下载器已在某个 Docker 网络中运行，可以直接加入该网络：

```sh
ANIOPEN_DOWNLOADER_NETWORK=your-network \
  docker compose -f compose.yaml -f compose.network.yaml up -d
```

aria2 填完整的 JSON-RPC 地址；qBittorrent 填 WebUI 根地址。`download_base_path` 是下载器看到的路径，媒体文件由下载器直接写入对应目录。

## 访问与安全

服务默认监听所有网卡的 8090 端口。除健康检查和登录页外，所有 API 都需要访问令牌，令牌保存在浏览器会话中；配置中的密码和密钥默认脱敏显示。

可以通过 `.env` 或环境变量调整部署：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `ANIOPEN_PORT` | `8090` | 宿主机端口 |
| `ANIOPEN_BIND_ADDRESS` | `0.0.0.0` | 监听地址，可设为 `127.0.0.1` 限制本机访问 |
| `ANIOPEN_API_TOKEN` | 自动生成 | 固定访问令牌 |
| `ANIOPEN_ALLOWED_HOSTS` | 空 | 允许的 Host，逗号分隔；使用自定义域名时必填 |
| `ANIOPEN_UID` / `ANIOPEN_GID` | `10001` | 容器运行用户 |
| `TZ` | `Asia/Shanghai` | 容器时区 |

项目面向可信网络中的单管理员使用。通过公网访问时，建议在反向代理上启用 HTTPS，并妥善保管令牌。

## 运行维护

- **健康检查**：`/api/health` 返回进程状态，`/api/health/ready` 检查 RSS 与下载器连接。Docker 每 30 秒调用就绪接口，依赖故障时容器显示为 `unhealthy`，恢复后自动转回 `healthy`。
- **备份**：服务每 6 小时生成一次在线备份，默认保留 7 天，存放在 `/data/backups`。手动备份：

  ```sh
  docker compose exec aniopen-sync \
    aniopen-sync backup --output /data/backups/manual.tar.gz
  docker compose cp aniopen-sync:/data/backups/manual.tar.gz ./aniopen-backup.tar.gz
  ```

  备份包含下载器凭据，请保存到安全位置，并定期复制到其它存储或使用绑定挂载。
- **升级**：

  ```sh
  git pull
  docker compose up -d --build
  ```

  数据库和配置会自动迁移，升级前的状态会保留备份。

- **日志**：Compose 默认轮转日志（单文件 10 MB，保留 3 份），可用 `docker compose logs -f aniopen-sync` 查看。

部署、恢复和故障排查的更多细节见 [docs/operations.md](docs/operations.md)。

## 开发

需要 Python 3.11+、Node.js 22.12+ 和 [uv](https://docs.astral.sh/uv/)。

```sh
uv sync --locked --extra dev
npm ci --prefix web
npm run build --prefix web
uv run aniopen-sync
```

本地默认访问 <http://127.0.0.1:8090>，数据目录为 `./data`，首次启动的令牌会打印在日志中。`ANIOPEN_DATA_DIR` 和 `ANIOPEN_WEB_DIR` 可以覆盖数据与前端目录。

运行检查与测试：

```sh
uv run ruff check aniopen_sync tests scripts
uv run pytest -q
npm test --prefix web
```

开发约定和测试说明见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 相关文档

- [部署、升级与恢复](docs/operations.md)
- [完整配置示例](config/example.yaml)
- [变更记录](CHANGELOG.md)
- [贡献指南](CONTRIBUTING.md)

## 许可与致谢

项目使用 [Apache-2.0](LICENSE) 许可。

ANi RSS 解析逻辑来源于 [opennaslab/kubespider](https://github.com/opennaslab/kubespider)，上游版本与修改说明见 [NOTICE](NOTICE)；在线季集映射由 [ChowDPa02k/ani-tmdb-mapper](https://github.com/ChowDPa02k/ani-tmdb-mapper) 提供。
