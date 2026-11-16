"""在合并样本汇总上执行 Welch 比较。

口径与 src/services/business.py 完全相同（改善量 A−B、Welch–Satterthwaite
自由度、双侧 t、t 分布区间、Cohen d 用合并标准差），区别仅在于输入是
各站充分统计量合并后的结果，而非原始片段。
"""
from __future__ import annotations

import math

from .statistics import ARMS, combine_summaries
from .tdist import t_ppf, two_sided_pvalue

METHOD = ("联邦合并口径：各站仅上报件数 n、均值与平方离差和 ss；"
          "中心按平行公式合并 n/均值/ss 后，计算改善量=均值(A)−均值(B)，"
          "SE=√(sA²/nA+sB²/nB)，Welch–Satterthwaite 自由度，"
          "双侧 t 检验与 t 分布置信区间，Cohen d 用合并标准差。"
          "全程不传输、不存储任何原始节拍。")


def merge_groups(station_payloads):
    """把多站上报合并为 A/B 两组的合并样本统计。"""
    by_arm = {arm: [] for arm in ARMS}
    for payload in station_payloads:
        for arm in ARMS:
            by_arm[arm].append(payload["groups"][arm])
    return {arm: combine_summaries(by_arm[arm]) for arm in ARMS}


def welch_from_merged(merged, confidence=0.95):
    """对合并后的 A/B 汇总执行 Welch 检验。"""
    if confidence not in (0.9, 0.95, 0.99):
        raise ValueError("置信度只接受 0.90/0.95/0.99")
    a, b = merged["A"], merged["B"]
    na, nb = a["n"], b["n"]
    ma, mb = a["mean"], b["mean"]
    va, vb = a["variance"], b["variance"]
    if va is None or vb is None:
        raise ValueError("合并后每组至少需要 2 个片段才能估计方差")
    se2 = va / na + vb / nb
    if se2 <= 1e-15:
        raise ValueError("两组均无变异，Welch 检验无法估计误差")
    df = se2 * se2 / ((va / na) ** 2 / (na - 1) + (vb / nb) ** 2 / (nb - 1))
    diff = ma - mb
    se = math.sqrt(se2)
    statistic = diff / se
    p = two_sided_pvalue(statistic, df)
    margin = t_ppf((1 + confidence) / 2, df) * se
    low, high = diff - margin, diff + margin
    pooled = math.sqrt(((na - 1) * va + (nb - 1) * vb) / (na + nb - 2))
    effect = diff / pooled
    rows = [
        {"arm": arm, "n": merged[arm]["n"],
         "mean": round(merged[arm]["mean"], 4),
         "sd": round(merged[arm]["sd"], 4)}
        for arm in ARMS
    ]
    return {
        "summary": (f"B 相对 A 平均节拍缩短 {diff:.3f} 秒；"
                    f"{confidence * 100:g}% 区间为 [{low:.3f}, {high:.3f}] 秒。"),
        "metrics": [
            {"label": "改善量 A−B", "value": round(diff, 5), "unit": "秒"},
            {"label": "双侧 p 值", "value": float(f"{p:.6g}"), "unit": ""},
            {"label": "Cohen d", "value": round(effect, 5), "unit": ""},
            {"label": "Welch 自由度", "value": round(df, 3), "unit": ""},
        ],
        "table": {
            "columns": [
                {"key": "arm", "label": "实验组"},
                {"key": "n", "label": "片段数（各站合并）"},
                {"key": "mean", "label": "均值/秒"},
                {"key": "sd", "label": "样本标准差/秒"},
            ],
            "rows": rows,
        },
        "chart": {
            "type": "bar",
            "labels": ["A 原动作", "B 改动作"],
            "series": [{"name": "平均单件节拍/秒",
                        "values": [round(ma, 4), round(mb, 4)]}],
        },
        "findings": [
            (f"区间下界 {low:.4f} 秒，上界 {high:.4f} 秒；"
             f"{'区间未跨 0' if low > 0 or high < 0 else '区间跨 0'}。"),
            f"t={statistic:.4f}，自由度={df:.3f}；由各站充分统计量合并计算。",
            "仅在分组随机、片段独立的前提下支持实验推断；中心从未接收原始节拍。",
        ],
        "method": METHOD,
        "warnings": ["联邦合并不改变 Welch 口径；若各站采样机制不同，需另行评估异质性。"],
        "statistic_t": statistic,
        "df": df,
        "p_value": p,
        "ci": {"confidence": confidence, "low": low, "high": high},
        "cohen_d": effect,
    }


def analyze_payloads(station_payloads, confidence=0.95):
    """便捷入口：多站上报 → 合并 → Welch 结果。"""
    return welch_from_merged(merge_groups(station_payloads), confidence)
