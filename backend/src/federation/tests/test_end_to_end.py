"""端到端测试：中心 HTTP + 站点端发件箱（离线补交、重试幂等、封算）。"""
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request

from src.federation.app import serve_in_thread
from src.federation.station import StationClient, StationStore


def post(base, path, body):
    req = urllib.request.Request(
        base + path, data=json.dumps(body, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=10) as r:
        return json.loads(r.read().decode())


ROSTER = [
    {"station_id": "YC-NORTH", "station_name": "北车间", "sample_version": "sv-a"},
    {"station_id": "YC-SOUTH", "station_name": "南车间", "sample_version": "sv-b"},
]


def fragments(tag, per):
    import random
    rng = random.Random({"N": 101, "S": 202}[tag])
    rows = []
    for arm, mu, sigma in [("A", 19.5, 1.4), ("B", 18.2, 2.1)]:
        for i in range(per):
            rows.append({"run": f"{tag}-{arm}{i:03}", "arm": arm,
                         "shift": "早班", "seconds": round(rng.gauss(mu, sigma), 3)})
    return rows


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.httpd, self.base = serve_in_thread(
            os.path.join(self.tmp.name, "center.db"))
        self.trial = post(self.base, "/api/federation/trials", {
            "name": "端到端联合试验", "confidence": 0.95, "roster": ROSTER})[1]
        self.tid = self.trial["id"]
        self.clients = []
        for sid, sname, ver, tag in [
                ("YC-NORTH", "北车间", "sv-a", "N"),
                ("YC-SOUTH", "南车间", "sv-b", "S")]:
            store = StationStore(os.path.join(self.tmp.name, f"{sid}.db"))
            store.add_fragments(self.tid, fragments(tag, 30))
            self.clients.append(StationClient(sid, sname, ver, store, self.base))

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.httpd.store.close()
        for c in self.clients:
            c.store.close()
        self.tmp.cleanup()

    def test_full_flow_dashboard_has_no_raw_values(self):
        # 站点本地准备：报文只有 n/mean/ss
        for c in self.clients:
            payload, digest = c.prepare(self.tid)
            self.assertEqual(set(payload["groups"]["A"]), {"n", "mean", "ss"})
            self.assertEqual(len(c.store.outbox_view()), 1)
        # 联网发送
        for c in self.clients:
            results = c.flush()
            self.assertEqual([r["status"] for r in results], ["accepted"])
        view = get(self.base, f"/api/federation/trials/{self.tid}")
        self.assertEqual(view["missing"], [])
        self.assertEqual(view["received_count"], 2)
        # 页面与 API 的任何字段都不含原始节拍（本站片段如 N-A001 不得出现）
        raw = json.dumps(view, ensure_ascii=False)
        self.assertNotIn("N-A001", raw)
        self.assertNotIn("S-B010", raw)
        # 封算
        _, sealed = post(self.base, f"/api/federation/trials/{self.tid}/seal", {})
        self.assertIn("result", sealed)
        # 仪表盘 HTML 同样不含原值
        with urllib.request.urlopen(self.base + "/", timeout=10) as r:
            html = r.read().decode()
        self.assertNotIn("N-A001", html)
        self.assertIn("联合试验", html)

    def test_offline_retry_is_idempotent(self):
        c = self.clients[0]
        c.prepare(self.tid)
        # 指向不存在的中心：离线，发件箱保留 pending
        c.base_url = "http://127.0.0.1:9"  # discard 端口，连接被拒
        offline = c.flush()
        self.assertTrue(offline[0]["offline"])
        self.assertEqual(c.store.outbox_view()[0]["state"], "pending")
        self.assertGreaterEqual(c.store.outbox_view()[0]["attempts"], 1)
        # 恢复联网，补交成功；再次 flush 无重复
        c.base_url = self.base
        self.assertEqual(c.flush()[0]["status"], "accepted")
        second = c.flush()
        self.assertEqual(second, [])  # 发件箱已空
        view = get(self.base, f"/api/federation/trials/{self.tid}")
        self.assertEqual(view["received_count"], 1)
        # 直接重发同一报文，中心判 duplicate，不重复合计
        status, resp = post(self.base, "/api/federation/submissions", {
            "trial_id": self.tid, "station_id": c.station_id,
            "sample_version": c.sample_version,
            "payload": c.build_payload(self.tid)})
        self.assertEqual(resp["status"], "duplicate")

    def test_conflict_and_version_rejection_over_http(self):
        c = self.clients[0]
        payload, _ = c.prepare(self.tid)
        # 错误版本（报文本身仍需是合法汇总，否则会先被统计校验拦下）
        status, resp = post(self.base, "/api/federation/submissions", {
            "trial_id": self.tid, "station_id": c.station_id,
            "sample_version": "WRONG", "payload": payload})
        self.assertEqual(status, 200)
        self.assertEqual(resp["status"], "rejected")
        # 正确版本接收
        self.assertEqual(post(self.base, "/api/federation/submissions", {
            "trial_id": self.tid, "station_id": c.station_id,
            "sample_version": c.sample_version, "payload": payload})[1]["status"],
            "accepted")
        # 同站同版异值
        tampered = json.loads(json.dumps(payload))
        tampered["groups"]["A"]["mean"] += 1.0
        resp = post(self.base, "/api/federation/submissions", {
            "trial_id": self.tid, "station_id": c.station_id,
            "sample_version": c.sample_version, "payload": tampered})[1]
        self.assertEqual(resp["status"], "rejected")
        view = get(self.base, f"/api/federation/trials/{self.tid}")
        self.assertEqual(len(view["conflicts"]), 2)
        # 缺站时封算返回 422
        code, resp = post(self.base, f"/api/federation/trials/{self.tid}/seal", {})
        self.assertEqual(code, 422)
        self.assertIn("未到齐", resp["detail"])

    def test_health(self):
        self.assertEqual(get(self.base, "/health")["status"], "healthy")


if __name__ == "__main__":
    unittest.main()
