"""联合试验服务层：把 HTTP 无关的业务规则集中在此。

规则对应需求：
- 建立联合试验时固定站点名单与各站样本版本；
- 站点只交换 n/mean/ss，原始片段留在站点；
- 离线补交/重试幂等（同站同版同值不重复合计）；
- 同站同版异值拒收；旧试验迟到数据不混入新试验；
- 全部站点到齐才可封算；封算与接收并发时结果与所用汇总一致；
- 中断后凭持久化恢复原进度。
"""
from __future__ import annotations

import hashlib
import json

from .statistics import ARMS, validate_payload
from .welch import analyze_payloads


def canonical_hash(station_id, sample_version, payload):
    """对规范化后的汇总内容计算哈希（仅汇总，绝无原始片段）。"""
    body = json.dumps({"station_id": station_id,
                       "sample_version": sample_version,
                       "groups": payload["groups"]},
                      ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class FederationError(ValueError):
    """可向调用方展示的业务校验错误。"""


class FederationService:
    def __init__(self, store):
        self.store = store

    # ---------- 建试验 ----------
    def create_trial(self, name, confidence, roster):
        name = str(name or "").strip()
        if not name:
            raise FederationError("试验名称不能为空")
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            raise FederationError("置信度只接受 0.90/0.95/0.99")
        if confidence not in (0.9, 0.95, 0.99):
            raise FederationError("置信度只接受 0.90/0.95/0.99")
        if not isinstance(roster, list) or len(roster) < 2:
            raise FederationError("联合试验至少需要两个站点")
        clean = []
        for s in roster:
            if not isinstance(s, dict):
                raise FederationError("站点条目格式错误")
            sid = str(s.get("station_id", "")).strip()
            sname = str(s.get("station_name", sid)).strip() or sid
            version = str(s.get("sample_version", "")).strip()
            if not sid or not version:
                raise FederationError("每个站点都需要 station_id 与 sample_version")
            clean.append({"station_id": sid, "station_name": sname,
                          "sample_version": version})
        try:
            trial_id = self.store.create_trial(name, confidence, clean)
        except ValueError as exc:
            raise FederationError(str(exc))
        return self.store.get_trial(trial_id)

    # ---------- 站点上报 ----------
    def submit(self, trial_id, station_id, sample_version, payload):
        try:
            groups = validate_payload(payload)  # 只含 n/mean/ss
        except (ValueError, TypeError) as exc:
            raise FederationError(str(exc))
        clean_payload = {"groups": groups}
        # 附带站点本地的件数自检（A/B 各自件数），中心不还原原值
        digest = canonical_hash(station_id, sample_version, clean_payload)
        status, detail = self.store.accept_submission(
            trial_id, str(station_id), str(sample_version), clean_payload, digest)
        return {"status": status, "detail": detail, "content_hash": digest}

    # ---------- 封算 ----------
    def seal(self, trial_id):
        ok, info = self.store.seal_if_complete(trial_id, analyze_payloads)
        if not ok:
            raise FederationError(info.get("reason", "当前不可封算") +
                                  (("：" + "、".join(info["missing"]))
                                   if info.get("missing") else ""))
        return info

    # ---------- 页面视图 ----------
    def trial_view(self, trial_id):
        trial = self.store.get_trial(trial_id)
        if not trial:
            raise FederationError("联合试验不存在")
        submissions = {s["station_id"]: s for s in
                       self.store.list_submissions(trial_id)}
        stations = []
        for s in trial["roster"]:
            sid = s["station_id"]
            received = sid in submissions
            stations.append({
                "station_id": sid,
                "station_name": s["station_name"],
                "expected_version": s["sample_version"],
                "received_version": submissions[sid]["sample_version"] if received else None,
                "status": "received" if received else "missing",
                "received_at": submissions[sid]["received_at"] if received else None,
                # 只回显件数与均值等汇总，绝不回传原始片段
                "totals": {arm: submissions[sid]["payload"]["groups"][arm]["n"]
                           for arm in ARMS} if received else None,
            })
        missing = [s["station_id"] for s in stations if s["status"] == "missing"]
        return {
            "id": trial["id"],
            "name": trial["name"],
            "confidence": trial["confidence"],
            "status": trial["status"],
            "created_at": trial["created_at"],
            "sealed_at": trial["sealed_at"],
            "stations": stations,
            "missing": missing,
            "received_count": len(stations) - len(missing),
            "station_count": len(stations),
            "conflicts": self.store.list_conflicts(trial_id),
            "deliveries": self.store.list_deliveries(trial_id),
            "result": trial["result"],
            "used_summary": trial["used_summary"],
        }

    def list_views(self):
        return [self.trial_view(t["id"]) for t in self.store.list_trials()]
