import os
import secrets
from contextlib import asynccontextmanager
from importlib.metadata import version
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from filelock import FileLock, Timeout
from pydantic import ValidationError

from .config import Config, ConfigStore, load_yaml
from .db import Database
from .security import access_token, allowed_host, mask_config, preserve_secrets
from .service import Service


def create_app(data_dir: Path | None = None, web_dir: Path | None = None):
    data_dir = data_dir or Path(os.environ.get("ANIOPEN_DATA_DIR", "./data"))
    web_dir = web_dir or Path(
        os.environ.get("ANIOPEN_WEB_DIR", Path(__file__).resolve().parent.parent / "web/dist")
    )

    @asynccontextmanager
    async def lifespan(app):
        data_dir.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(data_dir / ".instance.lock"))
        try:
            lock.acquire(timeout=0)
        except Timeout as exc:
            raise RuntimeError("数据目录已被其它进程使用；请使用单个 worker") from exc
        service = None
        try:
            app.state.access_token = access_token(data_dir)
            config = ConfigStore(data_dir / "config.yaml")
            service = Service(config, Database(data_dir / "history.sqlite3"), data_dir)
            app.state.service = service
            service.start()
            yield
        finally:
            try:
                if service:
                    service.stop()
            finally:
                lock.release()

    app = FastAPI(
        title="ANiOpen Sync",
        version=version("aniopen-sync"),
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.middleware("http")
    async def security_headers(request, call_next):
        if not allowed_host(request.headers.get("host", "")):
            response = JSONResponse(
                {"detail": "Host 不在允许列表；自定义域名请设置 ANIOPEN_ALLOWED_HOSTS"}, status_code=400
            )
        else:
            response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
        )
        if request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def authorize(request: Request, authorization: str = Header("")):
        expected = ("Bearer " + app.state.access_token).encode("utf-8")
        if not secrets.compare_digest(authorization.encode("utf-8"), expected):
            raise HTTPException(401, "请填写访问令牌")
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc.lower() != request.headers.get("host", "").lower():
            raise HTTPException(403, "不允许跨站请求")

    @app.get("/api/health")
    def health():
        return {"status": "ok", "authentication_required": True, "version": app.version}

    @app.get("/api/health/ready")
    def readiness():
        payload = app.state.service.dependency_health.snapshot()
        return JSONResponse(
            payload | {"version": app.version}, status_code=200 if payload["status"] == "ok" else 503
        )

    def config_response(config, reveal=False):
        payload = config.model_dump() if reveal else mask_config(config)
        return {
            "config": payload,
            "yaml": yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
            "secrets_redacted": not reveal,
        }

    @app.get("/api/config", dependencies=[Depends(authorize)])
    def get_config():
        return config_response(app.state.service.config.get())

    @app.post("/api/config/reveal", dependencies=[Depends(authorize)])
    def reveal_config():
        return config_response(app.state.service.config.get(), reveal=True)

    @app.get("/api/config/schema", dependencies=[Depends(authorize)])
    def schema():
        return Config.model_json_schema()

    def parse_config(payload):
        try:
            value = load_yaml(payload["yaml"]) if "yaml" in payload else payload
            value = preserve_secrets(value, app.state.service.config.get())
            return Config.model_validate(value)
        except ValidationError as exc:
            raise HTTPException(422, exc.errors(include_input=False, include_context=False)) from exc
        except (yaml.YAMLError, ValueError, TypeError, AttributeError) as exc:
            raise HTTPException(422, "配置 / YAML 格式无效，请检查字段类型") from exc

    @app.post("/api/config/validate", dependencies=[Depends(authorize)])
    def validate_config(payload: dict):
        return config_response(parse_config(payload))

    @app.put("/api/config", dependencies=[Depends(authorize)])
    def save_config(payload: dict):
        app.state.service.save_config(parse_config(payload))
        return {"saved": True}

    @app.get("/api/status", dependencies=[Depends(authorize)])
    def status():
        return app.state.service.status()

    @app.get("/api/tasks", dependencies=[Depends(authorize)])
    def tasks(
        page: int = Query(1, ge=1),
        page_size: int = Query(30, ge=1, le=100),
        search: str = "",
        status: str = "",
    ):
        return app.state.service.db.tasks(page, page_size, search, status)

    @app.post("/api/tasks/{task_id}/resolve-submission", dependencies=[Depends(authorize)])
    def resolve_submission(task_id: int, payload: dict):
        confirmed = payload.get("confirmed_not_submitted", False)
        if not isinstance(confirmed, bool):
            raise HTTPException(422, "confirmed_not_submitted 必须是布尔值")
        try:
            return app.state.service.resolve_unknown(task_id, confirmed)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/sync", status_code=202, dependencies=[Depends(authorize)])
    def sync():
        service = app.state.service
        if not service.config.get().source.enabled:
            raise HTTPException(409, "请先在配置中启用 ANi 同步")
        if not service.trigger():
            raise HTTPException(409, "同步正在进行中")
        return {"accepted": True}

    @app.get("/api/openapi.json", dependencies=[Depends(authorize)])
    def openapi():
        return app.openapi()

    if (web_dir / "assets").exists():
        app.mount("/assets", StaticFiles(directory=web_dir / "assets"), name="assets")

    @app.get("/")
    def index():
        if (web_dir / "index.html").exists():
            return FileResponse(web_dir / "index.html")
        raise HTTPException(503, "此项目使用完整容器发行；源码开发请先构建 web 前端")

    return app
