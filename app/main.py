"""FastAPI 应用入口：DNS SOA 序列号推进审计 API。"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse

from .schemas import (
    AuditItem,
    AuditRequest,
    AuditResponse,
    DeltaPair,
    ProposeItem,
    ProposeRequest,
    ProposeResponse,
    TransferPlanItem,
    TransferPlanRequest,
    TransferPlanResponse,
)
from .serial import (
    compare_serial,
    forward_distance,
    propose_serial,
)
from .transfer import (
    Delta,
    DeltaSetError,
    follow_delta_chain,
    normalize_deltas,
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


@app.post("/transfer-plan", response_model=TransferPlanResponse)
def transfer_plan(request: TransferPlanRequest) -> TransferPlanResponse:
    """为每个副本规划追到目标序列号的传输方式（IXFR / AXFR）。

    先校验增量集合（422）：

    * 非严格更新的增量（to_serial 相对 from_serial 为 equal / older /
      ambiguous，按 RFC 1982 环形顺序判定）拒绝；
    * 同一 from_serial 指向不同 to_serial 的分叉拒绝；
    * 完全重复的增量去重。

    再为每个副本沿增量链追到目标：

    * ``up_to_date``：当前值已等于目标；
    * ``ixfr``：存在唯一完整链，``ordered_deltas`` 按应用顺序返回；
    * ``axfr``：遇到缺口（``chain_gap``）、环路（``cycle_detected``）
      或与目标不可比较（``target_not_comparable``：目标相对当前值为回退
      或正对点），回退全量传输并给出 ``reason``。

    链路匹配只按 from_serial 精确衔接，不按整数大小排序；
    ``results`` 保持副本输入顺序。
    """
    _check_duplicate_names([r.name for r in request.replicas])

    try:
        index = normalize_deltas(
            [Delta(d.from_serial, d.to_serial) for d in request.deltas]
        )
    except DeltaSetError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": exc.error, **exc.detail},
        )

    results = []
    for replica in request.replicas:
        mode, chain, reason = follow_delta_chain(
            replica.current, request.target, index
        )
        results.append(
            TransferPlanItem(
                name=replica.name,
                current=replica.current,
                mode=mode,
                ordered_deltas=[
                    DeltaPair(
                        from_serial=delta.from_serial, to_serial=delta.to_serial
                    )
                    for delta in chain
                ],
                reason=reason,
            )
        )

    return TransferPlanResponse(target=request.target, results=results)
