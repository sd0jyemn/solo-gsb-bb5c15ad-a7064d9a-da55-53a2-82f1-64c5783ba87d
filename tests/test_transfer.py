"""IXFR 增量链规划单元测试（仅依赖标准库）。

运行：python -m unittest discover -s tests -v
"""

import random
import unittest

from app.serial import HALF, MAX_SERIAL, MOD
from app.transfer import (
    Delta,
    DeltaSetError,
    MODE_AXFR,
    MODE_IXFR,
    MODE_UP_TO_DATE,
    REASON_CHAIN_GAP,
    REASON_CYCLE_DETECTED,
    REASON_TARGET_NOT_COMPARABLE,
    follow_delta_chain,
    normalize_deltas,
)


def D(a, b):
    return Delta(from_serial=a, to_serial=b)


class NormalizeDeltasTests(unittest.TestCase):
    def test_basic_index(self):
        index = normalize_deltas([D(1, 2), D(2, 3)])
        self.assertEqual(index, {1: 2, 2: 3})

    def test_duplicate_deltas_deduped(self):
        index = normalize_deltas([D(1, 2), D(1, 2), D(2, 3), D(1, 2)])
        self.assertEqual(index, {1: 2, 2: 3})

    def test_wrap_around_delta_accepted(self):
        # 4294967295 -> 0 是严格更新（前向距离 1），不能按整数大小拒绝
        index = normalize_deltas([D(MAX_SERIAL, 0)])
        self.assertEqual(index, {MAX_SERIAL: 0})

    def test_equal_serial_rejected(self):
        with self.assertRaises(DeltaSetError) as ctx:
            normalize_deltas([D(5, 5)])
        self.assertEqual(ctx.exception.error, "delta_not_strictly_newer")
        self.assertEqual(ctx.exception.detail["relation"], "equal")
        self.assertEqual(ctx.exception.detail["forward_distance"], 0)

    def test_older_delta_rejected(self):
        with self.assertRaises(DeltaSetError) as ctx:
            normalize_deltas([D(2, 1)])
        self.assertEqual(ctx.exception.error, "delta_not_strictly_newer")
        self.assertEqual(ctx.exception.detail["relation"], "older")

    def test_ambiguous_delta_rejected(self):
        # 前向距离恰好 2^31：RFC 1982 不可比较，必须拒绝
        with self.assertRaises(DeltaSetError) as ctx:
            normalize_deltas([D(0, HALF)])
        self.assertEqual(ctx.exception.error, "delta_not_strictly_newer")
        self.assertEqual(ctx.exception.detail["relation"], "ambiguous")
        self.assertEqual(ctx.exception.detail["forward_distance"], HALF)

    def test_wrap_around_older_rejected(self):
        # 0 -> 4294967295：整数上“更大”，但环形顺序是回退，必须拒绝
        with self.assertRaises(DeltaSetError) as ctx:
            normalize_deltas([D(0, MAX_SERIAL)])
        self.assertEqual(ctx.exception.error, "delta_not_strictly_newer")
        self.assertEqual(ctx.exception.detail["relation"], "older")

    def test_fork_rejected(self):
        with self.assertRaises(DeltaSetError) as ctx:
            normalize_deltas([D(1, 2), D(1, 3)])
        self.assertEqual(ctx.exception.error, "conflicting_deltas")
        self.assertEqual(ctx.exception.detail["from_serial"], 1)
        self.assertEqual(ctx.exception.detail["to_serials"], [2, 3])

    def test_fork_after_duplicate_rejected(self):
        # 先出现重复（去重），之后出现真正分叉仍须拒绝
        with self.assertRaises(DeltaSetError) as ctx:
            normalize_deltas([D(1, 2), D(1, 2), D(1, 9)])
        self.assertEqual(ctx.exception.error, "conflicting_deltas")


