# 贡献与验证

保持 ANi 专用、单实例和下载器只读观察边界。不添加引擎任务暂停 / 删除 / 重试接口，不引入媒体海报爬虫或 AI 依赖。新增配置须进入 Pydantic schema、WebUI 标签 / 提示、默认示例与文档；修改数据格式须保留旧去重状态、升级前备份和兼容验证。

```sh
uv sync --locked --extra dev
uv run ruff check aniopen_sync tests scripts
uv run ruff format --check aniopen_sync tests scripts
uv run pytest -q
npm ci --prefix web
npm test --prefix web
npm run build --prefix web
npm audit --prefix web --audit-level=high --registry=https://registry.npmjs.org
uv build
uv run python scripts/check_sdist.py --run-tests
```

真实协议与浏览器验证均使用一次性本地容器 / 生成的数据，绝不能对生产下载器运行测试：

```sh
uv run python scripts/check_downloaders.py
cd web && npx playwright install chromium && cd ..
uv run python scripts/check_compose.py --browser
```

协议脚本验证 aria2 下载生成的无害文件和 qB 提交 / 状态查询，无任务控制 API。Compose 脚本生成独立项目与数据卷，结束后删除这些测试资源，不触碰常用数据卷。受限构建网络可给 Compose 脚本传 `--build-proxy URL`；本机有 Chrome 时可设置 `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH`。

CI 覆盖 Python 3.11 / 3.12 / 3.13、前端单元测试、完整容器冷启动、浏览器表单与认证、aria2 和 qB 版本矩阵。源码包包含测试与前端源码；wheel 是后端组件，不包含完整产品界面。发布运行方式为 Git checkout + Compose，无预设的公开镜像仓库地址。

提交修复时说明触发条件、最终行为、验证结果与仍存在的边界；避免上传真实数据目录、凭据与个人运维记录。上游归属见 NOTICE。在线映射继续使用实时 main，修改缓存策略不得冻结数据更新。
