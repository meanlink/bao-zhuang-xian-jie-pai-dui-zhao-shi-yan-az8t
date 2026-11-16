"""持久化与业务规则测试：幂等、冲突拒收、版本/封算隔离、恢复、并发一致性。"""
import json
import os
import tempfile
import threading
import unittest

from src.federation.service import FederationError, FederationService
from src.federation.store import Store
from src.federation.welch import analyze_payloads


def payload(na, ma, ssa, nb, mb, ssb):
    return {"groups": {"A": {"n": na, "mean": ma, "ss": ssa},
                       "B": {"n": nb, "mean": mb, "ss": ssb}}}


ROSTER = [
    {"station_id": "S1", "station_name": "北车间", "sample_version": "v1"},
    {"station_id": "S2", "station_name": "南车间", "sample_version": "v2"},
    {"station_id": "S3", "station_name": "东车间", "sample_version": "v1"},
]


class ServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "center.db")
        self.svc = FederationService(Store(self.db))
        self.trial = self.svc.create_trial("联合试验", 0.95, ROSTER)
        self.tid = self.trial["id"]

    def tearDown(self):
        self.svc.store.close()
        self.tmp.cleanup()


class RosterAndSubmissionTests(ServiceTestBase):
    def test_roster_fixed_and_progress_view(self):
        view = self.svc.trial_view(self.tid)
        self.assertEqual(view["status"], "collecting")
        self.assertEqual(view["station_count"], 3)
        self.assertEqual(set(view["missing"]), {"S1", "S2", "S3"})
        self.assertEqual({s["expected_version"] for s in view["stations"]},
                         {"v1", "v2"})

    def test_duplicate_roster_rejected(self):
        with self.assertRaises(FederationError):
            self.svc.create_trial("x", 0.95, [
                {"station_id": "X", "sample_version": "v"},
                {"station_id": "X", "sample_version": "v"}])

    def test_unknown_station_rejected(self):
        r = self.svc.submit(self.tid, "GHOST", "v1", payload(5, 1.0, 4, 5, 2.0, 4))
        self.assertEqual(r["status"], "rejected")
        self.assertEqual(len(self.svc.trial_view(self.tid)["conflicts"]), 1)

    def test_wrong_version_rejected(self):
        r = self.svc.submit(self.tid, "S1", "WRONG", payload(5, 1.0, 4, 5, 2.0, 4))
        self.assertEqual(r["status"], "rejected")
        self.assertIn("版本", r["detail"])
        # 正确版本随后仍可正常接收
        ok = self.svc.submit(self.tid, "S1", "v1", payload(5, 1.0, 4, 5, 2.0, 4))
        self.assertEqual(ok["status"], "accepted")

    def test_idempotent_retry_not_double_counted(self):
        p = payload(6, 10.0, 12.0, 6, 9.0, 10.0)
        first = self.svc.submit(self.tid, "S1", "v1", p)
        again = self.svc.submit(self.tid, "S1", "v1", json.loads(json.dumps(p)))
        self.assertEqual(first["status"], "accepted")
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(first["content_hash"], again["content_hash"])
        subs = self.svc.store.list_submissions(self.tid)
        self.assertEqual(len([s for s in subs if s["station_id"] == "S1"]), 1)

    def test_same_station_same_version_different_value_rejected(self):
        self.svc.submit(self.tid, "S1", "v1", payload(6, 10.0, 12.0, 6, 9.0, 10.0))
        conflict = self.svc.submit(
            self.tid, "S1", "v1", payload(6, 10.5, 12.0, 6, 9.0, 10.0))
        self.assertEqual(conflict["status"], "rejected")
        self.assertIn("不一致", conflict["detail"])
        view = self.svc.trial_view(self.tid)
        self.assertEqual(len(view["conflicts"]), 1)
        # 原汇总保持不变，仍可被封算使用
        sub = [s for s in self.svc.store.list_submissions(self.tid)
               if s["station_id"] == "S1"][0]
        self.assertAlmostEqual(sub["payload"]["groups"]["A"]["mean"], 10.0)

    def test_invalid_payload_rejected(self):
        with self.assertRaises(FederationError):
            self.svc.submit(self.tid, "S1", "v1",
                            {"groups": {"A": {"n": 0, "mean": 1, "ss": 0}}})
        with self.assertRaises(FederationError):
            self.svc.submit(self.tid, "S1", "v1", "groups-bad")


