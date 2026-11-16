"""把纯标准库的联合试验服务适配为 FastAPI 路由。

federation 包本体不依赖 fastapi；仅本模块在被主应用导入时使用 FastAPI。
端点与 src/federation/app.py 的 stdlib HTTP 服务保持一致，便于二选一部署：
- 独立部署：python -m src.federation.cli center（无第三方依赖）
- 内嵌部署：主 FastAPI 应用挂载本路由（复用现有鉴权与网关）
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException

from src.config.settings import FEDERATION_DB
from src.federation.service import FederationError, FederationService
from src.federation.store import Store
from src.middlewares.auth import can_write, current_user

router = APIRouter(prefix="/federation", tags=["federation"])

_service = None


def get_service() -> FederationService:
    global _service
    if _service is None:
        os.makedirs(os.path.dirname(FEDERATION_DB) or ".", exist_ok=True)
        _service = FederationService(Store(FEDERATION_DB))
    return _service


@router.get("/trials")
def list_trials(svc=Depends(get_service), _=Depends(current_user)):
    return svc.list_views()


@router.get("/trials/{trial_id}")
def get_trial(trial_id: int, svc=Depends(get_service), _=Depends(current_user)):
    try:
        return svc.trial_view(trial_id)
    except FederationError as exc:
        raise HTTPException(404, str(exc))


@router.post("/trials", status_code=201)
def create_trial(body: dict, svc=Depends(get_service), _=Depends(can_write)):
    try:
        return svc.create_trial(body.get("name"),
                                body.get("confidence", 0.95),
                                body.get("roster"))
    except FederationError as exc:
        raise HTTPException(422, str(exc))


@router.post("/submissions")
def submit(body: dict, svc=Depends(get_service), _=Depends(current_user)):
    try:
        return svc.submit(body["trial_id"], body["station_id"],
                          body["sample_version"], body["payload"])
    except FederationError as exc:
        raise HTTPException(422, str(exc))
    except KeyError as exc:
        raise HTTPException(422, f"请求缺少字段：{exc}")


@router.post("/trials/{trial_id}/seal")
def seal(trial_id: int, svc=Depends(get_service), _=Depends(can_write)):
    try:
        return svc.seal(trial_id)
    except FederationError as exc:
        raise HTTPException(422, str(exc))
