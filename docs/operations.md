# 部署、升级与恢复

## 日常使用

项目根目录执行 `docker compose up`，首次会构建前后端，后续复用已有镜像。后台启动用 `docker compose up -d`。首次输入令牌后在 WebUI 配好下载器并启用订阅。数据卷与容器分离，停止 / 重建容器不会清空配置和去重历史。不要执行 `docker compose down -v`，除非有意删除数据。

构建需要访问 npm、Python 包索引、镜像仓库和 GitHub Releases；锁文件固定应用依赖，Dockerfile 固定基础镜像 digest，s6-overlay 3.2.3.2 下载校验固定 SHA-256。支持 amd64 与 arm64。受限网络可以配置 Docker 构建代理，不需要改业务代码。

设置 `.env` 中的端口、Host 白名单、固定令牌等部署变量后用 `docker compose up -d` 应用；只在 WebUI 改业务配置无需重启。容器 TZ 不会隐式覆盖已保存的 `schedule.timezone`。cron 星期建议 `mon`–`sun`；APScheduler 3 的数字 0 为周一。interval 无起点时从配置加载时间计时，钟表对齐应使用 cron。

## 健康检查

Docker 没有原生 `warning` 健康状态。应用用 `warning` 表达依赖故障，容器通过 `unhealthy` 暴露给 Docker、Compose、Portainer 等外部工具；不会因依赖故障自行重启或操作下载任务。

| 接口 | 用途 | 正常 | 告警 |
| --- | --- | --- | --- |
| `/api/health` | 进程存活、登录页启动探测 | HTTP 200，`status: ok` | 无法连接应用进程 |
| `/api/health/ready` | RSS / 下载器可用性；无需令牌 | HTTP 200，`status: ok` | HTTP 503，`status: warning` |

启用订阅后，独立线程每 60 秒获取当前订阅方式的 RSS 并验证 XML，使用订阅配置的代理；同时通过所选下载器的只读 API 验证可达性和认证。aria2 使用 `getGlobalStat`，qBittorrent 登录后读取 `app/version`。检查不提交 RSS 条目，也不触发下载、重试或接管新 GID；单次请求超时最多 5 秒，无网络重试。尚未启用订阅时跳过外部依赖，保证首次 `docker compose up` 能正常启动。

检查结果缓存在内存中，访问健康接口不会发起网络请求。配置变化后立即唤醒探测，旧配置的结果不会覆盖新配置。等待首轮 / 新配置检查时为 `pending`，超过 180 秒未更新为 `stale`；两者均告警，避免探测线程卡住却持续显示健康。故障只返回 `rss_timeout`、`rss_unavailable`、`downloader_timeout`、`downloader_unavailable` 等代码，不返回地址、密钥或上游错误正文。

Docker 每 30 秒读取就绪接口，启动宽限 30 秒，连续 3 次失败后变为 `unhealthy`，随后一次成功即恢复 `healthy`。依赖恢复后通常一个探测周期内更新；WebUI 和配置接口在告警期间继续可用。查看原因：

```sh
docker inspect --format '{{json .State.Health}}' "$(docker compose ps -q aniopen-sync)"
docker compose exec aniopen-sync python -m aniopen_sync.healthcheck
```

s6 的 `/run` 必须是可写、可执行的 tmpfs，归运行用户所有，不能加 `noexec`；根文件系统仍只读。默认 Compose 已配置。NAS bind mount 可设置 `ANIOPEN_UID=1001`、`ANIOPEN_GID=1001`，并提前将 `/data` 的宿主目录归属设为对应 app 用户。自定义 Compose 的 `user` 时，也须同步 `/run` 的 `uid/gid`，权限保持 `0700`。主程序收到 SIGTERM 后等待当前同步 / 数据库收尾，Compose 保留 120 秒停止宽限。

## 升级

