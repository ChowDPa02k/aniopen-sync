# ANiOpen Sync

从 ANi RSS 自动创建下载任务，通过 WebUI 编辑配置、手动同步和观察进度。解析器来自 Kubespider，保留季号映射、集数偏移、黑名单和 qBittorrent 子分类。使用 FastAPI、APScheduler 3、React、Ant Design ProComponents 与 SQLite。

项目专用于 ANi，界面与文档以中文维护。下载器由你提供；本项目提交任务后只读观察，不控制引擎中的暂停、删除、重试或分类，不抓取海报，不依赖 AI 服务。

## 启动

在项目目录执行（需要 Docker Engine / Desktop 与 Compose v2）：

```sh
docker compose up
```

首次自动构建完整 WebUI、创建持久化数据卷和访问令牌，无须预先创建 `.env`。打开 `http://服务器IP:8090`（本机可用 <http://127.0.0.1:8090>），输入启动日志中的令牌。后台运行可使用 `docker compose up -d`；再次查看令牌：

```sh
docker compose exec aniopen-sync aniopen-sync token
```

进入“配置中心”，设置下载器地址、凭据、下载器内保存根目录，检查时区、订阅方式与规则，再启用订阅并保存。首次启动订阅关闭，启用后下一次同步会处理当前 RSS 中所有尚未见过的条目。默认 HTTP 方式要求 aria2；Torrent 方式可使用 aria2 或 qBittorrent。HTTP RSS 若返回站点离线页面，会显示明确错误；可按源的可用情况切换为 Torrent。

“同步”页保留下载统计卡片，展示 RSS 下载任务并提供手动同步；表格自动填充卡片下方的剩余高度，筛选、表头和分页保留在可见区域，任务在表格内部滚动。“同步记录”页单独查看计划状态与最近同步执行结果。

默认计划是 **Asia/Shanghai 每小时 06 分 30 秒、36 分 30 秒**，与容器启动时刻无关。支持秒级 cron、interval、date、时区、起止时间和抖动。预设按钮保留你选择的时区。

```yaml
schedule:
  enabled: true
  timezone: Asia/Shanghai
  trigger: cron
  cron: {minute: '6,36', second: '30'}
```

业务配置均可在表单和完整 YAML 中修改，保存后立即应用；已经执行的同步沿用开始时的配置。完整默认值见 [config/example.yaml](config/example.yaml)。未知字段、非法路径、无效定时规则会拒绝保存。YAML 使用十进制数字与 true/false 布尔值，不将 `1:30` 当作数字或 `on` 当作布尔值。

## 下载器寻址

容器中的 `127.0.0.1` 指向本服务自身。按下载器的位置填写连接地址：

| 下载器位置 | 地址示例 | 部署方法 |
| --- | --- | --- |
| 本机源码开发 | `http://127.0.0.1:6800/jsonrpc` | Python 与下载器在同一主机网络 |
| 同一 Docker 网络 | `http://aria2:6800/jsonrpc` 或 `http://qbittorrent:8080` | 使用下面的外部网络覆盖文件 |
| Docker 宿主机 | `http://host.docker.internal:6800/jsonrpc` | Desktop 内置此名称；Linux 需添加 `extra_hosts: ["host.docker.internal:host-gateway"]`，且下载器须监听可达地址 |
| 其它机器 / 反向代理 | `http://192.0.2.10:6800/jsonrpc` 或 `https://downloads.example/qbit` | 填实际可达地址、凭据和证书设置；支持反代路径前缀 |

连接已有 Docker 网络，无须启动或替换下载器：

```sh
ANIOPEN_DOWNLOADER_NETWORK=your-network docker compose -f compose.yaml -f compose.network.yaml up -d
```

qBittorrent 应填写 WebUI 根地址，aria2 填写完整 RPC 地址。qB WebUI 的端口 / 反代设置须与实际访问地址兼容；若登录返回 HTTP 401，请先核查其 WebUI 端口、代理和 CSRF 设置。优先使用同网络服务名与内部端口，无须关闭安全校验。

**媒体目录无需挂载给本服务。** `download_base_path` 是下载器看到的路径，例如 `/downloads` 加相对目录 `TV/ANi`。qB 自动管理开启时，实际目录由其分类配置决定。纯 v2 Torrent / 磁力需要 qB 的 libtorrent 2.x 构建；aria2 不支持纯 v2。版本实测范围见[协议验证](docs/production-compatibility.md)。

## 长期运行

