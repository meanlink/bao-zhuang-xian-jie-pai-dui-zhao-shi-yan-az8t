"""联合试验的 SQLite 持久化（仅标准库）。

存储内容只有试验名册与各站的汇总统计量（n/mean/ss），任何表都不保存
原始节拍。所有状态变更走短事务，配合 BEGIN IMMEDIATE 实现接收与封算
并发下的串行化，保证“结果和所用汇总一致”，中断后重启可凭库内状态恢复。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS trials (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    confidence   REAL NOT NULL,
    roster       TEXT NOT NULL,           -- JSON: [{station_id, station_name, sample_version}]
    status       TEXT NOT NULL DEFAULT 'collecting',  -- collecting | sealed
    created_at   REAL NOT NULL,
    sealed_at    REAL,
    result       TEXT,                    -- 封算结果快照 JSON
    used_summary TEXT                     -- 封算所用汇总 JSON（与 result 同事务写入）
);
CREATE TABLE IF NOT EXISTS submissions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trial_id        INTEGER NOT NULL REFERENCES trials(id),
    station_id      TEXT NOT NULL,
    sample_version  TEXT NOT NULL,
    payload         TEXT NOT NULL,        -- 仅含 n/mean/ss 的汇总 JSON
    content_hash    TEXT NOT NULL,        -- 规范化载荷哈希，用于幂等/冲突判定
    received_at     REAL NOT NULL,
    UNIQUE(trial_id, station_id)          -- 每站每试验只保留一条有效汇总
);
CREATE TABLE IF NOT EXISTS conflicts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trial_id        INTEGER NOT NULL REFERENCES trials(id),
    station_id      TEXT NOT NULL,
    sample_version  TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    reason          TEXT NOT NULL,
    payload         TEXT NOT NULL,
    occurred_at     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS deliveries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    trial_id     INTEGER NOT NULL REFERENCES trials(id),
    station_id   TEXT NOT NULL,
    status       TEXT NOT NULL,           -- accepted | duplicate | rejected
    detail       TEXT NOT NULL,
    occurred_at  REAL NOT NULL
);
"""


