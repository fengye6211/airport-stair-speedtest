# ookla.py — Ookla 服务器并行测速（speedtest.net / trevor.speedtestcustom.com 同引擎）
# 说明：fast.com 已被 Netflix 封禁自动化（403 Not Available），
# trevor.speedtestcustom.com 经逆向确认就是官方 Ookla speedtest-js-engine，
# 服务器列表来自 {subdomain}.speedtestcustom.com/api/js/servers（Ookla 全球网络）。
# 本模块按网页版同款方式：多连接并行下载随机图片，聚合测速。
import concurrent.futures
import random
import threading
import time

import requests

SIZES = ["random500x500.jpg", "random1000x1000.jpg", "random2000x2000.jpg",
         "random3000x3000.jpg", "random4000x4000.jpg"]
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0"


def fetch_servers(api_base, limit=20, proxy_port=None, timeout=25):
    """从 Ookla 服务器列表 API 取最近的服务器（speedtest.net 或 trevor.speedtestcustom.com）"""
    proxies = {"http": f"http://127.0.0.1:{proxy_port}", "https": f"http://127.0.0.1:{proxy_port}"} if proxy_port else None
    url = f"https://{api_base}/api/js/servers?engine=js&https_functional=true&limit={limit}"
    r = requests.get(url, headers={"User-Agent": UA}, proxies=proxies, timeout=timeout)
    r.raise_for_status()
    servers = r.json()
    return [{"name": s.get("name"), "sponsor": s.get("sponsor"), "id": s.get("id"),
             "host": s.get("host", "").replace(":8080", "")} for s in servers]


def parallel_download(proxy_port, servers, duration=20, streams=8, chunk=65536):
    """多流并行下载随机图片（与 speedtest.net 网页同思路），每秒聚合采样"""
    if not servers:
        return {"ok": False, "err": "no servers"}
    hosts = [f"http://{s['host']}:8080" for s in servers]
    proxies = {"http": f"http://127.0.0.1:{proxy_port}", "https": f"http://127.0.0.1:{proxy_port}"}
    start = time.time()
    deadline = start + duration
    total = [0]
    lock = threading.Lock()
    snapshots = []
    snap_lock = threading.Lock()
    sampling = [True]

    def worker(i):
        host = hosts[i % len(hosts)]
        local = 0
        try:
            while time.time() < deadline:
                url = f"{host}/speedtest/{random.choice(SIZES)}"
                with requests.get(url, proxies=proxies, stream=True,
                                  headers={"User-Agent": UA}, timeout=(8, 20)) as r:
                    if r.status_code != 200:
                        time.sleep(0.3)
                        continue
                    for chunk_ in r.iter_content(chunk):
                        local += len(chunk_)
                        if time.time() >= deadline:
                            break
        except Exception:
            pass
        with lock:
            total[0] += local
        return local

    def sampler():
        prev = 0
        while sampling[0]:
            time.sleep(1.0)
            with lock:
                cur = total[0]
            with snap_lock:
                snapshots.append(cur - prev)
            prev = cur

    th = threading.Thread(target=sampler, daemon=True)
    th.start()

    with concurrent.futures.ThreadPoolExecutor(max_workers=streams) as ex:
        futs = [ex.submit(worker, i) for i in range(streams)]
        results = [f.result() for f in futs]

    sampling[0] = False
    th.join(timeout=3)
    ms = (time.time() - start) * 1000
    total_bytes = total[0]
    mbps = total_bytes * 8 / 1e6 / (ms / 1000) if ms > 0 else 0
    with snap_lock:
        seconds = [round(s * 8 / 1e6, 2) for s in snapshots]
    return {"ok": True, "servers": [s["name"] for s in servers[:3]],
            "sponsors": [s["sponsor"] for s in servers[:3]],
            "streams": streams, "totalBytes": total_bytes, "ms": int(ms),
            "mbps": round(mbps, 2),
            "seconds": seconds,
            "minSampleMbps": round(min(seconds), 2) if seconds else None}


def run(proxy_port, duration=20, streams=8, api_base="www.speedtest.net", server_count=3):
    """一站式：取服务器 → 并行下载。

    服务器列表优先走被测节点的代理获取：国内直连 speedtest.net API 常被墙；
    且经代理取到的列表是「出口附近」的服务器，与网页版经该出口测速的行为一致。
    代理获取失败再回退直连。"""
    servers = []
    try:
        servers = fetch_servers(api_base, limit=20, proxy_port=proxy_port)[:server_count]
    except Exception:
        servers = []
    if not servers:
        try:
            servers = fetch_servers(api_base, limit=20, proxy_port=None)[:server_count]
        except Exception as e:
            return {"ok": False, "err": f"服务器列表获取失败: {str(e)[:80]}"}
    return parallel_download(proxy_port, servers, duration, streams)


if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 7890
    dur = int(sys.argv[2]) if len(sys.argv) > 2 else 15
    base = sys.argv[3] if len(sys.argv) > 3 else "www.speedtest.net"
    import json
    print(json.dumps(run(port, dur, 8, base), ensure_ascii=False, indent=1))
