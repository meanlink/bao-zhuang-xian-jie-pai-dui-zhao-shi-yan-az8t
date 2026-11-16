"""统计正确性：充分统计量合并与纯标准库 t 分布。

若环境装有 scipy，则同时与 scipy.stats.t 交叉验证；否则使用教材已知值。
"""
import math
import random
import unittest

from src.federation.statistics import combine_summaries, local_summary
from src.federation.tdist import t_cdf, t_ppf, two_sided_pvalue
from src.federation.welch import analyze_payloads, merge_groups


def _naive(values):
    n = len(values)
    mean = sum(values) / n
    ss = sum((x - mean) ** 2 for x in values)
    return {"n": n, "mean": mean, "ss": ss}


class SummaryTests(unittest.TestCase):
    def test_local_summary_matches_naive_and_welford(self):
        rng = random.Random(7)
        values = [rng.gauss(10, 2.5) for _ in range(50)]
        got = local_summary(values)
        want = _naive(values)
        self.assertEqual(got["n"], want["n"])
        self.assertAlmostEqual(got["mean"], want["mean"], places=12)
        self.assertAlmostEqual(got["ss"], want["ss"], places=8)

    def test_combine_equals_pooled_raw_sample(self):
        rng = random.Random(11)
        chunks = [[rng.gauss(20, 3) for _ in range(k)]
                  for k in (10, 17, 23, 5)]
        parts = [local_summary(c) for c in chunks]
        merged = combine_summaries(parts)
        pooled = [x for c in chunks for x in c]
        want = _naive(pooled)
        self.assertEqual(merged["n"], want["n"])
        self.assertAlmostEqual(merged["mean"], want["mean"], places=12)
        self.assertAlmostEqual(merged["ss"], want["ss"], places=7)
        self.assertAlmostEqual(merged["variance"],
                               want["ss"] / (want["n"] - 1), places=10)

    def test_combine_two_groups_matches_direct_welch_inputs(self):
        # 联邦合并后的 A/B 汇总与“直接拼原始值再算”的均值方差一致
        rng = random.Random(3)
        a_stations = [[rng.gauss(19.5, 1.4) for _ in range(30)] for _ in range(3)]
        b_stations = [[rng.gauss(18.2, 2.1) for _ in range(25)] for _ in range(3)]
        payloads = [
            {"groups": {"A": local_summary(a), "B": local_summary(b)}}
            for a, b in zip(a_stations, b_stations)]
        merged = merge_groups(payloads)
        raw_a = [x for c in a_stations for x in c]
        raw_b = [x for c in b_stations for x in c]
        self.assertEqual(merged["A"]["n"], len(raw_a))
        self.assertAlmostEqual(merged["A"]["mean"], sum(raw_a) / len(raw_a), 12)
        self.assertAlmostEqual(merged["B"]["mean"], sum(raw_b) / len(raw_b), 12)
        self.assertAlmostEqual(
            merged["A"]["variance"], _naive(raw_a)["ss"] / (len(raw_a) - 1), 10)

    def test_federated_equals_centralized_welch(self):
        # 关键口径：联邦结果 == 把所有原始片段集中后的 Welch 结果
        try:
            from scipy import stats
            import numpy as np
        except ImportError:
            self.skipTest("scipy/numpy 不可用时跳过交叉验证，口径由其余断言保证")
        rng = random.Random(21)
        a = [rng.gauss(19.5, 1.4) for _ in range(80)]
        b = [rng.gauss(18.2, 2.1) for _ in range(74)]
        # 拆成 3 个站点
        payloads = [
            {"groups": {"A": local_summary(a[:30]), "B": local_summary(b[:24])}},
            {"groups": {"A": local_summary(a[30:55]), "B": local_summary(b[24:50])}},
            {"groups": {"A": local_summary(a[55:]), "B": local_summary(b[50:])}}]
        fed = analyze_payloads(payloads, 0.95)
        na, nb = len(a), len(b)
        va, vb = float(np.var(a, ddof=1)), float(np.var(b, ddof=1))
        ma, mb = float(np.mean(a)), float(np.mean(b))
        se2 = va / na + vb / nb
        df = se2 ** 2 / ((va / na) ** 2 / (na - 1) + (vb / nb) ** 2 / (nb - 1))
        t = (ma - mb) / math.sqrt(se2)
        p = float(2 * stats.t.sf(abs(t), df))
        margin = float(stats.t.ppf(0.975, df)) * math.sqrt(se2)
        self.assertAlmostEqual(fed["statistic_t"], t, places=10)
        self.assertAlmostEqual(fed["p_value"], p, places=10)
        self.assertAlmostEqual(fed["df"], df, places=9)
        self.assertAlmostEqual(fed["ci"]["low"], ma - mb - margin, places=9)
        self.assertAlmostEqual(fed["ci"]["high"], ma - mb + margin, places=9)


class TDistTests(unittest.TestCase):
    def test_symmetric_cdf(self):
        self.assertAlmostEqual(t_cdf(0.0, 10), 0.5, places=12)
        self.assertAlmostEqual(t_cdf(1.5, 8) + t_cdf(-1.5, 8), 1.0, 12)

    def test_known_table_values(self):
        # 教材 t 表：df=10 双侧 5% 临界值约 2.228；df=20 约 2.086
        self.assertAlmostEqual(t_ppf(0.975, 10), 2.2281, places=3)
        self.assertAlmostEqual(t_ppf(0.975, 20), 2.0860, places=3)
        # df→大 时趋近正态 1.96
        self.assertAlmostEqual(t_ppf(0.975, 100000), 1.96, places=2)

    def test_pvalue_and_ppf_inverse(self):
        for df in (3, 8, 30):
            q = t_ppf(0.975, df)
            self.assertAlmostEqual(two_sided_pvalue(q, df), 0.05, places=9)

    def test_scipy_crosscheck_if_available(self):
        try:
            from scipy import stats
        except ImportError:
            self.skipTest("无 scipy，使用教材值验证")
        for df in (2, 5, 13, 60):
            for t in (-2.3, -0.4, 0.0, 0.7, 2.8):
                self.assertAlmostEqual(t_cdf(t, df),
                                       float(stats.t.cdf(t, df)), places=10)


if __name__ == "__main__":
    unittest.main()
