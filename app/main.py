"""FastAPI 应用入口：DNS SOA 序列号推进审计 API。"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse

from .schemas import (
    AuditItem,
    AuditRequest,
    AuditResponse,
    ProposeItem,
    ProposeRequest,
    ProposeResponse,
)
from .serial import (
    compare_serial,
    forward_distance,
    propose_serial,
)

app = FastAPI(
    title="DNS SOA 序列号推进审计 API",
    description=(
        "基于 RFC 1982 环形算术，对多个 DNS 副本的 SOA 序列号推进进行审计，"
        "并推导对所有副本都严格更新的最佳拟发布值。"
    ),
    version="1.0.0",
)


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


def _check_duplicate_names(names: list[str]) -> None:
    seen: set[str] = set()
    for name in names:
        if name in seen:
            raise HTTPException(
                status_code=422,
                detail={"error": "duplicate_replica_name", "name": name},
            )
        seen.add(name)


@app.post("/audit", response_model=AuditResponse)
def audit(request: AuditRequest) -> AuditResponse:
    """逐一判定拟发布值相对各副本当前序列号的关系。

    * newer：严格更新且可比较（1 <= 前向距离 < 2^31）
    * equal：相等（距离 0）
    * older：回退（2^31 < 距离 < 2^32）
    * ambiguous：差值正好 2^31（正对点，RFC 1982 规定不可比较）

    非 newer 的副本均为阻塞副本。
    """
    _check_duplicate_names([r.name for r in request.replicas])

    results = []
    blocking_replicas = []
    for replica in request.replicas:
        relation = compare_serial(replica.current, request.proposed)
        distance = forward_distance(replica.current, request.proposed)
        blocking = relation != "newer"
        if blocking:
            blocking_replicas.append(replica.name)
        results.append(
            AuditItem(
                name=replica.name,
                current=replica.current,
                proposed=request.proposed,
                relation=relation,
                forward_distance=distance,
                blocking=blocking,
            )
        )

    return AuditResponse(
        proposed=request.proposed,
        results=results,
        blocked=bool(blocking_replicas),
        blocking_replicas=blocking_replicas,
    )


@app.post("/propose", response_model=ProposeResponse)
def propose(request: ProposeRequest) -> ProposeResponse:
    """推导对所有副本都严格更新且可比较的候选序列号。

    最小化最大前向距离；同分取数值最小者。
    若各副本当前序列号无法被任意单一值同时严格推进（例如存在相差 2^31
    的正对点，或序列跨越超过半环），返回 409。
    """
    _check_duplicate_names([r.name for r in request.replicas])

    outcome = propose_serial([r.current for r in request.replicas])
    if outcome is None:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "no_valid_proposal",
                "message": (
                    "不存在能对所有副本都严格更新且可比较的候选值："
                    "副本当前序列号跨越半环（含相差 2^31 的正对点）"
                ),
            },
        )

    proposed, max_distance = outcome
    results = [
        ProposeItem(
            name=replica.name,
            current=replica.current,
            relation=compare_serial(replica.current, proposed),
            forward_distance=forward_distance(replica.current, proposed),
        )
        for replica in request.replicas
    ]

    return ProposeResponse(
        proposed=proposed,
        max_forward_distance=max_distance,
        results=results,
    )
