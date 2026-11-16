"""充分统计量与合并样本统计。

站点本地从原始片段计算 (n, mean, ss)，中心只用这些汇总值合并，
全程不接触任何原始节拍。
"""
from __future__ import annotations

import math
from numbers import Real

ARMS = ("A", "B")


def local_summary(values):
    """从站点本地原始片段计算充分统计量 (n, mean, ss)。

    ss 为平方离差和 Σ(x−mean)²，使用 Welford 单次扫描，数值稳定。
    原始 values 不会出现在返回值中。
    """
    n = 0
    mean = 0.0
    m2 = 0.0  # Welford 的 M2，即平方离差和
    for v in values:
        if isinstance(v, bool) or not isinstance(v, Real):
            raise ValueError("节拍必须为数值")
        x = float(v)
        if not math.isfinite(x) or x <= 0:
            raise ValueError("节拍必须为正有限数")
        n += 1
        delta = x - mean
        mean += delta / n
        m2 += delta * (x - mean)
    if n == 0:
        raise ValueError("样本为空")
    return {"n": n, "mean": mean, "ss": m2}


def combine_summaries(parts):
    """合并若干站点同一组的 (n, mean, ss) 为合并样本统计量。

    使用平行（Chan/Golub）合并公式，与直接拼接原始样本后再算完全等价，
    但不需要任何站点交出原值。返回合并后的 n、mean、ss、方差、标准差。
    """
    total_n = 0
    total_mean = 0.0
    total_ss = 0.0
    for p in parts:
        n = int(p["n"])
        if n <= 0:
            raise ValueError("件数必须为正整数")
        mean = float(p["mean"])
        ss = float(p["ss"])
        if not math.isfinite(mean) or not math.isfinite(ss) or ss < 0:
            raise ValueError("均值或平方离差和不合法")
        if total_n == 0:
            total_n, total_mean, total_ss = n, mean, ss
            continue
        nt = total_n + n
        delta = mean - total_mean
        # 组间离差并入，组内离差直接相加
        total_ss = total_ss + ss + (delta * delta) * (total_n * n) / nt
        total_mean = total_mean + delta * n / nt
        total_n = nt
    if total_n < 2:
        # 单观测无法估计方差
        return {"n": total_n, "mean": total_mean, "ss": total_ss,
                "variance": None, "sd": None}
    variance = total_ss / (total_n - 1)
    return {"n": total_n, "mean": total_mean, "ss": total_ss,
            "variance": variance, "sd": math.sqrt(variance)}


def validate_payload(payload):
    """校验站点上报载荷的统计结构（不接触原始片段）。"""
    if not isinstance(payload, dict):
        raise ValueError("上报内容必须是对象")
    groups = payload.get("groups")
    if not isinstance(groups, dict) or set(groups) != set(ARMS):
        raise ValueError("必须同时包含 A、B 两组的汇总")
    cleaned = {}
    for arm in ARMS:
        g = groups[arm]
        if not isinstance(g, dict):
            raise ValueError(f"{arm} 组汇总格式错误")
        n = g.get("n")
        if not isinstance(n, int) or isinstance(n, bool) or n <= 0:
            raise ValueError(f"{arm} 组件数必须为正整数")
        mean = float(g.get("mean"))
        ss = float(g.get("ss"))
        if not math.isfinite(mean) or not math.isfinite(ss) or ss < 0:
            raise ValueError(f"{arm} 组均值或平方离差和不合法")
        cleaned[arm] = {"n": n, "mean": mean, "ss": ss}
    return cleaned