class SealingTests(ServiceTestBase):
    def _fill(self, override=None):
        data = {
            "S1": (payload(20, 19.5, 40.0, 20, 18.1, 70.0), "v1"),
            "S2": (payload(18, 19.7, 36.0, 18, 18.4, 66.0), "v2"),
            "S3": (payload(22, 19.4, 44.0, 22, 18.0, 72.0), "v1"),
        }
        if override:
            data.update(override)
        for sid, (p, ver) in data.items():
            self.assertEqual(
                self.svc.submit(self.tid, sid, ver, p)["status"], "accepted")

    def test_cannot_seal_until_all_stations_arrive(self):
        self.svc.submit(self.tid, "S1", "v1", payload(20, 19.5, 40, 20, 18.1, 70))
        with self.assertRaises(FederationError) as ctx:
            self.svc.seal(self.tid)
        self.assertIn("未到齐", str(ctx.exception))
        self.assertIn("S2", str(ctx.exception))

    def test_seal_uses_merged_summary_and_snapshot_is_stable(self):
        self._fill()
        info = self.svc.seal(self.tid)
        self.assertFalse(info["already_sealed"])
        view = self.svc.trial_view(self.tid)
        self.assertEqual(view["status"], "sealed")
        self.assertIsNotNone(view["result"])
        self.assertEqual(set(view["used_summary"]), {"S1", "S2", "S3"})
        # 再次封算返回同一快照，不重算
        again = self.svc.seal(self.tid)
        self.assertTrue(again["already_sealed"])
        self.assertEqual(again["result"]["statistic_t"],
                         view["result"]["statistic_t"])

    def test_late_submission_after_seal_rejected_and_not_mixed(self):
        self._fill()
        self.svc.seal(self.tid)
        late = self.svc.submit(
            self.tid, "S1", "v1", payload(99, 1.0, 1.0, 99, 2.0, 1.0))
        self.assertEqual(late["status"], "rejected")
        view = self.svc.trial_view(self.tid)
        # 封算所用汇总保持原值，迟到数据未混入
        self.assertEqual(view["used_summary"]["S1"]["groups"]["A"]["n"], 20)

    def test_old_trial_late_data_never_mixes_into_new_trial(self):
        # 旧试验先收到 S1 后封算（另建站补齐），再建一个新试验
        self._fill()
        self.svc.seal(self.tid)
        new = self.svc.create_trial("新一期联合试验", 0.95, ROSTER)
        # 把旧试验 id 的上报错投到新试验：版本/名册相同但新试验尚未收集
        r = self.svc.submit(new["id"], "S1", "v1",
                            payload(20, 19.5, 40, 20, 18.1, 70))
        self.assertEqual(r["status"], "accepted")  # 新试验独立计数
        old_view = self.svc.trial_view(self.tid)
        new_view = self.svc.trial_view(new["id"])
        self.assertEqual(old_view["status"], "sealed")
        self.assertEqual(new_view["received_count"], 1)
        self.assertNotIn("S1", new_view["missing"])
        self.assertIn("S2", new_view["missing"])

    def test_recovery_after_interruption(self):
        self._fill()
        # 未封算即“中断”：重新打开库，进度应原样恢复
        self.svc.store.close()
        store2 = Store(self.db)
        svc2 = FederationService(store2)
        view = svc2.trial_view(self.tid)
        self.assertEqual(view["received_count"], 3)
        self.assertEqual(view["missing"], [])
        info = svc2.seal(self.tid)
        self.assertFalse(info["already_sealed"])
        # 再重开，封算快照仍在，不重复计算
        store2.close()
        store3 = Store(self.db)
        svc3 = FederationService(store3)
        self.assertEqual(svc3.trial_view(self.tid)["status"], "sealed")
        self.assertTrue(svc3.seal(self.tid)["already_sealed"])
        store3.close()


class ConcurrencyTests(ServiceTestBase):
    def test_concurrent_submit_and_seal_keep_consistency(self):
        # 三站并发上报，同时反复尝试封算；最终要么封算失败（缺站），
        # 要么封算成功且结果所用汇总恰好是当时全部三站的汇总。
        barrier = threading.Barrier(3)
        errors = []

        def worker(sid, ver):
            barrier.wait()
            for _ in range(3):
                r = self.svc.submit(
                    self.tid, sid, ver,
                    payload(15, 19.5 + len(sid), 30, 15, 18.0, 50))
                # 合法结果：接收/幂等；封算后迟到拒收也合法，但不得出现异值冲突
                if r["status"] not in ("accepted", "duplicate", "rejected"):
                    errors.append(r)
                if r["status"] == "rejected" and "不一致" in r["detail"]:
                    errors.append(r)

        threads = [threading.Thread(target=worker, args=(s["station_id"],
                                                         s["sample_version"]))
                   for s in ROSTER]
        for t in threads:
            t.start()
        seal_outcomes = []
        for _ in range(20):
            try:
                seal_outcomes.append(self.svc.seal(self.tid))
            except FederationError:
                pass
        for t in threads:
            t.join()
        self.assertFalse(errors)
        # 三站全部结束后必定可以封算（若并发期未封成，此处兜底）
        info = self.svc.seal(self.tid)
        seal_outcomes.append(info)
        # 并发期任何成功封算都必须基于齐全汇总
        for outcome in seal_outcomes:
            if not outcome.get("already_sealed"):
                self.assertEqual(set(outcome["used_summary"]),
                                 {"S1", "S2", "S3"})
        view = self.svc.trial_view(self.tid)
        self.assertEqual(view["status"], "sealed")
        # 结果所用汇总与库内 submissions 完全一致（无重复合计）
        subs = {s["station_id"]: s["payload"]
                for s in self.svc.store.list_submissions(self.tid)}
        self.assertEqual(set(subs), {"S1", "S2", "S3"})
        for sid, p in view["used_summary"].items():
            self.assertEqual(p, subs[sid])
        # 独立重算应与封算快照一致
        expect = analyze_payloads(
            [subs[s["station_id"]] for s in ROSTER], 0.95)
        self.assertAlmostEqual(expect["statistic_t"],
                               view["result"]["statistic_t"], places=12)


if __name__ == "__main__":
    unittest.main()