1. 先执行下面的手动备份并复制出容器，保留旧代码 revision 与镜像。
2. 更新源码，执行 `docker compose up -d --build`。
3. 检查 `docker compose ps`、日志和 WebUI 同步记录。数据库 / 配置的旧 schema 会先备份再自动升级；来自更高 schema 版本的数据会拒绝启动，避免旧程序写坏数据。

不自动更新应用和基础镜像，以免上游变更无人值守地破坏部署。依赖更新应走 CI 验证后再升级。下载站失效、磁盘耗尽、NAS 故障与安全补丁仍需管理员处理。

## 备份

服务启动及每 6 小时生成当天 `backups/automatic-YYYY-MM-DD.tar.gz`，同一天覆盖最新快照，默认保留 7 个日期。SQLite 使用在线 backup API，而非直接复制正在写入的主库文件；归档包含 `history.sqlite3`、`config.yaml`、`access-token`（使用环境令牌时可能不存在）和已存在的映射缓存。自动备份在同一数据卷，能防误升级，不能防整卷丢失。

手动备份与复制（`backup` 是 CLI 功能，不需要停止调度）：

```sh
docker compose exec aniopen-sync aniopen-sync backup --output /data/backups/manual.tar.gz
docker compose cp aniopen-sync:/data/backups/manual.tar.gz ./aniopen-backup.tar.gz
chmod 600 ./aniopen-backup.tar.gz
```

备份包含下载器凭据，必须存到受限目录；需要加密时在独立备份系统中完成。备份到其它存储并验证可恢复，比在同一卷保留更多副本更重要。

## 恢复 / 回滚

停止服务，使用与备份 schema 兼容的代码版本，将可信备份解压至空目录，再作为 `/data` 挂载。不要在运行中的数据目录覆盖 SQLite、WAL 或配置。示例 bind mount 覆盖文件：

```yaml
services:
  aniopen-sync:
    volumes:
      - ./restored-data:/data
```

```sh
docker compose stop
mkdir -m 700 restored-data
tar -xzf aniopen-backup.tar.gz -C restored-data
sudo chown -R 10001:10001 restored-data
docker compose -f compose.yaml -f compose.restore.yaml up -d
```

将上述 YAML 保存为 `compose.restore.yaml`。Compose 按容器挂载路径合并，`/data` 会替换原 named volume；以后命令始终使用同一组文件。原数据卷保留以便调查。若通过环境变量固定令牌，还需恢复同样的部署变量。

备份之后已经提交的任务不会在旧数据库中。恢复点越旧，重新同步重复下载的风险越高；恢复前先核查下载器和备份时间。SQLite 迁移备份只能回滚数据库，完整回滚优先使用升级前完整归档。

## 故障定位

| 现象 | 检查 |
| --- | --- |
| 健康但没有下载 | 首次默认关闭订阅；检查源、下载器启用状态、下一执行时间、同步运行结果 |
| RSS 解析失败 | 返回的可能是 HTML 离线页；修复地址 / 网络或选择可用的 ANi Torrent 源 |
| 下载器连接失败 | 容器 localhost、共享网络、端口、RPC 路径、凭据和证书；网络错误只保留最后快照 |
| qB 登录 401 | WebUI 配置端口与访问地址 / 反代设置；本服务发送匹配 endpoint 的 Referer |
| 提交结果不确定 | 详情“核对 RSS 提交结果”；先查引擎，确实未入队才人工确认，下次同步再发 |
| aria2 跟踪中断 | 旧 GID 已清除；新 GID 的 URI / hash 与目录必须唯一匹配；检查扫描上限与外部重建开关 |
| 自定义域名 400 | 设置 ANIOPEN_ALLOWED_HOSTS，检查反代 Host |
| 无法写入 / 备份 | 检查数据卷 UID 10001、剩余空间；保留日志，避免删除数据重新开始 |

没有自动修复下载引擎的任务控制接口；故障下载仍由下载器自身或你的回调脚本处理。
