# report.py — StairSpeedTest 风格测速图（优化升级版）
# 与 StairSpeedTest 一致的输出：一张 PNG 评分/速度图 + 报告。
# 升级点：
#   1) 三面板：Top 速度柱状(单/多线程) + 延迟柱状 + 全节点表格
#   2) 图中直接标注丢包/断流/重连与评分等级颜色
#   3) 数据真实性自检告警直接画在图上（防失真）
#   4) HTML 单文件（内嵌 PNG base64），随时可分享、可离线打开
# 用法: python report.py <result.json> [--out-dir DIR] [--title 标题]
import argparse
import base64
import datetime
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engine import compute_score, grade_of, emby_verdict, calc_consistency  # noqa: E402
import integrity  # noqa: E402


def _clean(s):
    return "".join(ch for ch in str(s)
                   if not (0x1F000 <= ord(ch) <= 0x1FAFF)
                   and not (0x2600 <= ord(ch) <= 0x27BF)
                   and not (0x2B00 <= ord(ch) <= 0x2BFF)
                   and ord(ch) != 0x200D).strip()


def load_result(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_rows(data):
    """summary -> 排序后的行列表（兼容落盘扁平格式与内存列表格式）"""
    summary = integrity.norm_summary(data.get("summary", {}))
    rows = []
    for k, v in summary.items():
        if "直连" in k:
            continue
        avg = sum(v["avgs"]) / len(v["avgs"]) if v["avgs"] else 0
        mn = min(v["mins"]) if v["mins"] else avg
        multi = sum(v["multis"]) / len(v["multis"]) if v.get("multis") else None
        lat = sum(v["lats"]) / len(v["lats"]) if v.get("lats") else None
        loss = sum(v["losses"]) / len(v["losses"]) if v.get("losses") else None
        st = v.get("stalls", 0)
        if isinstance(st, list):
            st = sum(st)
        cons = calc_consistency(v["avgs"]) if v["avgs"] else None
        sc = compute_score({"avg": avg, "min": mn, "multi_avg": multi}, lat, loss, st, cons)
        rows.append({"name": k, "avg": avg, "min": mn, "multi": multi, "lat": lat,
                     "loss": loss, "stalls": st, "cons": cons, "score": sc,
                     "grade": grade_of(sc), "verdict": emby_verdict(avg, mn)})
    rows.sort(key=lambda r: -r["score"])
    return rows


def render_png(data, rows, warnings, out_path, title):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    for fname in ("Microsoft YaHei", "SimHei", "SimSun", "Noto Sans CJK SC", "DejaVu Sans"):
        try:
            font_manager.findfont(fname, fallback_to_default=False)
            plt.rcParams["font.sans-serif"] = [fname]
            break
        except Exception:
            continue
    plt.rcParams["axes.unicode_minus"] = False

    top = rows[:12][::-1]
    stamp = data.get("timestamp", datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
    n_nodes = len(rows)
    n_dead = len(data.get("summary", {})) - 1 - n_nodes  # summary含直连

    fig = plt.figure(figsize=(14, max(10, 5.5 + n_nodes * 0.24 + len(warnings) * 0.3)))
    gs = fig.add_gridspec(3, 1, height_ratios=[2.2, 1.2, 2.6], hspace=0.45)

    # ---- 面板1：Top 速度柱状（单线程实心 + 多线程浅色 + 评分/等级）----
    ax1 = fig.add_subplot(gs[0])
    if top:
        names = [_clean(r["name"])[:20] for r in top]
        avgs = [r["avg"] for r in top]
        multis = [r["multi"] or 0 for r in top]
        scores = [r["score"] for r in top]
        colors = ["#2ecc71" if s >= 80 else "#f1c40f" if s >= 60 else "#e74c3c" for s in scores]
        y = list(range(len(top)))
        ax1.barh(y, avgs, color=colors, alpha=0.9, label="单线程 MB/s")
        ax1.barh(y, multis, color="#3498db", alpha=0.45, label="多线程 MB/s")
        for yi, (r, nm) in zip(y, zip(top, names)):
            ax1.text(r["avg"] + 0.4, yi, f"{r['avg']:.1f} | {r['grade']}{r['score']}"
                     + (f" | 丢包{r['loss']:.0f}%" if r["loss"] else "")
                     + (f" | 断流{r['stalls']}" if r["stalls"] else ""),
                     va="center", fontsize=8)
        ax1.set_yticks(y)
        ax1.set_yticklabels(names, fontsize=8)
        ax1.set_xlabel("MB/s")
        ax1.set_title("Top 12 节点 · 下行速度（单线程实心 / 多线程浅色，标注=评分+等级+丢包+断流）",
                      fontsize=11)
        ax1.grid(axis="x", alpha=0.3)
        ax1.legend(fontsize=8, loc="lower right")
    else:
        ax1.text(0.5, 0.5, "无可用节点", ha="center", transform=ax1.transAxes)

    # ---- 面板2：延迟柱状 ----
    ax2 = fig.add_subplot(gs[1])
    lat_rows = [r for r in rows if r["lat"]][:12][::-1]
    if lat_rows:
        names2 = [_clean(r["name"])[:16] for r in lat_rows]
        lats = [r["lat"] for r in lat_rows]
        y2 = list(range(len(lat_rows)))
        ax2.barh(y2, lats, color="#9b59b6", alpha=0.85)
        for yi, (la, nm) in zip(y2, zip(lats, names2)):
            ax2.text(la + 4, yi, f"{la:.0f}ms", va="center", fontsize=8)
        ax2.set_yticks(y2)
        ax2.set_yticklabels(names2, fontsize=8)
        ax2.set_xlabel("ms")
        ax2.set_title("节点延迟（延迟越低越好）", fontsize=11)
        ax2.grid(axis="x", alpha=0.3)
    else:
        ax2.text(0.5, 0.5, "无延迟数据", ha="center", transform=ax2.transAxes)

    # ---- 面板3：全节点表格 ----
    ax3 = fig.add_subplot(gs[2])
    ax3.axis("off")
    rows_data = [[_clean(r["name"])[:22], f"{r['lat']:.0f}" if r["lat"] else "-",
                  f"{r['loss']:.0f}" if r["loss"] is not None else "-",
                  f"{r['avg']:.2f}", f"{r['multi']:.2f}" if r["multi"] else "-",
                  f"{r['min']:.2f}", f"{r['stalls']}",
                  f"{r['cons']:.2f}" if r["cons"] is not None else "-",
                  f"{r['score']} {r['grade']}", _clean(r["verdict"])] for r in rows[:25]]
    if rows_data:
        tbl = ax3.table(cellText=rows_data,
                        colLabels=["节点", "延迟ms", "丢包%", "单MB/s", "多MB/s",
                                   "最低MB/s", "断流", "一致性", "评分", "评估"],
                        loc="center", cellLoc="left")
        tbl.auto_set_font_size(False)
        tbl.set_fontsize(7.5)
        tbl.scale(1, 1.15)
    ax3.set_title(f"全部节点（{n_nodes} 可用" + (f"，{n_dead} 不可用" if n_dead else "") + "，前25）",
                  fontsize=11)

    # ---- 数据真实性自检区 ----
    axw = fig.add_axes([0.04, 0.005, 0.92, 0.02 + 0.016 * len(warnings)])
    axw.axis("off")
    axw.text(0, 1, "\n".join(warnings[:8]), va="top", ha="left", fontsize=7.5, color="#c0392b")

    q, qmsg = integrity.verdict(warnings)
    fig.suptitle(f"{title}  [{q}级数据质量]  {stamp[:4]}-{stamp[4:6]}-{stamp[6:8]} "
                 f"{stamp[8:10]}:{stamp[10:12]}   {qmsg}",
                 fontsize=13, fontweight="bold")
    fig.savefig(out_path, dpi=140, bbox_inches="tight")
    plt.close(fig)
    return out_path


def render_html(data, rows, warnings, png_path, out_path, title, source_url):
    with open(png_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    q, qmsg = integrity.verdict(warnings)
    stamp = data.get("timestamp", "")
    trs = []
    for r in rows:
        trs.append(
            "<tr><td class='l'>" + str(r["name"]) + "</td>"
            + ("<td>" + f"{r['lat']:.0f}</td>" if r["lat"] else "<td>-</td>")
            + ("<td>" + f"{r['loss']:.0f}</td>" if r["loss"] is not None else "<td>-</td>")
            + "<td><b>" + f"{r['avg']:.2f}</b></td>"
            + ("<td>" + f"{r['multi']:.2f}</td>" if r["multi"] else "<td>-</td>")
            + "<td>" + f"{r['min']:.2f}</td><td>{r['stalls']}</td>"
            + ("<td>" + f"{r['cons']:.2f}</td>" if r["cons"] is not None else "<td>-</td>")
            + "<td><span class='g" + str(r["grade"]) + "'>" + f"{r['score']} {r['grade']}</span></td>"
            + "<td class='l'>" + str(r["verdict"]) + "</td></tr>")
    warns_html = "".join(f"<li>{w}</li>" for w in warnings) or "<li>无（通过自检）</li>"
    html = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>__TITLE__ · 测速报告</title>
<style>
:root{color-scheme:dark}
body{font-family:'Microsoft YaHei',system-ui,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;padding:24px}
h1{color:#f8fafc;border-bottom:2px solid #334155;padding-bottom:10px}
h2{color:#7dd3fc;margin-top:28px}
img{max-width:100%;border-radius:8px}
table{border-collapse:collapse;width:100%;font-size:13px;margin-top:10px}
th{background:#1e293b;color:#94a3b8;padding:6px 8px;cursor:pointer;position:sticky;top:0}
td{padding:5px 8px;border-bottom:1px solid #1e293b;text-align:center}
td.l{text-align:left}
.gS{color:#f59e0b;font-weight:bold}.gA{color:#22c55e;font-weight:bold}
.gB{color:#84cc16;font-weight:bold}.gC{color:#eab308;font-weight:bold}
.gD{color:#ef4444;font-weight:bold}
.warn{background:#1e293b;border-left:4px solid #f59e0b;padding:10px 14px;border-radius:4px;font-size:13px}
.meta{color:#64748b;font-size:12px}
.qA{color:#22c55e;font-weight:bold}.qB{color:#eab308;font-weight:bold}.qC{color:#ef4444;font-weight:bold}
</style></head><body>
<h1>🌐 __TITLE__</h1>
<p class="meta">时间戳 __STAMP__ · 数据质量: <span class="q__Q__">__Q__级 · __QMSG__</span></p>
<p class="meta">订阅来源: __SOURCE__ · 原始数据: 同目录 result_*.json（含逐秒采样，可复核）</p>
<h2>测速图</h2>
<img src="data:image/png;base64,__B64__" alt="测速图">
<h2>数据真实性自检</h2>
<ul class="warn">__WARNS__</ul>
<h2>全部节点明细（点击表头排序）</h2>
<table id="t"><thead><tr><th>节点</th><th>延迟ms</th><th>丢包%</th><th>单MB/s</th><th>多MB/s</th><th>最低MB/s</th><th>断流</th><th>一致性</th><th>评分</th><th>评估</th></tr></thead>
<tbody>__ROWS__</tbody></table>
<script>
const t=document.getElementById('t');let dir=1;
t.querySelectorAll('th').forEach((th,i)=>th.onclick=()=>{dir*=-1;
const tb=t.tBodies[0];[...tb.rows].sort((a,b)=>{const x=a.cells[i].innerText,y=b.cells[i].innerText;
const nx=parseFloat(x),ny=parseFloat(y);return (isNaN(nx)?x.localeCompare(y):nx-ny)*dir;})
.forEach(r=>tb.appendChild(r));});
</script>
</body></html>"""
    html = (html.replace("__TITLE__", str(title))
                .replace("__STAMP__", str(stamp))
                .replace("__Q__", str(q))
                .replace("__QMSG__", str(qmsg))
                .replace("__SOURCE__", str(source_url or "本地文件"))
                .replace("__B64__", b64)
                .replace("__WARNS__", warns_html)
                .replace("__ROWS__", "".join(trs)))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return out_path


def main():
    ap = argparse.ArgumentParser(description="生成 StairSpeedTest 风格测速报告（PNG+HTML）")
    ap.add_argument("result_json")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--title", default="机场节点测速报告")
    ap.add_argument("--source-url", default="")
    args = ap.parse_args()

    data = load_result(args.result_json)
    rows = build_rows(data)
    stamp = data.get("timestamp", datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
    out_dir = args.out_dir or os.path.dirname(os.path.abspath(args.result_json))
    meta = {"duration": data.get("duration_s", 0), "threads": 4,
            "time": datetime.datetime.strptime(stamp, "%Y%m%d_%H%M%S") if len(stamp) == 14 else datetime.datetime.now(),
            "rounds": data.get("rounds", [])}
    warnings = integrity.analyze(data.get("summary", {}), meta)
    w = integrity.check_mihomo_instances()
    if w:
        warnings.insert(0, w)

    png = os.path.join(out_dir, f"result_report_{stamp}.png")
    html = os.path.join(out_dir, f"result_report_{stamp}.html")
    render_png(data, rows, warnings, png, args.title)
    render_html(data, rows, warnings, png, html, args.title, args.source_url)
    print(f"[+] 评分图: {png}")
    print(f"[+] HTML报告: {html}")
    for w in warnings:
        print(f"    ! {w}")


if __name__ == "__main__":
    main()
