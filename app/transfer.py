"""IXFR 增量链规划：沿 (from_serial, to_serial) 增量链把副本追到目标序列号。

链路匹配遵循 RFC 1982 环形顺序：只按 ``from_serial`` 精确匹配下一跳，
并用模 2^32 前向距离判定“严格更新”，**不按整数大小排序**。

规则：

* 每条增量必须 ``to_serial`` 对 ``from_serial`` 严格更新
  （``1 <= (to - from) mod 2^32 <= 2^31 - 1``），否则该增量集合非法；
* 同一 ``from_serial`` 指向不同 ``to_serial`` 为分叉，增量集合非法；
* 完全相同的增量去重；
* 副本当前值已等于目标 -> ``up_to_date``；
* 存在唯一完整链 -> ``ixfr``（分叉已拒绝，链确定性唯一）；
* 缺口 / 环路 / 与目标不可比较 -> ``axfr``，附 ``reason``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .serial import NEWER, compare_serial, forward_distance, is_strictly_newer

MODE_UP_TO_DATE = "up_to_date"
MODE_IXFR = "ixfr"
MODE_AXFR = "axfr"

# mode 为 axfr 时的原因
REASON_CHAIN_GAP = "chain_gap"                      # 缺口：链中断，没有下一条增量
REASON_CYCLE_DETECTED = "cycle_detected"           # 环路：链重复经过某个序列号
REASON_TARGET_NOT_COMPARABLE = "target_not_comparable"  # 目标不在前半环内（older/ambiguous）

# 增量集合非法时的错误码
ERROR_NOT_STRICTLY_NEWER = "delta_not_strictly_newer"
ERROR_CONFLICTING_DELTAS = "conflicting_deltas"


@dataclass(frozen=True)
class Delta:
    """一条 IXFR 增量：把序列号从 ``from_serial`` 推进到 ``to_serial``。"""

    from_serial: int
    to_serial: int


class DeltaSetError(ValueError):
    """增量集合非法：存在非严格更新的增量，或同一 from_serial 的分叉。"""

    def __init__(self, error: str, detail: dict) -> None:
        super().__init__(error)
        self.error = error
        self.detail = detail


def normalize_deltas(deltas: Sequence[Delta]) -> Dict[int, int]:
    """去重并校验增量集合，返回 ``from_serial -> to_serial`` 索引。

    * 完全相同的重复增量直接去重；
    * 非严格更新（``equal`` / ``older`` / ``ambiguous``）抛
      ``DeltaSetError("delta_not_strictly_newer")``；
    * 同一 ``from_serial`` 指向不同 ``to_serial``（分叉）抛
      ``DeltaSetError("conflicting_deltas")``。
    """
    index: Dict[int, int] = {}
    for delta in deltas:
        if not is_strictly_newer(delta.from_serial, delta.to_serial):
            raise DeltaSetError(
                ERROR_NOT_STRICTLY_NEWER,
                {
                    "from_serial": delta.from_serial,
                    "to_serial": delta.to_serial,
                    "relation": compare_serial(delta.from_serial, delta.to_serial),
                    "forward_distance": forward_distance(
                        delta.from_serial, delta.to_serial
                    ),
                },
            )
        existing = index.get(delta.from_serial)
        if existing is None:
            index[delta.from_serial] = delta.to_serial
        elif existing != delta.to_serial:
            raise DeltaSetError(
                ERROR_CONFLICTING_DELTAS,
                {
                    "from_serial": delta.from_serial,
                    "to_serials": [existing, delta.to_serial],
                },
            )
        # existing == delta.to_serial：完全重复，去重跳过
    return index


def follow_delta_chain(
    current: int,
    target: int,
    index: Dict[int, int],
) -> Tuple[str, List[Delta], Optional[str]]:
    """从 *current* 出发沿增量链追 *target*。

    返回 ``(mode, ordered_deltas, reason)``：

    * ``current == target`` -> ``("up_to_date", [], None)``；
    * 目标相对当前值不是严格更新（回退 older，或正对点 ambiguous）->
      ``("axfr", [], "target_not_comparable")``；
    * 链中断（找不到以链头为 from_serial 的增量）->
      ``("axfr", [], "chain_gap")``；
    * 链重复经过某个序列号 -> ``("axfr", [], "cycle_detected")``；
    * 恰好追到目标 -> ``("ixfr", 有序增量链, None)``。

    分叉已在 :func:`normalize_deltas` 阶段拒绝，故从任一节点出发的链
    确定性唯一；每步消耗一个 ``from_serial``，至多走 ``len(index) + 1`` 步。
    """
    if current == target:
        return MODE_UP_TO_DATE, [], None

    if compare_serial(current, target) != NEWER:
        # 目标不在当前值的前半环内：回退（older）或正对点（ambiguous），
        # 沿严格更新的增量链不可能追到目标
        return MODE_AXFR, [], REASON_TARGET_NOT_COMPARABLE

    visited = {current}
    chain: List[Delta] = []
    node = current
    while True:
        to_serial = index.get(node)
        if to_serial is None:
            return MODE_AXFR, [], REASON_CHAIN_GAP
        chain.append(Delta(node, to_serial))
        node = to_serial
        if node == target:
            return MODE_IXFR, chain, None
        if node in visited:
            return MODE_AXFR, [], REASON_CYCLE_DETECTED
        visited.add(node)
