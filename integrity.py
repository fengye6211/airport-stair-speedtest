# integrity.py — 数据真实性自检（防失真）
# 实战教训总结：测速数据失真的几大来源
#   1) speed.cloudflare.com 单连接限流（首秒爆发后骤降）→ 单线程数据被压低
#   2) 小文件+持续下载 → 频繁重连 → 触发机场“新建连接限流”
#   3) mihomo GLOBAL 默认 DIRECT，节点切换失败会悄悄走直连
#   4) 多实例/多任务并发抢同一节点带宽 → 互相压低
#   5) 晚高峰 vs 非高峰数据直接对比 → 误判机场质量
# 本模块在每轮测试后自动运行，输出可读告警，并在报告图中标注。

import datetime
import statistics


def norm_summary(summary):
    """落盘 JSON 的 summary 是扁平字段（avg_mbps 等），统一转为内存列表格式"""
    out = {}
    for k, v in summary.items():
        if not isinstance(v, dict):
            continue
        if "avgs" in v:
            out[k] = v
            continue
        out[k] = {
            "avgs": [v["avg_mbps"]] if v.get("avg_mbps") is not None else [],
            "mins": [v["min_mbps"]] if v.get("min_mbps") is not None else [],
            "multis": [v["multi_mbps"]] if v.get("multi_mbps") is not None else [],
            "lats": [v["avg_latency_ms"]] if v.get("avg_latency_ms") is not None else [],
            "losses": [v["loss_pct"]] if v.get("loss_pct") is not None else [],
            "stalls": v.get("stalls", 0) if isinstance(v.get("stalls"), (int, float)) else sum(v.get("stalls", []) or []),
            "rounds": v.get("rounds") if isinstance(v.get("rounds"), int) else None,
        }
    return out


def _alive(summary):
    return {k: v for k, v in summary.items() if "直连" not in k and v.get("avgs")}