- s6-overlay 管理容器 PID 1、信号与子进程清理；主程序退出由 Docker 重启策略恢复。Compose 自动轮转日志，默认使用 UID/GID 10001、只读根文件系统与独立数据卷。数据、令牌、映射缓存和备份保存在 `/data`。
- 订阅启用后每 60 秒独立只读检查当前 RSS 和所选下载器，即使没有新下载任务也能发现连接故障。`/api/health/ready` 故障时返回 HTTP 503 和 `status: warning`，恢复后返回 200；Docker 健康检查将其映射为 `unhealthy` / `healthy`。详见[健康检查](docs/operations.md#健康检查)。
- 默认每 15 秒观察进度；错误、完成、跟踪中断采用独立低频间隔。单轮观察最多 1,000 条记录，aria2 扫描最多 5,000 条引擎结果；达到上限时提示并停止猜测关联；提交前基线不完整的新任务仍会发送，但只观察已知 GID 与原生后继。外部重建跟踪可关闭。
- 完成 / 移除的任务继续观察 24 小时。错误任务保持低频观察，外部修复后能恢复进度。aria2 回调重建 GID 的关联规则见[跟踪说明](docs/tracking.md)。
- 每 6 小时维护历史：完成 / 移除记录保留 180 天、最多 20,000 条；未解决故障不会因清理消失。轻量去重标识持久保留，清理历史不会重新下载。
- 每 6 小时生成当天的在线数据库与配置备份，默认保留 7 天。升级旧数据前另建迁移备份；配置和缓存原子写入。请定期将备份复制到独立存储，参见[升级与恢复](docs/operations.md)。
- 网络读取有大小上限、超时和有限重试。明确拒绝 / 发送前失败按退避时间等待后续同步；发送结果不确定时不会盲目重发，可在记录详情中先核查、再人工确认未入队。没有引擎任务控制按钮。
- 重启后默认立即同步一次已启用订阅，以补查 RSS 中仍存在的新条目；可关闭 `maintenance.sync_on_startup`。RSS 已滚出窗口的条目无法凭空补回。运行中的 misfire / coalesce 与停机恢复是不同机制。

在线季集映射继续实时读取 `main`，以获取首播后修正。网络失败时使用同源有效缓存，页面显示来源、抓取时间与摘要。用户映射优先于在线，再到内置；同层最长关键字优先，集数偏移只应用首个优先层中的一个规则；季号 0 表示特别篇，文件名使用 S00。

默认将 `resources.ani.rip` 替换为 `cloud.ani-download.workers.dev`，保留既有绕过原域名 Cloudflare 拦截的兼容行为。这与在线季集映射开关无关，可在 `source.url_rewrites` 修改，设 `{}` 可关闭；关闭后直连可能失败。详情同时展示脱敏后的原始地址与实际下载地址，替换规则变化不会把已见条目当作全新条目。

## 访问与安全

默认监听宿主机 8090，**所有配置和历史 API 始终要求令牌**；仅健康检查和静态登录页公开。令牌存于浏览器会话中，验证成功后才保存。密钥默认脱敏，单独点击“显示保存的密钥”才会读取明文。401 重新认证会保留未保存配置。

可用 `.env` 设置 `ANIOPEN_BIND_ADDRESS=127.0.0.1` 限制本机访问、`ANIOPEN_PORT` 改端口、`ANIOPEN_API_TOKEN` 固定令牌。自动生成的令牌随数据卷持久保留。自定义域名需设置 `ANIOPEN_ALLOWED_HOSTS=sync.example.com`；反代需保留浏览器 Host，跨主机使用 HTTPS。本项目面向可信网络的单管理员，不提供多租户能力。RSS / 映射是受信任输入，修改 URL 相当于授权服务访问对应上游。更多说明见 [SECURITY.md](SECURITY.md)。

## 迁移与开发

从 Kubespider 导入配置和去重标识见[迁移说明](docs/migration.md)。不会导入虚构的旧下载历史，也不会接管生产引擎控制权。

正式使用方式为 **Docker + Git checkout**；Python wheel 仅为后端组件，不是独立完整 WebUI 产品。源码开发需要 Python 3.11+、Node.js 22.12+ 和 uv：

```sh
uv sync --locked --extra dev
npm ci --prefix web
npm run build --prefix web
uv run aniopen-sync
```

默认本地端口 8090、数据目录 `./data`，首次令牌见日志。`ANIOPEN_DATA_DIR`、`ANIOPEN_WEB_DIR` 可覆盖目录。单进程、单 worker、单数据目录锁保护，不能通过增加 worker 扩容。API schema 可在认证后读取 `/api/openapi.json`。测试与贡献说明见 [CONTRIBUTING.md](CONTRIBUTING.md)，本次审查取舍见[修订决策](docs/audit-decisions-2026-10-18.md)。

## 许可与来源

Apache-2.0。解析逻辑来源于 [opennaslab/kubespider](https://github.com/opennaslab/kubespider)，上游 revision 与修改说明见 [NOTICE](NOTICE)。在线映射来自 [ChowDPa02k/ani-tmdb-mapper](https://github.com/ChowDPa02k/ani-tmdb-mapper)，运行时按需读取，不随本仓库分发其数据。外部源可用性由其维护者决定；长期运行仍需要偶尔检查磁盘、备份与安全更新。