class Store:
    """线程安全的 SQLite 封装：单连接 + 串行锁 + WAL。"""

    def __init__(self, path):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()

    # ---------- 试验 ----------
    def create_trial(self, name, confidence, roster, created_at=None):
        """建立联合试验，固定站点名单与各站样本版本（之后不可变）。"""
        created_at = time.time() if created_at is None else created_at
        fixed = [{"station_id": str(s["station_id"]),
                  "station_name": str(s["station_name"]),
                  "sample_version": str(s["sample_version"])} for s in roster]
        ids = [s["station_id"] for s in fixed]
        if len(ids) != len(set(ids)):
            raise ValueError("站点名单存在重复 station_id")
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO trials(name, confidence, roster, status, created_at) "
                "VALUES (?,?,?,'collecting',?)",
                (name, confidence, json.dumps(fixed, ensure_ascii=False), created_at))
            return cur.lastrowid

    def get_trial(self, trial_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM trials WHERE id=?", (trial_id,)).fetchone()
            return self._trial_row(row) if row else None

    def list_trials(self):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM trials ORDER BY id DESC").fetchall()
            return [self._trial_row(r) for r in rows]

    def _trial_row(self, row):
        d = dict(row)
        d["roster"] = json.loads(d["roster"])
        d["result"] = json.loads(d["result"]) if d["result"] else None
        d["used_summary"] = json.loads(d["used_summary"]) if d["used_summary"] else None
        return d

    # ---------- 接收上报 ----------
    def accept_submission(self, trial_id, station_id, sample_version, payload,
                          content_hash, now=None):
        """幂等接收一次上报。

        返回 (status, detail)：
          accepted  首次计入
          duplicate 同站同版同值，重试不重复合计
          rejected  同站同版异值 / 版本不符 / 试验已封算 / 非名册站
        全部在一个 BEGIN IMMEDIATE 事务内判定，接收与封算互斥。
        """
        now = time.time() if now is None else now
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                trial = self._conn.execute(
                    "SELECT * FROM trials WHERE id=?", (trial_id,)).fetchone()
                if not trial:
                    self._conn.rollback()
                    return "rejected", "联合试验不存在"
                if trial["status"] == "sealed":
                    # 旧试验封算后迟到的数据绝不混入；冲突留痕单独提交以保留记录
                    self._log_conflict(trial_id, station_id, sample_version,
                                       content_hash, "试验已封算，迟到数据拒收",
                                       payload, now)
                    self._log_delivery(trial_id, station_id, "rejected",
                                       "试验已封算，迟到数据不再接收", now)
                    self._conn.commit()
                    return "rejected", "试验已封算，迟到数据不再接收"
                roster = {s["station_id"]: str(s["station_id"]) for s in
                          json.loads(trial["roster"])}
                versions = {s["station_id"]: s["sample_version"] for s in
                            json.loads(trial["roster"])}
                if station_id not in roster:
                    self._log_conflict(trial_id, station_id, sample_version,
                                       content_hash, "站点不在固定名单内",
                                       payload, now)
                    self._log_delivery(trial_id, station_id, "rejected",
                                       "站点不在本试验固定名单内", now)
                    self._conn.commit()
                    return "rejected", "站点不在本试验固定名单内"
                expected_version = versions[station_id]
                if sample_version != expected_version:
                    # 版本不符直接拒收并留痕，旧试验/错版本数据不会混入
                    self._log_conflict(trial_id, station_id, sample_version,
                                       content_hash,
                                       f"样本版本不符，要求 {expected_version}",
                                       payload, now)
                    self._log_delivery(trial_id, station_id, "rejected",
                                       f"样本版本不符，固定版本为 {expected_version}",
                                       now)
                    self._conn.commit()
                    return "rejected", (f"样本版本不符：本站点固定版本为 "
                                        f"{expected_version}")
                existing = self._conn.execute(
                    "SELECT * FROM submissions WHERE trial_id=? AND station_id=?",
                    (trial_id, station_id)).fetchone()
                if existing:
                    if existing["content_hash"] == content_hash:
                        # 离线补交/重试：同站同版同值，幂等返回，不重复合计
                        self._log_delivery(trial_id, station_id, "duplicate",
                                           "重复上报已忽略，未重复合计", now)
                        self._conn.commit()
                        return "duplicate", "与已接收汇总一致，按重试忽略"
                    # 同站同版异值：冲突拒收，保留原有效汇总
                    self._log_conflict(trial_id, station_id, sample_version,
                                       content_hash, "同站同版出现异值，拒收",
                                       payload, now)
                    self._log_delivery(trial_id, station_id, "rejected",
                                       "同站同版异值冲突，保留原汇总", now)
                    self._conn.commit()
                    return "rejected", "同站同版汇总与已接收数据不一致，已拒收"
                self._conn.execute(
                    "INSERT INTO submissions(trial_id, station_id, sample_version,"
                    " payload, content_hash, received_at) VALUES (?,?,?,?,?,?)",
                    (trial_id, station_id, sample_version,
                     json.dumps(payload, ensure_ascii=False, sort_keys=True),
                     content_hash, now))
                self._log_delivery(trial_id, station_id, "accepted",
                                   "汇总已计入", now)
                self._conn.commit()
                return "accepted", "汇总已接收并计入"
            except Exception:
                self._conn.rollback()
                raise

    def list_submissions(self, trial_id):
        with self._lock:
            rows = self._conn.execute(
                "SELECT station_id, sample_version, payload, content_hash, received_at "
                "FROM submissions WHERE trial_id=? ORDER BY station_id",
                (trial_id,)).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["payload"] = json.loads(d["payload"])
                out.append(d)
            return out

    def list_conflicts(self, trial_id):
        with self._lock:
            rows = self._conn.execute(
                "SELECT station_id, sample_version, reason, occurred_at "
                "FROM conflicts WHERE trial_id=? ORDER BY id DESC",
                (trial_id,)).fetchall()
            return [dict(r) for r in rows]

    def list_deliveries(self, trial_id):
        with self._lock:
            rows = self._conn.execute(
                "SELECT station_id, status, detail, occurred_at "
                "FROM deliveries WHERE trial_id=? ORDER BY id DESC",
                (trial_id,)).fetchall()
            return [dict(r) for r in rows]

    # ---------- 封算 ----------
    def ready_to_seal(self, trial_id):
        """全部站点到齐才可封算。"""
        with self._lock:
            trial = self.get_trial(trial_id)
            if not trial:
                return False, "试验不存在", [], []
            received = {s["station_id"] for s in self.list_submissions(trial_id)}
            required = {s["station_id"] for s in trial["roster"]}
            missing = sorted(required - received)
            return (not missing), trial, missing, sorted(received)

    def seal_if_complete(self, trial_id, compute):
        """所有站点到齐时封算。

        compute(merged_payloads, confidence) 由调用方注入统计逻辑。
        在同一 BEGIN IMMEDIATE 事务内复核齐全、读取汇总、写入结果，
        因此与并发接收互斥：封算瞬间看到的汇总就是结果所用汇总。
        返回 (sealed: bool, info)。
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                trial = self._conn.execute(
                    "SELECT * FROM trials WHERE id=?", (trial_id,)).fetchone()
                if not trial:
                    self._conn.rollback()
                    return False, {"reason": "试验不存在"}
                if trial["status"] == "sealed":
                    # 中断恢复/重复封算：返回既有快照，绝不重算覆盖
                    self._conn.rollback()
                    return True, {"already_sealed": True,
                                  "result": json.loads(trial["result"]),
                                  "used_summary": json.loads(trial["used_summary"])}
                roster = json.loads(trial["roster"])
                rows = self._conn.execute(
                    "SELECT station_id, payload FROM submissions "
                    "WHERE trial_id=? ORDER BY station_id", (trial_id,)).fetchall()
                by_station = {r["station_id"]: json.loads(r["payload"]) for r in rows}
                missing = sorted({s["station_id"] for s in roster} - set(by_station))
                if missing:
                    self._conn.rollback()
                    return False, {"reason": "尚有站点未到齐", "missing": missing}
                payloads = [by_station[s["station_id"]] for s in roster]
                confidence = trial["confidence"]
                result = compute(payloads, confidence)
                used = {s["station_id"]: by_station[s["station_id"]] for s in roster}
                sealed_at = time.time()
                self._conn.execute(
                    "UPDATE trials SET status='sealed', sealed_at=?, result=?,"
                    " used_summary=? WHERE id=?",
                    (sealed_at, json.dumps(result, ensure_ascii=False),
                     json.dumps(used, ensure_ascii=False, sort_keys=True), trial_id))
                self._conn.commit()
                return True, {"already_sealed": False, "result": result,
                              "used_summary": used, "sealed_at": sealed_at}
            except Exception:
                self._conn.rollback()
                raise

    # ---------- 内部 ----------
    def _log_conflict(self, trial_id, station_id, version, content_hash,
                      reason, payload, when):
        self._conn.execute(
            "INSERT INTO conflicts(trial_id, station_id, sample_version,"
            " content_hash, reason, payload, occurred_at) VALUES (?,?,?,?,?,?,?)",
            (trial_id, station_id, version, content_hash, reason,
             json.dumps(payload, ensure_ascii=False, sort_keys=True), when))

    def _log_delivery(self, trial_id, station_id, status, detail, when):
        self._conn.execute(
            "INSERT INTO deliveries(trial_id, station_id, status, detail, occurred_at)"
            " VALUES (?,?,?,?,?)",
            (trial_id, station_id, status, detail, when))
