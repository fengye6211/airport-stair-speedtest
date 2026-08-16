#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""机场节点测速 · Web 版（StairSpeedTest 应用式体验）

双击 启动测速.bat → 命令行页面显示端口链接 → 点击跳转浏览器 GUI：
粘贴订阅链接 / 上传订阅文件 → 点「开始测速」→ 实时进度 → 页面下方出结果
（测速图 + 明细表 + 数据质量评级 + 防失真告警）。

依赖：仅 Python 标准库 + 项目依赖（requests/pyyaml/matplotlib）。
用法: python webapp.py [端口]   （默认 8787）
"""
import base64
import datetime
import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(BASE, "work")
LOG = os.path.join(WORK, "web_run.log")
PROGRESS = os.path.join(BASE, "progress.json")
UPLOAD = os.path.join(WORK, "uploads")
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8787

os.makedirs(WORK, exist_ok=True)
os.makedirs(UPLOAD, exist_ok=True)

STATE = {"running": False, "proc": None, "started_at": None, "job_id": None,
         "last_result": None, "cancel": False, "stage": "", "stage_idx": 0, "stage_total": 1}


def build_fast_sub(src, result_json, n=10):
    """从快扫结果取 Top N 节点，生成过滤订阅（Clash YAML）供精测阶段使用"""
    import engine as E
    text, _raw = E.load_subscription(src, E.DEFAULT_UA)
    proxies = E.parse_subscription_text(text)
    if not proxies and "proxies:" in text[:2000]:
        import yaml
        proxies = yaml.safe_load(text).get("proxies", [])
    data = json.load(open(result_json, "r", encoding="utf-8"))
    ranked = sorted(data.get("summary", {}).items(),
                    key=lambda kv: -(kv[1].get("score") or 0))
    names = [k for k, _v in ranked if "直连" not in k][:n]
    order = {nm: i for i, nm in enumerate(names)}
    picked = [p for p in proxies if p.get("name") in names]
    picked.sort(key=lambda p: order.get(p.get("name"), 99))
    out = os.path.join(UPLOAD, "fast_sub.yaml")
    import yaml
    with open(out, "w", encoding="utf-8") as f:
        yaml.safe_dump({"proxies": picked}, f, allow_unicode=True, sort_keys=False)
    return out, len(picked)


def build_stages(params):
    """按模式生成阶段命令队列（快速=快扫全量+Top精测；完整=全部精测）"""
    src = (params.get("source") or "").strip()
    file_b64 = params.get("file_b64") or ""
    fname = params.get("filename") or "upload.txt"
    if file_b64:
        fpath = os.path.join(UPLOAD, os.path.basename(fname) or "upload.txt")
        with open(fpath, "wb") as f:
            f.write(base64.b64decode(file_b64))
        src = fpath
    if not src:
        return None, "请填写订阅链接或上传订阅文件"

    duration = int(params.get("duration") or 15)
    limit = int(params.get("limit") or 0)
    ookla = int(params.get("ookla") or 0)
    title = (params.get("title") or "机场节点测速").strip() or "机场节点测速"
    mode = params.get("mode") or "fast"
    common = []
    if limit:
        common += ["--limit", str(limit)]
    if ookla:
        common += ["--ookla", str(ookla)]
    common += ["--title", title]
    if params.get("source_url"):
        common += ["--source-url", params["source_url"]]

    if mode == "full":
        stages = [("完整精测（全部节点）",
                   [src, "--accurate", "--duration", str(duration)] + common)]
    else:  # fast：快扫全量 → 精测 Top 10
        conc = int(params.get("concurrency") or 6)
        conc = max(1, min(12, conc))
        stages = [(f"① 全量并发快扫（并发{conc}，初筛）",
                   [src, "--sweep", "--sweep-duration", "8", "--concurrency", str(conc),
                    "--limit", str(limit)] + common)]
    return stages, None


def _strip_sweep_args(args):
    """去掉快扫专属参数（及其取值），保留 --limit/--ookla/--title 等通用参数"""
    out = []
    skip = 0
    for a in args:
        if skip:
            skip -= 1
            continue
        if a == "--sweep":               # 布尔 flag，无取值
            continue
        if a in ("--sweep-duration", "--concurrency"):  # 带取值参数，连同取值一起跳过
            skip = 1
            continue
        out.append(a)
    return out


def run_job(params):
    """后台线程：按阶段队列启动 main.py 子进程（快速模式在阶段1完成后动态追加阶段2）"""
    try:
        stages, err = build_stages(params)
        if stages is None:
            STATE["last_result"] = {"error": err}
            return
        STATE["stage_total"] = len(stages)
        idx = 1
        while idx <= len(stages):
            if STATE["cancel"]:
                break
            sname, args = stages[idx - 1]
            STATE["stage"] = sname
            STATE["stage_idx"] = idx
            cmd = [sys.executable, os.path.join(BASE, "main.py")] + args
            with open(LOG, "w", encoding="utf-8") as f:
                STATE["proc"] = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT, cwd=BASE)
            STATE["proc"].wait()
            if STATE["cancel"]:
                break
            # 快速模式：阶段1（快扫）完成后，取结果生成过滤订阅并追加阶段2（Top N 精测）
            if idx == 1 and params.get("mode", "fast") != "full":
                fast_n = int(params.get("fast_top") or 10)
                rj = newest_result(STATE.get("started_at") or 0)
                if rj:
                    sub, cnt = build_fast_sub(stages[0][1][0], rj, fast_n)
                    if cnt > 0:
                        tail = _strip_sweep_args(stages[0][1])[1:]  # 去掉 src 与快扫参数
                        stages.append((f"② Top {cnt} 节点精测（权威数据）",
                                       [sub, "--accurate",
                                        "--duration", str(int(params.get("duration") or 15))] + tail))
                        STATE["stage_total"] = len(stages)
            idx += 1
    except Exception as e:
        STATE["last_result"] = {"error": f"启动失败: {e}"}
    finally:
        STATE["running"] = False
        STATE["proc"] = None


def newest_result(after_ts=0):
    cands = []
    for n in os.listdir(BASE):
        if n.startswith("result_") and n.endswith(".json") and "report" not in n and "deep" not in n:
            p = os.path.join(BASE, n)
            m = os.path.getmtime(p)
            if m >= after_ts:
                cands.append((m, p))
    if not cands:
        return None
    cands.sort()
    return cands[-1][1]


def log_tail(n=60):
    try:
        with open(LOG, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
        return lines[-n:]
    except Exception:
        return []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif path == "/api/status":
            prog = None
            if os.path.exists(PROGRESS):
                try:
                    with open(PROGRESS, "r", encoding="utf-8") as f:
                        prog = json.load(f)
                except Exception:
                    pass
            res = {"running": STATE["running"], "progress": prog,
                   "log": log_tail(50), "error": (STATE.get("last_result") or {}).get("error"),
                   "stage": STATE.get("stage", ""), "stage_idx": STATE.get("stage_idx", 0),
                   "stage_total": STATE.get("stage_total", 1)}
            self._send(200, json.dumps(res, ensure_ascii=False))
        elif path == "/api/result":
            rj = newest_result((STATE.get("started_at") or 0) - 5)
            if not rj:
                self._send(200, json.dumps({"ok": False, "msg": "尚无结果"}))
                return
            try:
                data = json.load(open(rj, "r", encoding="utf-8"))
                import integrity
                import report
                stamp = data.get("timestamp", "")
                meta = {"duration": data.get("duration_s", 0), "threads": 4,
                        "time": datetime.datetime.strptime(stamp, "%Y%m%d_%H%M%S")
                        if len(stamp) == 14 else datetime.datetime.now(),
                        "rounds": data.get("rounds", [])}
                warnings = integrity.analyze(data.get("summary", {}), meta)
                q, qmsg = integrity.verdict(warnings)
                png = os.path.join(BASE, f"result_report_{stamp}.png")
                png_b64 = None
                if os.path.exists(png):
                    png_b64 = base64.b64encode(open(png, "rb").read()).decode()
                html_path = os.path.join(BASE, f"result_report_{stamp}.html")
                out = {"ok": True, "timestamp": stamp, "quality": q, "qmsg": qmsg,
                       "warnings": warnings, "png_b64": png_b64,
                       "report_html": os.path.basename(html_path) if os.path.exists(html_path) else None,
                       "rows": report.build_rows(data)}
                self._send(200, json.dumps(out, ensure_ascii=False))
            except Exception as e:
                self._send(200, json.dumps({"ok": False, "msg": str(e)}))
        elif path == "/report.html":
            rj = newest_result((STATE.get("started_at") or 0) - 5)
            if not rj:
                self._send(404, "no report")
                return
            stamp = json.load(open(rj, "r", encoding="utf-8")).get("timestamp", "")
            hp = os.path.join(BASE, f"result_report_{stamp}.html")
            if os.path.exists(hp):
                self._send(200, open(hp, "rb").read(), "text/html; charset=utf-8")
            else:
                self._send(404, "report not generated yet")
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/api/run":
            if STATE["running"]:
                self._send(200, json.dumps({"ok": False, "msg": "已有测试在运行，请先停止"}))
                return
            try:
                ln = int(self.headers.get("Content-Length", 0))
                params = json.loads(self.rfile.read(ln).decode("utf-8"))
            except Exception as e:
                self._send(200, json.dumps({"ok": False, "msg": f"参数解析失败: {e}"}))
                return
            STATE["running"] = True
            STATE["started_at"] = time.time()
            STATE["last_result"] = None
            STATE["cancel"] = False
            STATE["job_id"] = f"job{int(time.time())}"
            threading.Thread(target=run_job, args=(params,), daemon=True).start()
            self._send(200, json.dumps({"ok": True, "job_id": STATE["job_id"]}))
        elif path == "/api/cancel":
            STATE["cancel"] = True
            p = STATE.get("proc")
            if p and p.poll() is None:
                try:
                    p.kill()
                except Exception:
                    pass
                STATE["running"] = False
                self._send(200, json.dumps({"ok": True, "msg": "已停止"}))
            else:
                self._send(200, json.dumps({"ok": False, "msg": "没有运行中的测试"}))
        else:
            self._send(404, json.dumps({"error": "not found"}))


PAGE = r"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>机场节点测速 · Web 版</title>
<style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{font-family:'Microsoft YaHei',system-ui,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;padding:20px;max-width:1100px;margin:0 auto}
h1{color:#f8fafc;border-bottom:2px solid #334155;padding-bottom:10px;font-size:22px}
h2{color:#7dd3fc;font-size:16px;margin-top:22px}
.card{background:#1e293b;border-radius:10px;padding:16px 18px;margin:12px 0}
label{display:block;color:#94a3b8;font-size:13px;margin:10px 0 4px}
textarea,input[type=text],select{width:100%;background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:6px;padding:8px;font-size:14px}
textarea{height:64px;font-family:inherit}
.row{display:flex;gap:12px;flex-wrap:wrap}
.row>div{flex:1;min-width:130px}
button{background:#0ea5e9;color:#fff;border:none;border-radius:8px;padding:10px 22px;font-size:15px;cursor:pointer;margin-top:14px}
button:hover{background:#0284c7}
button:disabled{opacity:.5;cursor:not-allowed}
button.red{background:#ef4444}.button.red:hover{background:#dc2626}
.bar{height:10px;background:#0f172a;border-radius:5px;overflow:hidden;margin:8px 0}
.barfill{height:100%;background:linear-gradient(90deg,#0ea5e9,#22c55e);width:0;transition:width .5s}
.meta{color:#94a3b8;font-size:12px}
table{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}
th{background:#0f172a;color:#94a3b8;padding:6px 8px;text-align:center;position:sticky;top:0}
td{padding:5px 8px;border-bottom:1px solid #334155;text-align:center}
td.l{text-align:left}
.gA{color:#22c55e;font-weight:bold}.gB{color:#84cc16;font-weight:bold}.gC{color:#eab308;font-weight:bold}.gD{color:#ef4444;font-weight:bold}.gS{color:#f59e0b;font-weight:bold}
img{max-width:100%;border-radius:8px}
.warn{border-left:4px solid #f59e0b;background:#0f172a;padding:8px 12px;border-radius:4px;font-size:13px;margin:4px 0}
.qA{color:#22c55e}.qB{color:#eab308}.qC{color:#ef4444}
#statusline{font-size:13px;color:#7dd3fc}
.hidden{display:none}
</style></head><body>
<h1>🌐 机场节点测速 <span class="meta">StairSpeedTest 风格 · 防失真 · Web 版</span></h1>

<div class="card">
  <label>① 订阅链接（粘贴机场订阅 URL）</label>
  <textarea id="src" placeholder="https://你的机场订阅链接"></textarea>
  <label>或 上传订阅文件（base64 txt / Clash yaml）</label>
  <input type="file" id="file" accept=".txt,.yaml,.yml,.conf,.raw">
  <div class="row">
    <div><label>测试模式</label><select id="mode"><option value="fast">快速（推荐）：快扫全量+Top10精测</option><option value="full">完整：全部节点逐个精测</option></select></div>
    <div><label>快扫并发数</label><input type="number" id="concurrency" value="6" min="1" max="12" title="并发越高快扫越快，但会分摊带宽（快扫仅初筛，精测不受影响）"></div>
    <div><label>精测持续(秒)</label><input type="number" id="duration" value="15" min="5" max="120"></div>
    <div><label>节点数上限(0=全部)</label><input type="number" id="limit" value="0" min="0"></div>
    <div><label>Top N Ookla 深测(0=关)</label><input type="number" id="ookla" value="0" min="0" max="10"></div>
  </div>
  <div class="meta" id="est">快速模式预计：40 节点机场约 8~10 分钟（快扫初筛 + Top10 精测 15s/节点）；完整模式约 25~40 分钟</div>
  <button id="runbtn" onclick="startRun()">▶ 开始测速</button>
  <button class="red hidden" id="cancelbtn" onclick="cancelRun()">■ 停止</button>
</div>

<div class="card hidden" id="progressCard">
  <h2>⏱ 测试进度</h2>
  <div id="statusline">准备中...</div>
  <div class="bar"><div class="barfill" id="barfill"></div></div>
  <div class="meta" id="pctline"></div>
  <div id="nodeProgress"></div>
  <pre id="logbox" class="meta" style="max-height:120px;overflow:auto;white-space:pre-wrap"></pre>
</div>

<div class="card hidden" id="resultCard">
  <h2>📊 测速结果 <span id="quality" class="meta"></span></h2>
  <div id="warnings"></div>
  <img id="chart" alt="测速图">
  <div class="row" style="margin-top:10px">
    <button onclick="openReport()">📄 打开完整 HTML 报告</button>
  </div>
  <div id="tableWrap"></div>
</div>

<script>
let timer=null, lastStamp=null;
function startRun(){
  const params={duration:+document.getElementById('duration').value||15,
    limit:+document.getElementById('limit').value||0,
    mode:document.getElementById('mode').value,
    concurrency:+document.getElementById('concurrency').value||6,
    ookla:+document.getElementById('ookla').value||0,
    source:document.getElementById('src').value.trim()};
  const f=document.getElementById('file').files[0];
  if(!params.source && !f){alert('请先填写订阅链接或选择订阅文件');return;}
  if(f){
    const rd=new FileReader();
    rd.onload=()=>{params.file_b64=rd.result.split(',')[1];params.filename=f.name;doRun(params);};
    rd.readAsDataURL(f);return;
  }
  doRun(params);
}
function doRun(params){
  fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(params)})
   .then(r=>r.json()).then(j=>{
     if(!j.ok){alert(j.msg);return;}
     document.getElementById('runbtn').disabled=true;
     document.getElementById('cancelbtn').classList.remove('hidden');
     document.getElementById('progressCard').classList.remove('hidden');
     document.getElementById('resultCard').classList.add('hidden');
     lastStamp=null;
     timer=setInterval(poll,1000);
   });
}
function cancelRun(){
  fetch('/api/cancel',{method:'POST'}).then(r=>r.json()).then(j=>{if(j.ok){clearInterval(timer);stopUI();}});
}
function poll(){
  fetch('/api/status').then(r=>r.json()).then(s=>{
    const p=s.progress||{};
    const stageTxt=s.stage?` · 阶段 ${s.stage_idx}/${s.stage_total}：${s.stage}`:'';
    document.getElementById('statusline').textContent=
      (s.running?'▶ 运行中':'⏹ 已结束')+stageTxt+' · '+(p.phase||'…')+' · '+(p.message||'');
    document.getElementById('barfill').style.width=(p.pct||0)+'%';
    document.getElementById('pctline').textContent=
      '进度 '+p.done+'/'+p.total+' · '+p.pct+'% · 已用 '+p.elapsed_s+'s'+(p.eta_s?' · 预计剩余 '+p.eta_s+'s':'');
    const rows=Object.entries(p.nodes||{}).map(([n,d])=>
      `<tr><td class=l>${n}</td><td>${d.latency??'-'}</td><td>${d.loss??'-'}</td><td>${d.avg??'-'}</td><td>${d.multi??'-'}</td><td>${d.stalls??'-'}</td><td>${d.status}</td></tr>`).join('');
    document.getElementById('nodeProgress').innerHTML=
      rows?`<table><thead><tr><th>节点</th><th>延迟ms</th><th>丢包%</th><th>单MB/s</th><th>多MB/s</th><th>断流</th><th>状态</th></tr></thead><tbody>${rows}</tbody></table>`:'';
    document.getElementById('logbox').textContent=(s.log||[]).join('\n');
    if(!s.running){
      clearInterval(timer);stopUI();
      loadResult();
    }
  }).catch(()=>{});
}
function stopUI(){
  document.getElementById('runbtn').disabled=false;
  document.getElementById('cancelbtn').classList.add('hidden');
}
function loadResult(){
  fetch('/api/result').then(r=>r.json()).then(d=>{
    if(!d.ok){document.getElementById('statusline').textContent='无结果'+(d.msg?': '+d.msg:'');return;}
    lastStamp=d.timestamp;
    document.getElementById('resultCard').classList.remove('hidden');
    document.getElementById('quality').innerHTML=`数据质量 <span class="q${d.quality}">${d.quality}级 · ${d.qmsg}</span>`;
    document.getElementById('warnings').innerHTML=(d.warnings||[]).map(w=>`<div class="warn">${w}</div>`).join('');
    if(d.png_b64){document.getElementById('chart').src='data:image/png;base64,'+d.png_b64;}
    const rows=(d.rows||[]).map((r,i)=>`<tr><td>${i+1}</td><td class=l>${r.name}</td>
      <td>${r.lat??'-'}</td><td>${r.loss??'-'}</td><td><b>${r.avg.toFixed(2)}</b></td>
      <td>${r.multi?r.multi.toFixed(2):'-'}</td><td>${r.min.toFixed(2)}</td><td>${r.stalls}</td>
      <td><span class="g${r.grade}">${r.score} ${r.grade}</span></td><td class=l>${r.verdict}</td></tr>`).join('');
    document.getElementById('tableWrap').innerHTML=
      `<h2>明细</h2><table><thead><tr><th>#</th><th>节点</th><th>延迟ms</th><th>丢包%</th><th>单MB/s</th><th>多MB/s</th><th>最低MB/s</th><th>断流</th><th>评分</th><th>评估</th></tr></thead><tbody>${rows}</tbody></table>`;
  });
}
function openReport(){
  if(lastStamp){window.open('/report.html','_blank');}else{alert('暂无报告');}
}
</script></body></html>"""


def main():
    print("=" * 58)
    print("  机场节点测速 · Web 版  (StairSpeedTest 风格 · 防失真)")
    print("=" * 58)
    print("\n  >> 测速服务已启动：")
    print("\n      http://127.0.0.1:%d\n" % PORT)
    print("  在浏览器打开上面的链接，粘贴订阅链接/上传文件即可测速。")
    print("  关闭本窗口 = 停止服务。\n")
    threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        p = STATE.get("proc")
        if p and p.poll() is None:
            try:
                p.kill()
            except Exception:
                pass
        print("\n服务已停止")


if __name__ == "__main__":
    main()