def analyze(summary, meta=None):
    """返回告警列表。meta: dict(duration=秒, threads=线程数, time=datetime, url=订阅来源)"""
    meta = meta or {}
    summary = norm_summary(summary)
    warns = []
    nodes = _alive(summary)

    # 1) 时段标注（晚高峰对比失真）
    now = meta.get("time") or datetime.datetime.now()
    h = now.hour
    if h >= 19 or h < 1:
        warns.append(f"[时段] 当前为晚高峰时段({now:%H:%M})，机场速度普遍低于非高峰，"
                     f"请勿与白天的历史数据直接对比（可 --loop 多轮覆盖再评估）")
    else:
        warns.append(f"[时段] 测试于 {now:%H:%M}（非高峰）进行，速度通常优于晚高峰")

    # 2) “全节点均匀慢”模式：单连接限流特征
    avgs = [statistics.mean(v["avgs"]) for v in nodes.values() if v["avgs"]]
    multis = [statistics.mean(v["multis"]) for v in nodes.values() if v.get("multis")]
    if len(avgs) >= 4:
        med = statistics.median(avgs)
        if med > 0.5:
            uniform = sum(1 for a in avgs if abs(a - med) <= med * 0.15) / len(avgs)
            if uniform >= 0.6:
                if multis and statistics.median(multis) > 3 * med:
                    warns.append(
                        f"[限流特征] 全部节点单线程速度异常均匀(±15%内占{uniform:.0%})且多线程远高于单线程"
                        f"(中位 {med:.1f} vs {statistics.median(multis):.1f} MB/s)——"
                        f"疑似单连接限流/测试源问题，单线程数据不可信，请检查测速源或换 --test-url 重测")
                else:
                    warns.append(
                        f"[异常均匀] 全部节点单线程速度异常均匀(±15%内占{uniform:.0%})，"
                        f"可能为测试源限流或机场统一限速")

    # 3) 直连快于节点：节点选择可能未生效（仅当节点速度本身很低时才告警，避免直连/节点正常差异误报）
    direct = summary.get("直连(无代理)")
    if direct and direct.get("avgs") and avgs:
        d_avg = statistics.mean(direct["avgs"])
        best = max(avgs)
        if d_avg > 2 * best and best < 20:
            warns.append(
                f"[直连更快] 直连基线({d_avg:.1f} MB/s)远高于全部节点最高({best:.1f} MB/s)——"
                f"节点可能未生效(检查 GLOBAL 选择)或机场当前不可用")

    # 4) 文件提前下完 / 重连失真
    duration = meta.get("duration") or 0
    early = []
    for name, v in nodes.items():
        # 用轮次明细的 samples 数量判断（summary 不含 samples，此处靠 meta.rounds）
        pass
    if meta.get("rounds"):
        for rnd in meta["rounds"]:
            for row in rnd.get("rows", []):
                res = row.get("res") or {}
                if res and duration > 2 and res.get("samples") and row.get("name") != "直连(无代理)":
                    n = len(res["samples"])
                    if n < duration - 2:
                        early.append(f"{row['name'][:20]}({n}s/{duration}s)")
                if res and res.get("reconnects"):
                    warns.append(f"[重连] {row['name'][:24]} 发生 {res['reconnects']} 次重连，"
                                 f"可能触发机场连接限流，该节点速度或偏低")
    if early:
        warns.append(f"[文件早完] 以下节点测速文件提前下完({len(early)}个): " + "、".join(early[:5]) +
                     "，数据可能失真，建议加大 --size-mb 或换更大文件源")

    # 5) 断流严重节点
    heavy = [f"{k[:18]}(断流{v['stalls']})" for k, v in nodes.items()
             if duration > 2 and v.get("stalls", 0) >= max(3, duration / 3)]
    if heavy:
        warns.append(f"[断流严重] " + "、".join(heavy[:6]) + " —— 看视频会频繁卡顿")

    # 6) 多线程缺失（仅精测模式检查：快扫只测单线程是设计，不告警）
    if meta.get("threads", 1) > 1 and meta.get("duration", 0) > 0:
        missing = [k[:18] for k, v in nodes.items() if not v.get("multis")]
        if missing:
            warns.append(f"[多线程缺失] {len(missing)} 个节点多线程测试失败: " + "、".join(missing[:5]))

    # 7) Emby 压测：晚高峰 QoS 限速 / 多路并发不足
    throttled, starved = [], []
    for e in meta.get("emby") or []:
        if not isinstance(e, dict):
            continue
        pa = e.get("phaseA") or {}
        pb = e.get("phaseB") or {}
        th = pa.get("throttle")
        if th is not None and th < 0.7 and pa.get("first3"):
            throttled.append(f"{e.get('name', '?')[:18]}(前{pa['first3']:.0f}→后{pa['last3']:.0f}MB/s,{th:.0%})")
        if pb and pb.get("worst") is not None and pb["worst"] < 8:
            starved.append(f"{e.get('name', '?')[:18]}({pb['streams']}路最差{pb['worst']:.1f}MB/s)")
    if throttled:
        warns.append("[晚高峰限速] 持续流量后被压速（长片源会越看越卡）: " + "、".join(throttled[:4]))
    if starved:
        warns.append("[多路不足] 多设备同时观看会卡（最差一路<8MB/s）: " + "、".join(starved[:4]))

    return warns


def check_mihomo_instances():
    """检测是否存在多个 mihomo 实例（并发互扰风险）"""
    import subprocess
    try:
        # errors="replace"：中文 Windows 的 tasklist 输出为 GBK，
        # 若系统开了 UTF-8 模式（PYTHONUTF8=1）按 utf-8 解码会抛异常；ASCII 的
        # "mihomo.exe" 不受替换影响，计数仍准确
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq mihomo.exe", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, errors="replace", timeout=10).stdout
        n = sum(1 for line in out.splitlines() if "mihomo" in line.lower())
        if n > 1:
            return f"[并发互扰] 检测到 {n} 个 mihomo 实例在运行，可能互相抢占机场带宽导致数据失真；建议关闭其他代理客户端/测试进程"
    except Exception:
        pass
    return None


def verdict(warns):
    """整体数据质量评级"""
    serious = [w for w in warns if w.startswith(("[限流特征]", "[直连更快]", "[并发互扰]"))]
    if serious:
        return "C", "存在严重失真风险，数据仅供参考"
    if len(warns) >= 3:
        return "B", "数据基本可信，存在需注意的干扰因素"
    return "A", "数据可信（已通过防失真自检）"
