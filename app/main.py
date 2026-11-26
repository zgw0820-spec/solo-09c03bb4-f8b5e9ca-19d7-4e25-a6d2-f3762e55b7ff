"""FastAPI 应用：Kubernetes 本地盘上线预检。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.datastructures import UploadFile

from .solver import MAX_PODS, MAX_PVS, ValidationError, plan, plan_json_text

BASE_DIR = Path(__file__).resolve().parent
MAX_UPLOAD_BYTES = 1_000_000

app = FastAPI(
    title="Kubernetes 本地盘上线预检",
    description="导入 PV / Pod 清单 JSON，计算最多能同时启动的 Pod 组合。",
    version="1.0.0",
)

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


class JsonBody(BaseModel):
    json_text: str


def _result_response(payload: Any) -> JSONResponse:
    try:
        return JSONResponse(plan(payload))
    except ValidationError as exc:
        return JSONResponse(
            status_code=422,
            content={"detail": exc.errors},
        )


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
def plan_from_text(body: JsonBody) -> JSONResponse:
    try:
        return JSONResponse(plan_json_text(body.json_text))
    except ValidationError as exc:
        return JSONResponse(status_code=422, content={"detail": exc.errors})


@app.post("/api/plan/upload")
async def plan_from_upload(request: Request) -> JSONResponse:
    """接收 multipart 文件上传，字段名 `file`。"""
    form = await request.form()
    upload = form.get("file")
    if not isinstance(upload, UploadFile):
        raise HTTPException(status_code=400, detail="缺少上传文件字段 file")
    raw = await upload.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="文件超过 1 MB 上限")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="文件必须是 UTF-8 编码的 JSON")
    try:
        return JSONResponse(plan_json_text(text))
    except ValidationError as exc:
        return JSONResponse(status_code=422, content={"detail": exc.errors})


@app.post("/api/plan/json")
def plan_from_json(payload: Dict[str, Any]) -> JSONResponse:
    return _result_response(payload)


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
