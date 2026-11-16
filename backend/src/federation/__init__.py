"""联合试验（联邦汇总）——纯标准库实现。

设计要点（与 BUSINESS.md 的 Welch 口径完全一致，但不依赖 numpy/scipy）：
- 原始节拍片段永不离开站点；站点只交换三个充分统计量：件数 n、均值 mean、
  平方离差和 ss（sum of squared deviations, 即 Σ(x-mean)²）。
- 中心把各站同组的 (n, mean, ss) 合并成合并样本的 n/mean/方差，再执行与
  普通比较相同的 Welch t 检验、t 分布区间与 Cohen d。
- t 分布的 CDF/PPF 在此用 math.lgamma 与正则化不完全贝塔函数实现，
  便于与 scipy.stats.t 的结果交叉验证（见 federation/tests）。
"""
