"""联合试验命令行入口（纯标准库）。

子命令：
  center --db PATH [--host H --port P]        启动中心服务与简报页面
  demo   --workdir PATH                       生成两站本地原始片段与演示试验数据
  station-flush --db PATH --station ID --url  站点打开发件箱重试（离线补交）
  seal  --url CENTER --trial ID               触发封算
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import urllib.request

from .app import build_server
from .station import StationClient, StationStore


def _post(base, path, body):
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get(base, path):
    with urllib.request.urlopen(base.rstrip("/") + path, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def cmd_center(args):
    httpd = build_server(args.db, host=args.host, port=args.port, verbose=True)
    print(f"联合试验中心已启动：http://{args.host}:{httpd.server_address[1]}")
    print("简报页面：/  ；健康检查：/health")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()
        httpd.store.close()


def _gen_fragments(seed, arm_mu_sigma, per_arm, station_tag):
    rng = random.Random(seed)
    rows = []
    for arm, mu, sigma in arm_mu_sigma:
        for i in range(per_arm):
            shift = "早班" if i % 2 == 0 else "晚班"
            seconds = round(max(1.0, rng.gauss(
                mu + (0.8 if shift == "晚班" else 0), sigma)), 3)
            rows.append({"run": f"{station_tag}-{arm}{i+1:03}",
                         "arm": arm, "shift": shift, "seconds": seconds})
    return rows


def cmd_demo(args):
    """在两个站点目录各自生成原始片段（只在站点本地），并建立演示试验。"""
    wd = args.workdir
    os.makedirs(wd, exist_ok=True)
    center_db = os.path.join(wd, "center.db")
    stations = [
        {"station_id": "YC-NORTH", "station_name": "榆川北车间",
         "sample_version": "sv-2026-11a", "seed": 2001, "tag": "N", "per": 40},
        {"station_id": "YC-SOUTH", "station_name": "榆川南车间",
         "sample_version": "sv-2026-11b", "seed": 2002, "tag": "S", "per": 36},
    ]
    # 1) 中心建试验，固定名单与版本
    from .store import Store
    from .service import FederationService
    svc = FederationService(Store(center_db))
    trial = svc.create_trial(
        "两车间封装动作 11 月联合试验", 0.95,
        [{"station_id": s["station_id"], "station_name": s["station_name"],
          "sample_version": s["sample_version"]} for s in stations])
    clients = []
    for s in stations:
        # 2) 原始片段只写入站点本地库
        sdb = os.path.join(wd, f"station-{s['station_id']}.db")
        store = StationStore(sdb)
        rows = _gen_fragments(
            s["seed"], [("A", 19.5, 1.4), ("B", 18.2, 2.1)], s["per"], s["tag"])
        store.add_fragments(trial["id"], rows)
        client = StationClient(s["station_id"], s["station_name"],
                               s["sample_version"], store, args.url)
        client.prepare(trial["id"])  # 本地算 n/mean/ss 并入发件箱，尚未发送
        clients.append(client)
        view = client.local_view(trial["id"])
        print(f"[{s['station_name']}] 本地原始片段 {view['local_fragment_count']} 条"
              f"（仅存 {sdb}）；发件箱已就绪，报文不含原值。")
    print(f"演示试验已建立：trial_id={trial['id']}，中心库 {center_db}")
    print("站点启动并联网后执行 station-flush 上报；全部到齐后执行 seal 封算。")


def cmd_flush(args):
    store = StationStore(args.db)
    # 站点身份从发件箱内容读取，CLI 只需指定本地库
    item = store.outbox_view()[0]
    client = StationClient(item["station_id"], item["station_id"],
                           item["sample_version"], store, args.url)
    for r in client.flush():
        print(json.dumps(r, ensure_ascii=False))


def cmd_seal(args):
    print(json.dumps(_post(args.url, f"/api/federation/trials/{args.trial}/seal", {}),
                     ensure_ascii=False, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description="联合试验（联邦汇总）工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("center")
    p.add_argument("--db", required=True)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=19303)
    p.set_defaults(func=cmd_center)

    p = sub.add_parser("demo")
    p.add_argument("--workdir", required=True)
    p.add_argument("--url", default="http://127.0.0.1:19303")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("station-flush")
    p.add_argument("--db", required=True)
    p.add_argument("--url", default="http://127.0.0.1:19303")
    p.set_defaults(func=cmd_flush)

    p = sub.add_parser("seal")
    p.add_argument("--url", default="http://127.0.0.1:19303")
    p.add_argument("--trial", type=int, required=True)
    p.set_defaults(func=cmd_seal)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
