"""FastAPI 应用：Kubernetes 本地盘上线预检。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.datastructures import UploadFile

from .solver import (
    MAX_PODS,
    MAX_PVS,
    ValidationError,
    normalize_options,
    plan,
    plan_json_text,
)

BASE_DIR = Path(__file__).resolve().parent
MAX_UPLOAD_BYTES = 1_000_000

app = FastAPI(
    title="Kubernetes 本地盘上线预检",
    description="导入 PV / Pod 清单 JSON，计算最多能同时启动的 Pod 组合。",
    version="1.1.0",
)

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


class JsonBody(BaseModel):
    json_text: str
    min_pods: Optional[Any] = None
    time_limit_ms: Optional[Any] = None


def _result_response(
    payload: Any,
    min_pods: Any = None,
    time_limit_ms: Any = None,
) -> JSONResponse:
    try:
        min_pods_value, time_limit_value = normalize_options(min_pods, time_limit_ms)
        return JSONResponse(
            plan(
                payload,
                min_pods=min_pods_value,
                time_limit_ms=time_limit_value,
            )
        )
    except ValidationError as exc:
        return JSONResponse(
            status_code=422,
            content={"detail": exc.errors},
        )


def _options_from_query(request: Request) -> tuple:
    """从 query string 读取两个可选参数，非法值转成中文 422。"""
    min_pods = request.query_params.get("min_pods")
    time_limit_ms = request.query_params.get("time_limit_ms")
    try:
        return normalize_options(min_pods, time_limit_ms)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors)


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((BASE_DIR / "static" / "index.html").read_text("utf-8"))


@app.get("/healthz")
def healthz() -> Dict[str, Any]:
    return {"status": "ok", "limits": {"max_pvs": MAX_PVS, "max_pods": MAX_PODS}}


@app.get("/api/sample")
def sample() -> JSONResponse:
    text = (BASE_DIR / "samples" / "contention.json").read_text("utf-8")
    return JSONResponse({"name": "contention", "json_text": text})


@app.post("/api/plan")
def plan_from_text(body: JsonBody, request: Request) -> JSONResponse:
    # body 字段优先；同时兼容 query string 传参。
    min_pods = body.min_pods
    if min_pods is None:
        min_pods = request.query_params.get("min_pods")
    time_limit_ms = body.time_limit_ms
    if time_limit_ms is None:
        time_limit_ms = request.query_params.get("time_limit_ms")
    try:
        min_pods_value, time_limit_value = normalize_options(min_pods, time_limit_ms)
        return JSONResponse(
            plan_json_text(
                body.json_text,
                min_pods=min_pods_value,
                time_limit_ms=time_limit_value,
            )
        )
    except ValidationError as exc:
        return JSONResponse(status_code=422, content={"detail": exc.errors})


@app.post("/api/plan/upload")
async def plan_from_upload(request: Request) -> JSONResponse:
    """接收 multipart 文件上传，字段名 `file`；选项走 query string 或表单字段。"""
    min_pods_value, time_limit_value = _options_from_query(request)
    form = await request.form()
    upload = form.get("file")
    if not isinstance(upload, UploadFile):
        raise HTTPException(status_code=400, detail="缺少上传文件字段 file")
    # query 未提供时，允许用同名表单字段补充。
    if min_pods_value is None and form.get("min_pods"):
        min_pods_value, _ = normalize_options(form.get("min_pods"), None)
    if time_limit_value is None and form.get("time_limit_ms"):
        _, time_limit_value = normalize_options(None, form.get("time_limit_ms"))
    raw = await upload.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="文件超过 1 MB 上限")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="文件必须是 UTF-8 编码的 JSON")
    try:
        return JSONResponse(
            plan_json_text(
                text,
                min_pods=min_pods_value,
                time_limit_ms=time_limit_value,
            )
        )
    except ValidationError as exc:
        return JSONResponse(status_code=422, content={"detail": exc.errors})


@app.post("/api/plan/json")
def plan_from_json(payload: Dict[str, Any], request: Request) -> JSONResponse:
    min_pods_value, time_limit_value = _options_from_query(request)
    # body 内也允许携带两个选项（query 优先）；求解前剥离，避免被当成清单未知字段。
    body_min = payload.get("min_pods")
    body_limit = payload.get("time_limit_ms")
    if min_pods_value is None and body_min is not None:
        min_pods_value, _ = normalize_options(body_min, None)
    if time_limit_value is None and body_limit is not None:
        _, time_limit_value = normalize_options(None, body_limit)
    clean_payload = {
        key: value
        for key, value in payload.items()
        if key not in {"min_pods", "time_limit_ms"}
    }
    return _result_response(
        clean_payload, min_pods=min_pods_value, time_limit_ms=time_limit_value
    )


def _samples_dir_entries() -> List[Dict[str, str]]:
    return [
        {"name": path.stem, "file": path.name}
        for path in sorted((BASE_DIR / "samples").glob("*.json"))
    ]


@app.get("/api/samples")
def list_samples() -> List[Dict[str, str]]:
    return _samples_dir_entries()


@app.get("/api/samples/{name}")
def get_sample(name: str) -> JSONResponse:
    path = BASE_DIR / "samples" / f"{name}.json"
    if not path.resolve().is_file() or path.resolve().parent != (BASE_DIR / "samples").resolve():
        raise HTTPException(status_code=404, detail="样例不存在")
    return JSONResponse({"name": name, "json_text": path.read_text("utf-8")})


@app.exception_handler(ValidationError)
def _validation_handler(_request: Request, exc: ValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": exc.errors})