class FollowDeltaChainTests(unittest.TestCase):
    def test_up_to_date(self):
        mode, chain, reason = follow_delta_chain(10, 10, {})
        self.assertEqual((mode, chain, reason), (MODE_UP_TO_DATE, [], None))

    def test_simple_ixfr_chain(self):
        # 增量乱序输入，结果必须按应用顺序排列
        index = normalize_deltas([D(2, 3), D(0, 1), D(1, 2)])
        mode, chain, reason = follow_delta_chain(0, 3, index)
        self.assertEqual(mode, MODE_IXFR)
        self.assertEqual(chain, [D(0, 1), D(1, 2), D(2, 3)])
        self.assertIsNone(reason)

    def test_single_hop_ixfr(self):
        index = normalize_deltas([D(MAX_SERIAL, 0)])
        mode, chain, reason = follow_delta_chain(MAX_SERIAL, 0, index)
        self.assertEqual(mode, MODE_IXFR)
        self.assertEqual(chain, [D(MAX_SERIAL, 0)])

    def test_chain_gap(self):
        index = normalize_deltas([D(0, 1), D(1, 2)])
        mode, chain, reason = follow_delta_chain(0, 5, index)
        self.assertEqual((mode, chain, reason), (MODE_AXFR, [], REASON_CHAIN_GAP))

    def test_gap_at_start(self):
        mode, chain, reason = follow_delta_chain(100, 105, {0: 1})
        self.assertEqual((mode, chain, reason), (MODE_AXFR, [], REASON_CHAIN_GAP))

    def test_target_older_is_not_comparable(self):
        # 当前值已在目标之后（回退发布），即便有增量也不追链
        index = {200: 201}
        mode, chain, reason = follow_delta_chain(200, 100, index)
        self.assertEqual(
            (mode, chain, reason),
            (MODE_AXFR, [], REASON_TARGET_NOT_COMPARABLE),
        )

    def test_target_antipode_is_not_comparable(self):
        mode, chain, reason = follow_delta_chain(0, HALF, {0: HALF - 1})
        self.assertEqual(
            (mode, chain, reason),
            (MODE_AXFR, [], REASON_TARGET_NOT_COMPARABLE),
        )

    def test_antipode_from_nonzero_is_not_comparable(self):
        current = 12345
        target = (current + HALF) % MOD
        mode, _, reason = follow_delta_chain(current, target, {})
        self.assertEqual(mode, MODE_AXFR)
        self.assertEqual(reason, REASON_TARGET_NOT_COMPARABLE)

    def test_cycle_detected(self):
        # 每跳都严格更新但整链绕环一周：0 -> 2^31-1 -> 2^32-2 -> 0
        # 各跳前向距离为 2^31-1、2^31-1、2，合法；对目标 5 构环
        index = normalize_deltas([D(0, HALF - 1), D(HALF - 1, MOD - 2), D(MOD - 2, 0)])
        mode, chain, reason = follow_delta_chain(0, 5, index)
        self.assertEqual((mode, chain, reason), (MODE_AXFR, [], REASON_CYCLE_DETECTED))

    def test_cycle_with_tail(self):
        # 链尾进入环：2^32-4 -> 0 -> 2^31-1 -> 2^32-2 -> 0
        tail = MAX_SERIAL - 3  # -> 0 的前向距离为 4，合法
        index = normalize_deltas([
            D(tail, 0),
            D(0, HALF - 1),
            D(HALF - 1, MOD - 2),
            D(MOD - 2, 0),
        ])
        mode, _, reason = follow_delta_chain(tail, 10, index)
        self.assertEqual((mode, reason), (MODE_AXFR, REASON_CYCLE_DETECTED))

    def test_chain_passes_target_exact_match_wins(self):
        # 链头序列中恰好等于目标即成功；多余增量与无关增量不影响
        index = normalize_deltas([
            D(0, 1), D(1, 2), D(2, 3),
            D(10, 11),             # 无关增量
            D(3, 100),             # 到达目标之后的“多余”增量，不应继续追
        ])
        mode, chain, reason = follow_delta_chain(0, 3, index)
        self.assertEqual(mode, MODE_IXFR)
        self.assertEqual(chain, [D(0, 1), D(1, 2), D(2, 3)])
        self.assertIsNone(reason)

    def test_chain_overshoots_then_gap(self):
        # 100 -> 110 越过目标 105（该跳对 100 仍是严格更新），之后无增量：缺口
        index = normalize_deltas([D(100, 110)])
        mode, chain, reason = follow_delta_chain(100, 105, index)
        self.assertEqual((mode, chain, reason), (MODE_AXFR, [], REASON_CHAIN_GAP))

    def test_ixfr_chain_strictly_newer_hop_by_hop(self):
        index = normalize_deltas([
            D(MAX_SERIAL - 1, MAX_SERIAL),
            D(MAX_SERIAL, 0),
            D(0, 1),
        ])
        mode, chain, reason = follow_delta_chain(MAX_SERIAL - 1, 1, index)
        self.assertEqual(mode, MODE_IXFR)
        self.assertEqual(
            chain,
            [D(MAX_SERIAL - 1, MAX_SERIAL), D(MAX_SERIAL, 0), D(0, 1)],
        )
        self.assertIsNone(reason)


def _fast_validate(raw_deltas, bits):
    """与 app.transfer.normalize_deltas 相同的算法，参数化位宽。

    成功返回 {from_serial: to_serial}；非法返回错误码字符串。
    """
    mod = 1 << bits
    half = 1 << (bits - 1)
    index = {}
    for f, t in raw_deltas:
        d = (t - f) % mod
        if not (1 <= d <= half - 1):
            return "delta_not_strictly_newer"
        if f in index and index[f] != t:
            return "conflicting_deltas"
        index.setdefault(f, t)
    return index


