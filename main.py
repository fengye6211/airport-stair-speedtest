#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""机场节点测速 · StairSpeedTest 风格 · 防失真升级版

随时随地测速：粘贴订阅链接或拖入订阅文件，自动解析、全量测速、
输出 StairSpeedTest 同款测速图（PNG）+ 单文件 HTML 报告（可分享）。

用法示例:
  python main.py "https://你的机场订阅链接"
  python main.py "D:\\下载\\订阅.yaml" --duration 30 --report
  python main.py <链接或文件> --limit 20 --duration 20
  python main.py <链接或文件> --ookla 3          # 对 Top3 追加 Ookla/trevor 深测
  python main.py <链接或文件> --title 我的机场 --source-url https://...

数据真实性（防失真）保证见 README.md「数据可信度」与 LESSONS.md。
"""
import datetime
import glob
import json
import os
import socket
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import integrity  # noqa: E402
import report     # noqa: E402

MIHOMO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools", "mihomo.exe")
NDK_URL = "https://dl.google.com/android/repository/android-ndk-r21e-linux-x86_64.zip"
G204 = "http://www.gstatic.com/generate_204"


def _own_args(argv):
    """剥离本项目自己的参数，其余原样传给引擎"""
    own = {"ookla": 0, "title": "机场节点测速报告", "source_url": ""}
    rest = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--ookla":
            own["ookla"] = int(argv[i + 1]) if i + 1 < len(argv) and argv[i + 1].isdigit() else 3
            i += 2
        elif a == "--title":
            own["title"] = argv[i + 1]
            i += 2
        elif a == "--source-url":
            own["source_url"] = argv[i + 1]
            i += 2
        else:
            rest.append(a)
            i += 1
    return own, rest


def newest_result(out_dir):
    files = glob.glob(os.path.join(out_dir, "result_2*.json"))
    files = [f for f in files if "deep" not in f and "report" not in f]
    return max(files, key=os.path.getmtime) if files else None


# ---------------- Ookla / trevor 深度测试（Top N 节点） ----------------

def _wait_port(port, timeout=25):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=1)
            s.close()
            return True
        except OSError:
            time.sleep(0.3)
    return False


def _latency(port, probes=5):
    import requests
    proxies = {"http": f"http://127.0.0.1:{port}", "https": f"http://127.0.0.1:{port}"}
    rtts = []
    for _ in range(probes):
        t0 = time.time()
        try:
            r = requests.get(G204, proxies=proxies, timeout=8)
            if r.status_code in (200, 204):
                rtts.append((time.time() - t0) * 1000)
        except Exception:
            pass
    return {"avgMs": round(sum(rtts) / len(rtts)) if rtts else None,
            "lossPct": round((1 - len(rtts) / probes) * 100)}


def _single_line(port, duration):
    import engine
    proxies = {"http": f"http://127.0.0.1:{port}", "https": f"http://127.0.0.1:{port}"}
    res, err = engine.stream_download(NDK_URL, proxies, duration, 1100 * 1024 * 1024,
                                      60, warmup=0)
    if res is None:
        return {"ok": False, "err": err}
    return {"ok": True, "mbps": round(res["avg"] * 8, 1), "mbps_min": round(res["min"] * 8, 1),
            "stalls": res["stalls"], "samples": res["samples"]}


def run_ookla_deep(proxies, top_names, duration=20, base_port=7850):
    """对指定节点逐个：起单节点 mihomo → 延迟/单线Google/Ookla/trevor 深测"""
    import engine
    import ookla
    results = []
    for i, name in enumerate(top_names):
        proxy = next((p for p in proxies if p.get("name") == name), None)
        if proxy is None:
            continue
        port = base_port + i * 2
        api = port + 1
        cfg = engine.build_config([proxy], port, api)
        cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "work", "deep.yaml")
        os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write(cfg)
        # 确保 GeoIP 库就位（mihomo 缺它无法启动）
        geo_src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools", "geoip.metadb")
        geo_dst = os.path.join(os.path.dirname(cfg_path), "geoip.metadb")
        if os.path.exists(geo_src) and not os.path.exists(geo_dst):
            import shutil
            shutil.copy(geo_src, geo_dst)
        logf = open(os.path.join(os.path.dirname(cfg_path), "deep_mihomo.log"), "w", encoding="utf-8")
        proc = subprocess.Popen([MIHOMO, "-d", os.path.dirname(cfg_path), "-f", cfg_path],
                                stdout=logf, stderr=subprocess.STDOUT)
        print(f"\n=== 深测 {name} ===", flush=True)
        if not _wait_port(port):
            proc.kill()
            results.append({"name": name, "ok": False, "err": "mihomo start failed"})
            continue
        time.sleep(0.8)
        entry = {"name": name, "ok": True}
        try:
            entry["latency"] = _latency(port)
            entry["singleGoogleMbps"] = _single_line(port, duration)
            entry["ookla"] = ookla.run(port, duration, 8, "www.speedtest.net")
            entry["trevor"] = ookla.run(port, duration, 8, "trevor.speedtestcustom.com")
        except Exception as e:
            entry["err"] = str(e)[:120]
        finally:
            try:
                proc.kill()
            except Exception:
                pass
        results.append(entry)
        print(f"  延迟={entry.get('latency')} 单线={entry.get('singleGoogleMbps', {}).get('mbps')}"
              f"Mbps ookla={entry.get('ookla', {}).get('mbps')}Mbps trevor={entry.get('trevor', {}).get('mbps')}Mbps",
              flush=True)
    return results


def main():
    own, rest = _own_args(sys.argv[1:])

    # 环境预检：多 mihomo 实例并发会互相压低带宽（失真源）
    w = integrity.check_mihomo_instances()
    if w:
        print("  ! " + w)

    # 调用引擎（全部引擎参数原样透传）
    if not rest:
        print("用法: python main.py <订阅链接或文件> [--duration 秒] [--limit N] [--ookla N] ...")
        return
    import engine
    sys.argv = ["engine"] + rest
    try:
        engine.main()
    except SystemExit as e:
        if e.code not in (None, 0):
            raise

    # 生成 StairSpeedTest 风格报告（PNG + 单文件 HTML）
    out_dir = os.path.dirname(os.path.abspath(__file__))
    rj = newest_result(out_dir)
    if not rj:
        print("[!] 未找到测速结果 JSON，跳过报告生成")
        return
    print(f"[*] 生成报告: {rj}")
    data = report.load_result(rj)
    rows = report.build_rows(data)
    stamp = data.get("timestamp", datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
    meta = {"duration": data.get("duration_s", 0), "threads": 4,
            "time": datetime.datetime.strptime(stamp, "%Y%m%d_%H%M%S") if len(stamp) == 14 else datetime.datetime.now(),
            "rounds": data.get("rounds", [])}
    warnings = integrity.analyze(data.get("summary", {}), meta)

    # Ookla/trevor 深测（可选 --ookla N）
    deep = []
    if own["ookla"] > 0:
        print(f"[*] 对 Top {own['ookla']} 节点做 Ookla/trevor 深度测试...")
        top_names = [r["name"] for r in rows[:own["ookla"]]]
        try:
            # 重新解析订阅得到完整节点
            text, raw = engine.load_subscription(rest[0], engine.DEFAULT_UA)
            proxies = engine.parse_subscription_text(text)
            if not proxies and "proxies:" in text[:2000]:
                import yaml
                doc = yaml.safe_load(text)
                proxies = doc.get("proxies", [])
            proxies = [p for p in proxies
                       if str(p.get("server", "")).strip() not in ("127.0.0.1", "localhost", "0.0.0.0")]
            deep = run_ookla_deep(proxies, top_names, duration=max(15, data.get("duration_s", 0) or 15))
            deep_path = os.path.join(out_dir, f"deep_results_{stamp}.json")
            with open(deep_path, "w", encoding="utf-8") as f:
                json.dump(deep, f, ensure_ascii=False, indent=1)
            print(f"[+] 深测结果: {deep_path}")
        except Exception as e:
            print(f"[!] 深测失败: {e}")

    png = os.path.join(out_dir, f"result_report_{stamp}.png")
    html = os.path.join(out_dir, f"result_report_{stamp}.html")
    report.render_png(data, rows, warnings, png, own["title"])
    report.render_html(data, rows, warnings, png, html, own["title"], own["source_url"])
    print(f"[+] 评分图: {png}")
    print(f"[+] HTML报告: {html}")
    for wmsg in warnings:
        print(f"    ! {wmsg}")
    q, qmsg = integrity.verdict(warnings)
    print(f"[*] 数据质量: {q}级 · {qmsg}")


if __name__ == "__main__":
    main()
