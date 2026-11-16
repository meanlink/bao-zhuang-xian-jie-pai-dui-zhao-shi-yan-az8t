"""纯标准库的 Student t 分布：CDF、生存函数、双侧 p 值、分位数。

不使用 scipy。t 分布 CDF 由正则化不完全贝塔函数 I_x(a,b) 给出：
    F(t; ν) = 1 − 0.5 * I_{ν/(ν+t²)}(ν/2, 1/2)        (t>0)
分位数用对 CDF 的二分求逆。federation 测试会与 scipy 的参考值比对。
"""
from __future__ import annotations

import math


def _betacf(a, b, x, max_iter=200, eps=3e-14):
    """Lentz 连分式（Numerical Recipes betacf），用于不完全贝塔函数。"""
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < 1e-30:
        d = 1e-30
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def betainc_regularized(x, a, b):
    """正则化不完全贝塔函数 I_x(a, b)，x∈[0,1]，a,b>0。"""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(lbeta + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    # 对称关系，保证连分式在收敛更快的一侧求值
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def t_cdf(t, df):
    """Student t 累积分布函数。"""
    if df <= 0:
        raise ValueError("自由度必须为正")
    x = df / (df + t * t)
    ib = betainc_regularized(x, df / 2.0, 0.5)
    if t >= 0:
        return 1.0 - 0.5 * ib
    return 0.5 * ib


def t_sf_abs(t_abs, df):
    """|t| 处的上侧概率 P(T ≥ t_abs)。"""
    if t_abs < 0:
        t_abs = abs(t_abs)
    x = df / (df + t_abs * t_abs)
    return 0.5 * betainc_regularized(x, df / 2.0, 0.5)


def two_sided_pvalue(t, df):
    """双侧 p 值 2·P(T ≥ |t|)。"""
    p = 2.0 * t_sf_abs(abs(t), df)
    return min(1.0, max(0.0, p))


def t_ppf(p, df):
    """t 分位数：返回使 F(t)=p 的 t，p∈(0,1)，二分求逆。"""
    if not 0.0 < p < 1.0:
        raise ValueError("分位概率必须在 (0,1) 内")
    if df <= 0:
        raise ValueError("自由度必须为正")
    lo, hi = -1.0, 1.0
    # 按 CDF 扩展括号，避免硬编码界值
    while t_cdf(hi, df) < p:
        lo, hi = hi, hi * 2
    while t_cdf(lo, df) > p:
        hi, lo = lo, lo * 2
    for _ in range(100):
        mid = (lo + hi) / 2.0
        if t_cdf(mid, df) < p:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-12:
            break
    return (lo + hi) / 2.0
