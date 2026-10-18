# 部署、升级与恢复

## 日常使用

```sh
docker compose up -d
docker compose logs -f aniopen-sync
```

首次启动会构建前后端镜像并生成访问令牌。配置、历史和令牌保存在 `aniopen-data` 数据卷中，停止或重建容器不会影响这些数据；除非需要清空数据，否则不要执行 `docker compose down -v`。

端口、监听地址、访问令牌、Host 白名单和运行用户通过 `.env` 或环境变量设置，修改后需重启容器。WebUI 中的业务配置保存后立即生效，无需重启。环境变量列表见 [README](../README.md#访问与安全)。

容器时区（`TZ`）与调度时区（`schedule.timezone`）相互独立，同步计划以配置中的时区为准。

镜像构建需要访问 npm、PyPI 和 GitHub Releases。受限网络可以配置 Docker 构建代理。

## 健康检查

| 接口 | 用途 | 正常 | 异常 |
| --- | --- | --- | --- |
| `/api/health` | 进程存活、登录页启动探测 | HTTP 200，`status: ok` | 无法连接应用 |
| `/api/health/ready` | RSS 与下载器可用性 | HTTP 200，`status: ok` | HTTP 503，`status: warning` |

启用订阅后，服务每 60 秒读取一次当前 RSS，并通过所选下载器的只读接口检查连接和认证。检查不会提交下载任务。未启用订阅时跳过外部依赖检查。

Docker 每 30 秒调用就绪接口，连续 3 次失败后容器标记为 `unhealthy`，恢复后自动回到 `healthy`。告警期间 WebUI 和配置接口仍可使用。

查看健康状态和失败原因：

```sh
docker inspect --format '{{json .State.Health}}' "$(docker compose ps -q aniopen-sync)"
docker compose exec aniopen-sync python -m aniopen_sync.healthcheck
```

## 数据权限

容器默认以 UID/GID `10001` 运行，`/data` 需要该用户可写。使用 NAS 的 bind mount 时，可以让容器用户匹配宿主目录的所有者：

```sh
ANIOPEN_UID=1001 ANIOPEN_GID=1001 docker compose up -d
```

默认的 `compose.yaml` 已处理好 `/run` 等运行时目录的权限和只读根文件系统。

## 升级

1. 按下方「备份」一节创建手动备份，并复制到容器外。
2. 更新代码并重建容器：

   ```sh
   git pull
   docker compose up -d --build
   ```

3. 检查 `docker compose ps`、容器日志和 WebUI 的同步记录。

数据库和配置会在升级时自动迁移，迁移前保留备份。如果数据来自更新版本的程序，服务会拒绝启动，避免旧版本写坏数据。从 0.4.0 回退到旧版本时，请同时恢复升级前的数据库备份。

项目不会自动更新代码或基础镜像，升级由管理员主动执行。

## 备份

服务启动时及之后每 6 小时生成一次当天备份，默认保留 7 天，存放于 `/data/backups/automatic-YYYY-MM-DD.tar.gz`。归档包含数据库、配置、访问令牌和映射缓存。

手动备份并复制到宿主机：

```sh
docker compose exec aniopen-sync \
  aniopen-sync backup --output /data/backups/manual.tar.gz
docker compose cp aniopen-sync:/data/backups/manual.tar.gz ./aniopen-backup.tar.gz
chmod 600 ./aniopen-backup.tar.gz
```

备份包含下载器凭据，应保存在受限目录。自动备份与数据在同一个卷中，能应对误升级和配置错误，但无法应对磁盘或卷丢失，建议定期复制到其它存储并验证可恢复。

## 恢复与回滚

停止服务，将可信备份解压到空目录，再把它作为 `/data` 挂载。不要把备份直接解压到正在使用的数据目录。

创建 `compose.restore.yaml`：

```yaml
services:
  aniopen-sync:
    volumes:
      - ./restored-data:/data
```

然后执行：

```sh
docker compose stop
mkdir -m 700 restored-data
tar -xzf aniopen-backup.tar.gz -C restored-data
sudo chown -R 10001:10001 restored-data
docker compose -f compose.yaml -f compose.restore.yaml up -d
```

之后使用同一组 Compose 文件管理服务。原数据卷会保留，确认恢复成功后再自行清理。使用自定义 `ANIOPEN_UID` / `ANIOPEN_GID` 时，`chown` 也改为对应值；通过环境变量固定令牌时，需要设置相同的值。

恢复使用较旧的备份后，备份时间之后提交的任务不在数据库中，重新同步可能产生重复下载。恢复前请核对下载器的现有任务与备份时间。数据库迁移备份只能回滚数据库，完整回滚请使用升级前的完整归档。

## aria2 HTTP 自动重试

默认关闭，只适用于本项目提交给 aria2 的 HTTP 下载任务。Torrent、磁力链接和 qBittorrent 任务仍由下载器处理。

在「配置中心 → 长期运行 → aria2 HTTP 自动重试」开启，或在完整 YAML 中配置：

```yaml
download_retry:
  enabled: true   # 默认值为 false
  delay_seconds: 600
  max_delay_seconds: 3600
  max_attempts: 10
  max_per_poll: 10
  error_codes: [2, 5, 6, 7, 19, 22, 29]
```

任务失败且错误码在列表中时，服务会沿用原地址、保存目录和文件名重新提交，并继续跟踪新任务。首次等待 `delay_seconds`，连续失败时按指数退避，最长不超过 `max_delay_seconds`。`max_attempts` 限制每条 RSS 的重试次数，设为 `0` 表示不限；`max_per_poll` 限制每轮最多提交的任务数。

默认错误码覆盖网络故障和 HTTP 响应错误（包括 403），磁盘、权限和资源不存在等错误不会自动重试。错误码含义见 [aria2 官方手册](https://aria2.github.io/manual/en/html/aria2c.html#exit-status)。

重试计划和进度会持久化，服务重启后继续执行。提交结果无法确认时，任务会标记为待确认并停止重发。关闭开关会取消尚未发送的重试。任务详情中可以看到重试状态、次数、下次计划时间和失败原因。

如果下载器端已经配置了失败回调脚本（例如 NAS 上的 `error.sh` 或 `quota-retry-watch.sh`），请只保留一套重试机制，以免重复下载。

## 故障排查

| 现象 | 检查 |
| --- | --- |
| 服务健康但没有下载 | 订阅是否启用、调度是否启用、下载器是否启用，以及同步记录中的执行结果 |
| RSS 解析失败 | RSS 地址是否可访问；站点离线时可能返回 HTML 页面，可尝试 ANi 的 Torrent 源 |
| 下载器连接失败 | 容器内的地址是否可达，端口、RPC 路径、凭据和证书是否正确 |
| qBittorrent 登录 401 | WebUI 端口、反向代理设置和 CSRF 配置是否与访问地址一致 |
| 自动重试没有触发 | 开关是否开启，错误码是否在列表中，任务是否为 aria2 HTTP 单文件；任务详情会显示当前状态和原因 |
| 提交结果不确定 | 在记录详情中核对下载器，确认任务确实未入队后再人工确认 |
| 自定义域名返回 400 | 设置 `ANIOPEN_ALLOWED_HOSTS`，并确认反向代理保留了原始 Host |
| 无法写入或备份失败 | 数据卷权限、剩余空间，以及 `ANIOPEN_UID` / `ANIOPEN_GID` 是否匹配 |

下载任务的暂停和删除由下载器负责。
