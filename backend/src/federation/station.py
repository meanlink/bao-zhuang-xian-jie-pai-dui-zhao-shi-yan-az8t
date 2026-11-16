"""站点端：原始片段留在站点，只产出并上报充分统计量。

- 原始节拍保存在站点本地库（station store），永不进入上报报文；
- 报文只含 trial_id、station_id、sample_version 与 A/B 两组的 n/mean/ss；
- 发件箱（outbox）支持离线补交：网络不可用时排队，恢复后重试；
- 重试携带同一 content_hash，中心据此幂等去重，绝不重复合计。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request

from .statistics import ARMS, local_summary


class StationStore:
    """站点本地库：保存原始片段（不出站点）与发件箱。"""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS fragments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trial_id INTEGER NOT NULL,
        arm TEXT NOT NULL,
        run TEXT NOT NULL,
        seconds REAL NOT NULL,
        UNIQUE(trial_id, run)
    );
    CREATE TABLE IF NOT EXISTS outbox (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trial_id INTEGER NOT NULL,
        station_id TEXT NOT NULL,
        sample_version TEXT NOT NULL,
        payload TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending',  -- pending | sent | failed
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        updated_at REAL NOT NULL
    );
    """

    def __init__(self, path):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(self.SCHEMA)
        self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()

    def add_fragments(self, trial_id, rows):
        """写入站点本地原始片段（run 唯一，与普通比较口径一致）。"""
        with self._lock, self._conn:
            seen = set()
            for r in rows:
                run, arm, seconds = str(r["run"]), str(r["arm"]), float(r["seconds"])
                if arm not in ARMS:
                    raise ValueError("组别必须为 A/B")
                if not (seconds > 0) or run in seen:
                    raise ValueError("片段号须唯一且节拍为正数")
                seen.add(run)
                self._conn.execute(
                    "INSERT OR IGNORE INTO fragments(trial_id, arm, run, seconds)"
                    " VALUES (?,?,?,?)", (trial_id, arm, run, seconds))

    def load_fragments(self, trial_id):
        with self._lock:
            rows = self._conn.execute(
                "SELECT arm, run, seconds FROM fragments WHERE trial_id=?",
                (trial_id,)).fetchall()
            return [dict(r) for r in rows]

    def queue_outbox(self, trial_id, station_id, sample_version, payload,
                     content_hash):
        with self._lock, self._conn:
            existing = self._conn.execute(
                "SELECT id FROM outbox WHERE trial_id=? AND station_id=?",
                (trial_id, station_id)).fetchone()
            if existing:
                return existing["id"]
            cur = self._conn.execute(
                "INSERT INTO outbox(trial_id, station_id, sample_version, payload,"
                " content_hash, state, updated_at) VALUES (?,?,?,?,?,'pending',?)",
                (trial_id, station_id, sample_version,
                 json.dumps(payload, ensure_ascii=False, sort_keys=True),
                 content_hash, time.time()))
            return cur.lastrowid

    def pending_outbox(self):
        with self._lock:
            return [dict(r) for r in self._conn.execute(
                "SELECT * FROM outbox WHERE state!='sent' ORDER BY id").fetchall()]

    def mark_sent(self, outbox_id):
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE outbox SET state='sent', updated_at=? WHERE id=?",
                (time.time(), outbox_id))

    def mark_attempt(self, outbox_id, error):
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE outbox SET attempts=attempts+1, last_error=?, updated_at=?"
                " WHERE id=?", (error, time.time(), outbox_id))

    def mark_failed(self, outbox_id, error):
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE outbox SET state='failed', attempts=attempts+1,"
                " last_error=?, updated_at=? WHERE id=?",
                (error, time.time(), outbox_id))

    def outbox_view(self):
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, trial_id, station_id, sample_version, state, attempts,"
                " last_error, updated_at FROM outbox ORDER BY id").fetchall()
            return [dict(r) for r in rows]


class StationClient:
    """一个站点的本地代理：本地算汇总，经发件箱上报。"""

    def __init__(self, station_id, station_name, sample_version, store,
                 center_base_url):
        self.station_id = station_id
        self.station_name = station_name
        self.sample_version = sample_version
        self.store = store
        self.base_url = center_base_url.rstrip("/")

    def build_payload(self, trial_id):
        """在站点本地把原始片段压缩成 A/B 的 n/mean/ss（原值不出站点）。"""
        rows = self.store.load_fragments(trial_id)
        grouped = {arm: [] for arm in ARMS}
        for r in rows:
            grouped[r["arm"]].append(r["seconds"])
        groups = {arm: local_summary(grouped[arm]) for arm in ARMS}
        return {"groups": groups}

    def prepare(self, trial_id):
        """本地计算并入队发件箱，返回待发报文（不含原始片段）。"""
        payload = self.build_payload(trial_id)
        from .service import canonical_hash
        digest = canonical_hash(self.station_id, self.sample_version, payload)
        self.store.queue_outbox(trial_id, self.station_id,
                                self.sample_version, payload, digest)
        return payload, digest

    def _post(self, path, body):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + path, data=data, method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def flush(self, max_items=None):
        """打开发件箱重试上报。成功标记 sent；网络错误保留 pending 待补交；
        中心明确拒收（冲突/版本不符/已封算）标记 failed，不再重试。"""
        results = []
        for item in self.store.pending_outbox():
            if max_items is not None and len(results) >= max_items:
                break
            body = {"trial_id": item["trial_id"],
                    "station_id": item["station_id"],
                    "sample_version": item["sample_version"],
                    "payload": json.loads(item["payload"])}
            try:
                status, resp = self._post("/api/federation/submissions", body)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                # 离线：留在发件箱，稍后补交，重试不产生重复合计
                self.store.mark_attempt(item["id"], str(exc))
                results.append({"outbox_id": item["id"], "offline": True,
                                "error": str(exc)})
                continue
            if status == 200 and resp.get("status") in ("accepted", "duplicate"):
                self.store.mark_sent(item["id"])
                results.append({"outbox_id": item["id"], **resp})
            else:
                # 中心业务拒收（同站同版异值、版本不符、已封算等）
                detail = resp.get("detail", "中心拒收")
                self.store.mark_failed(item["id"], detail)
                results.append({"outbox_id": item["id"], "offline": False,
                                "rejected": True, "detail": detail})
        return results

    def local_view(self, trial_id):
        """站点本地视图：可看本地原始片段统计；上报内容仅汇总。"""
        rows = self.store.load_fragments(trial_id)
        grouped = {arm: [r["seconds"] for r in rows if r["arm"] == arm]
                   for arm in ARMS}
        return {
            "station_id": self.station_id,
            "station_name": self.station_name,
            "sample_version": self.sample_version,
            "local_fragment_count": len(rows),
            "local_means": {arm: (sum(grouped[arm]) / len(grouped[arm])
                                  if grouped[arm] else None) for arm in ARMS},
            "outbox": self.store.outbox_view(),
            "note": "原始片段仅保存在本站点本地库，上报报文只含 n/mean/ss。",
        }