def _fast_follow(current, target, index, bits):
    """与 app.transfer.follow_delta_chain 相同的算法，参数化位宽。

    返回 (mode, path_len, reason)。
    """
    mod = 1 << bits
    half = 1 << (bits - 1)
    if current == target:
        return MODE_UP_TO_DATE, 0, None
    d = (target - current) % mod
    if not (1 <= d <= half - 1):
        return MODE_AXFR, 0, REASON_TARGET_NOT_COMPARABLE
    visited = {current}
    node = current
    steps = 0
    while True:
        nxt = index.get(node)
        if nxt is None:
            return MODE_AXFR, 0, REASON_CHAIN_GAP
        node = nxt
        steps += 1
        if node == target:
            return MODE_IXFR, steps, None
        if node in visited:
            return MODE_AXFR, 0, REASON_CYCLE_DETECTED
        visited.add(node)


def _brute_validate_and_plan(currents_targets_deltas, bits):
    """小位宽独立参考实现：与 app 逻辑一致但单独写出，交叉核对。

    输入: (replicas[(name, current)], target, raw_deltas[(f, t)...])
    输出: (error_or_none, {name: (mode, reason, path_len)})
    """
    replicas, target, raw_deltas = currents_targets_deltas
    mod = 1 << bits
    half = 1 << (bits - 1)

    def dist(a, b):
        return (b - a) % mod

    def is_newer(a, b):
        d = dist(a, b)
        return 1 <= d <= half - 1

    # 校验增量集合并建索引（重复去重、分叉拒绝）
    index = {}
    for f, t in raw_deltas:
        if not is_newer(f, t):
            return ("delta_not_strictly_newer", None)
        if f in index and index[f] != t:
            return ("conflicting_deltas", None)
        index.setdefault(f, t)

    out = {}
    for name, current in replicas:
        if current == target:
            out[name] = (MODE_UP_TO_DATE, None, 0)
            continue
        if not is_newer(current, target):
            out[name] = (MODE_AXFR, REASON_TARGET_NOT_COMPARABLE, 0)
            continue
        visited = {current}
        node = current
        steps = 0
        while True:
            nxt = index.get(node)
            if nxt is None:
                out[name] = (MODE_AXFR, REASON_CHAIN_GAP, 0)
                break
            node = nxt
            steps += 1
            if node == target:
                out[name] = (MODE_IXFR, None, steps)
                break
            if node in visited:
                out[name] = (MODE_AXFR, REASON_CYCLE_DETECTED, 0)
                break
            visited.add(node)
    return (None, out)


class TransferBruteForceTests(unittest.TestCase):
    def test_matches_reference_small_spaces(self):
        rng = random.Random(20260921)
        for bits in (3, 4, 6):
            mod = 1 << bits
            half = 1 << (bits - 1)
            for trial in range(800):
                target = rng.randrange(mod)
                n_replicas = rng.randint(1, 4)
                replicas = [
                    (f"r{i}", rng.randrange(mod)) for i in range(n_replicas)
                ]
                n_deltas = rng.randint(0, 8)
                raw = []
                for _ in range(n_deltas):
                    f = rng.randrange(mod)
                    t = rng.randrange(mod)
                    raw.append((f, t))
                    if rng.random() < 0.2:  # 制造重复
                        raw.append((f, t))

                ref_error, ref_plan = _brute_validate_and_plan(
                    (replicas, target, raw), bits
                )
                fast_index = _fast_validate(raw, bits)

                if ref_error is not None:
                    self.assertEqual(fast_index, ref_error, msg=f"raw={raw}")
                    continue

                for name, current in replicas:
                    mode, steps, reason = _fast_follow(
                        current, target, fast_index, bits
                    )
                    ref_mode, ref_reason, ref_len = ref_plan[name]
                    self.assertEqual(
                        (mode, reason, steps),
                        (ref_mode, ref_reason, ref_len),
                        msg=(
                            f"bits={bits} target={target} current={current} "
                            f"raw={raw}"
                        ),
                    )

    def test_small_spaces_exhaustive_deltas(self):
        # 4 位空间：穷举全部单增量，并抽样双增量；对全部 (current, target) 核对
        import itertools

        bits = 4
        mod = 1 << bits
        cases = [(f, t) for f in range(mod) for t in range(mod) if f != t]
        for raw in [(c,) for c in cases]:
            self._check_one(raw, bits)
        for raw in itertools.islice(itertools.permutations(cases, 2), 0, 20000):
            self._check_one(raw, bits)

    def _check_one(self, raw, bits):
        mod = 1 << bits
        replicas = [(f"r{c}", c) for c in range(mod)]
        for target in range(mod):
            ref_error, ref_plan = _brute_validate_and_plan(
                (replicas, target, list(raw)), bits
            )
            fast_index = _fast_validate(list(raw), bits)
            if ref_error is not None:
                self.assertEqual(fast_index, ref_error)
                continue
            for name, current in replicas:
                mode, steps, reason = _fast_follow(
                    current, target, fast_index, bits
                )
                ref_mode, ref_reason, ref_len = ref_plan[name]
                self.assertEqual(
                    (mode, reason, steps),
                    (ref_mode, ref_reason, ref_len),
                    msg=(
                        f"bits={bits} target={target} current={current} raw={raw}"
                    ),
                )


if __name__ == "__main__":
    unittest.main()
