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
import glob
import json
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import engine  # noqa: E402  (RUNTIME_DIR/冻结路径统一从 engine 取)

FROZEN = bool(getattr(sys, "frozen", False))
# 运行时产物（日志/进度/上传/结果）全部进临时运行目录：结果默认不落项目目录，
# 且 exe 模式下父/子进程共享同一路径（_MEIPASS 每次启动都不同）
WORK = engine.WORK_DIR
LOG = os.path.join(engine.RUNTIME_DIR, "web_run.log")
PROGRESS = os.path.join(engine.RUNTIME_DIR, "progress.json")
UPLOAD = os.path.join(engine.RUNTIME_DIR, "uploads")
PORT = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8787

os.makedirs(WORK, exist_ok=True)
os.makedirs(UPLOAD, exist_ok=True)

STATE = {"running": False, "proc": None, "started_at": None, "job_id": None,
         "last_result": None, "cancel": False, "stage": "", "stage_idx": 0,
         "stage_total": 1, "report": None}

# 测速源预设：默认走引擎防失真列表（Google 大文件优先 + 多级兜底）；
# 选预设/自定义时该源成为唯一主源（结果归因明确，不再混兜底数字）。
# 防重连失真：预设源一律用大文件（≥500MB）；小文件在精测时长内会下完→重连→触发机场连接限流
TEST_SOURCE_PRESETS = {
    "cachefly": "http://cachefly.cachefly.net/200mb.test",
    "ovh": "http://proof.ovh.net/files/10Gb.dat",
    "tele2": "http://speedtest.tele2.net/1000MB.zip",
}


def _find_emby_proxies(base, ua):
    """探测 Emby 服务器连通性：先直连，失败依次试本地常见代理端口
    （公益服域名常被墙，用户开着 Clash Verge/v2rayN 时走它们的混合端口即可）。
    返回可用的 proxies dict（直连成功返回 None）。"""
    import requests

    def probe(proxies):
        r = requests.get(f"{base}/emby/System/Info/Public",
                         headers={"User-Agent": ua}, proxies=proxies, timeout=8)
        r.raise_for_status()

    try:
        probe(None)
        return None
    except Exception:
        pass
    for port in (7897, 7890, 7899, 10809, 1080):
        px = {"http": f"http://127.0.0.1:{port}", "https": f"http://127.0.0.1:{port}"}
        try:
            probe(px)
            print(f"[*] Emby 直连不通，已改走本地代理 127.0.0.1:{port}", flush=True)
            return px
        except Exception:
            continue
    raise RuntimeError("无法连接 Emby 服务器：直连失败，且本地常见代理端口"
                       "(7897/7890/7899/10809/1080)都不通——请确认代理客户端开着，"
                       "或服务器地址是否填对")


def resolve_emby_test_url(server, username, password=""):
    """Emby/Jellyfin 服务器 → 自动登录 → 找库里最大的影片 → 返回可直接下载的测速直链。

    只需服务器地址 + 账号（公益服通常无密码），视频文件地址由 API 自动发现：
    1) POST /emby/Users/AuthenticateByName 拿 AccessToken（以 Hills 客户端身份登录，
       与用户实际看片的通道一致；禁"网页播放"的服务器拦的是浏览器，不拦客户端 API）
    2) GET /emby/Users/{uid}/Items?SortBy=Size 找最大文件
    3) /emby/Videos/{id}/stream?Static=true&api_key=... 即原始文件直链
    4) 先探测下载 1MB 确认可用，被拒则给出可读错误
    返回 {"url": 直链, "name": 片名, "size_gb": 大小, "ua": 应使用的UA}。"""
    import requests

    server = (server or "").strip().rstrip("/")
    if not server.startswith(("http://", "https://")):
        server = "http://" + server
    base = server[:-len("/emby")] if server.lower().endswith("/emby") else server
    ua = "Hills/2.2 (Android 14; okhttp4)"
    auth_hdr = ('MediaBrowser Client="Hills", Device="Android Phone", '
                'DeviceId="speedtest-local", Version="2.2"')
    # 连通性：直连 → 本地代理回退（公益服域名常需代理才可达）
    px = _find_emby_proxies(base, ua)

    r = requests.post(f"{base}/emby/Users/AuthenticateByName",
                      headers={"X-Emby-Authorization": auth_hdr,
                               "Content-Type": "application/json",
                               "User-Agent": ua},
                      json={"Username": username or "", "Pw": password or ""},
                      proxies=px, timeout=15)
    if r.status_code in (401,):
        raise RuntimeError("账号或密码错误（HTTP 401）")
    if r.status_code >= 400:
        raise RuntimeError(f"登录失败 HTTP {r.status_code}（检查服务器地址是否正确）")
    token = r.json().get("AccessToken")
    uid = (r.json().get("User") or {}).get("Id")
    if not token or not uid:
        raise RuntimeError("登录响应缺少 AccessToken/User.Id")

    r2 = requests.get(f"{base}/emby/Users/{uid}/Items",
                      params={"Recursive": "true", "IncludeItemTypes": "Movie,Episode",
                              "SortBy": "Size", "SortOrder": "Descending",
                              "Limit": 8, "Fields": "Size"},
                      headers={"X-Emby-Token": token, "User-Agent": ua},
                      proxies=px, timeout=20)
    if r2.status_code >= 400:
        raise RuntimeError(f"媒体库查询失败 HTTP {r2.status_code}")
    items = r2.json().get("Items") or []
    if not items:
        raise RuntimeError("媒体库为空（或账号无权访问）")
    best = max(items, key=lambda it: it.get("Size") or 0)
    size = best.get("Size") or 0
    vid = best["Id"]

    # 直链探测：公益服拦截姿势各异（封 api_key 参数 / 要 X-Emby-Token 头 / 要 MediaSourceId），
    # 依次尝试多种形式，哪个通用哪个。返回 url（可能带 token 头）
    candidates = [
        ("api_key参数", f"{base}/emby/Videos/{vid}/stream?Static=true&api_key={token}", None),
        ("api_key+MediaSourceId",
         f"{base}/emby/Videos/{vid}/stream?Static=true&api_key={token}&MediaSourceId={vid}", None),
        ("X-Emby-Token头",
         f"{base}/emby/Videos/{vid}/stream?Static=true", {"X-Emby-Token": token}),
        ("stream.mp4后缀",
         f"{base}/emby/Videos/{vid}/stream.mp4?Static=true&api_key={token}", None),
    ]
    url = token_hdr = None
    first_err = ""
    for label, u, extra_hdr in candidates:
        hdrs = {"User-Agent": ua, "Range": "bytes=0-1048575"}
        if extra_hdr:
            hdrs.update(extra_hdr)
        got = 0
        try:
            pr = requests.get(u, headers=hdrs, stream=True, proxies=px, timeout=25)
            if pr.status_code >= 400:
                body = ""
                try:
                    body = pr.text[:120]
                except Exception:
                    pass
                if not first_err:
                    first_err = f"HTTP {pr.status_code}{(' ' + body) if body else ''}"
                pr.close()
                continue
            for chunk in pr.iter_content(65536):
                got += len(chunk)
                if got >= 1048576:
                    break
            pr.close()
            if got >= 65536:
                url, token_hdr = u, extra_hdr
                print(f"[*] Emby 直链可用（{label}，探测 {got // 1024}KB）", flush=True)
                break
        except Exception as e:
            if not first_err:
                first_err = str(e)[:80]
    if url is None:
        raise RuntimeError(f"直链探测全部失败（{first_err}）——该服务器对直链下载限制严格，"
                           f"无法用它测带宽；请改回默认测速源做相对对比")

    return {"url": url, "name": best.get("Name") or best["Id"],
            "size_gb": round(size / 1073741824, 1), "ua": ua,
            "token": (token_hdr or {}).get("X-Emby-Token")}


def kill_tree(proc):
    """杀掉整棵进程树：main.py 的子进程（mihomo）不会随 proc.kill() 一起退出（Windows），
    残留的 mihomo 实例会在下次测试时互相抢带宽（失真源，见 integrity.check_mihomo_instances）。"""
    if proc is None:
        return
    try:
        if proc.poll() is not None:
            return
    except Exception:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=15)
            return
        except Exception:
            pass
    else:
        try:
            os.killpg(os.getpgid(proc.pid), 9)
            return
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass


def build_fast_sub(src, result_json, n=10):
    """从快扫结果取 Top N 节点，生成过滤订阅（Clash YAML）供精测阶段使用。

    必须复刻 engine.main 的节点管线（重名去重 + 假节点/占位节点过滤）：
    阶段1 的排名用的是去重后的名称，这里若不做同样处理，
    重名订阅的 Top-N 名称将对不上，导致精测阶段测错/漏测节点。"""
    import engine as E
    # UA 兜底：部分机场按 UA 分发内容，默认 UA 可能被拒（返回提示而非节点）。
    # 必须与 engine.main 的兜底逻辑一致，否则 URL 订阅的 Top-N 精测阶段会静默缺失。
    AUTO_UAS = ["clash-verge/v2.0.2", "v2rayNG/1.8.10", "mihomo/1.19.29",
                "sing-box/1.9.0", "NekoBox/1.3.0"]
    text = ""
    if str(src).startswith(("http://", "https://")):
        for ua in [E.DEFAULT_UA] + AUTO_UAS:
            try:
                text, _raw, _info = E.fetch_subscription(src, ua)
                if "不支持" not in text[:2000] and "请换用" not in text[:2000]:
                    break
            except Exception:
                continue
    else:
        text, _raw, _info = E.load_subscription(src, E.DEFAULT_UA)
    proxies = E.parse_subscription_text(text)
    if not proxies and "proxies:" in text[:2000]:
        import yaml
        proxies = yaml.safe_load(text).get("proxies", [])
    proxies = E.dedupe_names(proxies)
    proxies = [p for p in proxies
               if str(p.get("server", "")).strip() not in ("127.0.0.1", "localhost", "0.0.0.0")]
    proxies = [p for p in proxies
               if not any(k in p.get("name", "") for k in ("请选择", "选择节点", "请换用"))]
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
    top_n = max(1, min(30, int(params.get("fast_top") or 5)))
    rounds = max(1, min(12, int(params.get("rounds") or 1)))
    interval = max(0, min(180, int(params.get("interval_min") if params.get("interval_min") is not None else 5)))
    title = (params.get("title") or "机场节点测速").strip() or "机场节点测速"
    mode = params.get("mode") or "fast"
    # 多轮：精测重复 N 轮（覆盖晚高峰波动），引擎自动汇总均值/一致性出综合报告
    loop_args = ["--loop", str(rounds), "--interval-min", str(interval)] if rounds > 1 else []
    round_txt = f"×{rounds}轮" if rounds > 1 else ""
    common = []
    if limit:
        common += ["--limit", str(limit)]
    common += ["--title", title]
    if params.get("source_url"):
        common += ["--source-url", params["source_url"]]
    # Ookla 深测只挂在最终精测阶段：挂在快扫阶段会随阶段重复跑两遍，
    # 且快扫排名是分摊带宽的初筛，用它选深测对象没有意义。
    ookla_args = ["--ookla", str(ookla)] if ookla else []

    # 测速源：自定义/预设 → 该源成为唯一主源（机场对不同源限速不同，归因要明确）
    src_preset = params.get("test_source") or "default"
    url_args = []
    if src_preset in TEST_SOURCE_PRESETS:
        url_args = ["--test-url", TEST_SOURCE_PRESETS[src_preset]]
    elif src_preset == "custom":
        custom_url = (params.get("test_url") or "").strip()
        if custom_url:
            url_args = ["--test-url", custom_url]
    if params.get("test_ua"):
        url_args += ["--test-ua", params["test_ua"]]  # Emby 源：以客户端身份下载
    if params.get("test_token"):
        url_args += ["--test-token", params["test_token"]]  # Emby 头认证直链

    if mode == "full":
        stages = [(f"完整精测（全部节点）{round_txt}",
                   [src, "--accurate", "--duration", str(duration)] + loop_args + ookla_args + url_args + common)]
    else:  # fast / emby：快扫全量初筛，阶段2 由 run_job 动态追加（--test-url 经 tail 透传）
        conc = int(params.get("concurrency") or 6)
        conc = max(1, min(12, conc))
        stages = [(f"① 全量并发快扫（并发{conc}，初筛）",
                   [src, "--sweep", "--sweep-duration", "8", "--concurrency", str(conc),
                    "--last-round-concurrency", str(conc),  # 单轮快扫时引擎会用 last-round-concurrency 覆盖并发数
                    "--limit", str(limit)] + url_args + common)]
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
        if a in ("--sweep-duration", "--concurrency", "--last-round-concurrency"):  # 带取值参数，连同取值一起跳过
            skip = 1
            continue
        out.append(a)
    return out


def run_job(params):
    """后台线程：按阶段队列启动子进程（快速/Emby 模式在阶段1完成后动态追加阶段2）。

    冻结(exe)模式下没有 main.py 文件，用 `[exe, --cli, 参数]` 启动；
    脚本模式沿用 `[python, main.py, 参数]`。"""
    try:
        # Emby 源：自动登录找最大影片直链（用户只需填服务器地址+账号）
        if (params.get("test_source") or "") == "emby":
            try:
                info = resolve_emby_test_url(params.get("emby_server"),
                                             params.get("emby_user"),
                                             params.get("emby_pass"))
                params["test_url"] = info["url"]
                params["test_ua"] = info.get("ua")  # 以 Hills 客户端身份下载
                if info.get("token"):
                    params["test_token"] = info["token"]  # 头认证方案的直链
                params["test_source"] = "custom"  # 复用自定义源通道
                STATE["emby_file"] = info
                print(f"[*] Emby 测速源就绪: {info['name']} ({info['size_gb']}GB)", flush=True)
            except Exception as e:
                STATE["last_result"] = {"error": f"Emby 服务器解析失败: {e}"}
                return
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
            if FROZEN:
                cmd = [sys.executable, "--cli"] + args
            else:
                cmd = [sys.executable, os.path.join(BASE, "main.py")] + args
            with open(LOG, "w", encoding="utf-8") as f:
                STATE["proc"] = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT, cwd=BASE)
            STATE["proc"].wait()
            if STATE["cancel"]:
                break
            # 阶段1（快扫）完成后：取结果生成过滤订阅，按模式追加阶段2
            if idx == 1 and params.get("mode", "fast") != "full":
                mode = params.get("mode", "fast")
                fast_n = max(1, min(30, int(params.get("fast_top") or 5)))
                rounds = max(1, min(12, int(params.get("rounds") or 1)))
                interval = max(0, min(180, int(params.get("interval_min") if params.get("interval_min") is not None else 5)))
                loop_args = ["--loop", str(rounds), "--interval-min", str(interval)] if rounds > 1 else []
                round_txt = f"×{rounds}轮" if rounds > 1 else ""
                rj = newest_result(STATE.get("started_at") or 0)
                print(f"[run_job] 阶段1完成: rj={rj}", flush=True)
                if rj:
                    try:
                        sub, cnt = build_fast_sub(stages[0][1][0], rj, fast_n)
                        print(f"[run_job] build_fast_sub: sub={sub} cnt={cnt}", flush=True)
                    except Exception as e:
                        sub, cnt = None, 0
                        print(f"[run_job] build_fast_sub 异常: {e}", flush=True)
                        STATE["last_result"] = {"error": f"精测订阅构建失败: {e}"}
                    if cnt > 0:
                        tail = _strip_sweep_args(stages[0][1])[1:]  # 去掉 src 与快扫参数
                        ookla = int(params.get("ookla") or 0)
                        if ookla:
                            tail += ["--ookla", str(ookla)]  # 深测只在精测阶段跑
                        if mode == "emby":
                            streams = max(1, min(4, int(params.get("emby_streams") or 2)))
                            edur = max(15, min(180, int(params.get("emby_duration") or 60)))
                            stages.append((f"② Top {cnt} Emby 压测（单路{edur}s + {streams}路并发{edur}s）{round_txt}",
                                           [sub, "--sweep", "--sweep-duration", "6",
                                            "--concurrency", "3",
                                            "--emby", str(cnt),
                                            "--emby-streams", str(streams),
                                            "--emby-duration", str(edur)] + loop_args + tail))
                        else:
                            stages.append((f"② Top {cnt} 节点精测（权威数据）{round_txt}",
                                           [sub, "--accurate",
                                            "--duration", str(int(params.get("duration") or 15))] + loop_args + tail))
                        STATE["stage_total"] = len(stages)
            idx += 1
    except Exception as e:
        STATE["last_result"] = {"error": f"启动失败: {e}"}
    finally:
        kill_tree(STATE.get("proc"))   # 异常/取消路径下 mihomo 子进程也要一并清掉
        STATE["running"] = False
        STATE["proc"] = None
        finalize_result()


def finalize_result():
    """任务收尾：把最终报告（PNG/HTML/明细）读进内存 → 删除临时结果文件（默认不落盘）。

    报告文件由 main.py 阶段生成在临时目录；若缺失（异常路径）则此刻按需渲染。"""
    try:
        rj = newest_result((STATE.get("started_at") or 0) - 5)
        if not rj:
            STATE["report"] = None
            return
        import integrity
        import report
        data = report.load_result(rj)
        rows = report.build_rows(data)
        stamp = data.get("timestamp", datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
        meta = {"duration": data.get("duration_s", 0), "threads": 4,
                "time": (datetime.datetime.strptime(stamp, "%Y%m%d_%H%M%S")
                         if len(stamp) == 14 else datetime.datetime.now()),
                "rounds": data.get("rounds", []),
                "emby": data.get("emby", [])}
        warnings = integrity.analyze(data.get("summary", {}), meta)
        q, qmsg = integrity.verdict(warnings)
        png = os.path.join(engine.RUNTIME_DIR, f"result_report_{stamp}.png")
        html = os.path.join(engine.RUNTIME_DIR, f"result_report_{stamp}.html")
        if not os.path.exists(png):
            report.render_png(data, rows, warnings, png, "机场节点测速报告")
        if not os.path.exists(html):
            report.render_html(data, rows, warnings, png, html, "机场节点测速报告", "")
        png_b64 = None
        if os.path.exists(png):
            with open(png, "rb") as f:
                png_b64 = base64.b64encode(f.read()).decode()
        html_str = ""
        if os.path.exists(html):
            with open(html, "r", encoding="utf-8") as f:
                html_str = f.read()
        STATE["report"] = {"ok": True, "timestamp": stamp, "quality": q, "qmsg": qmsg,
                           "warnings": warnings, "png_b64": png_b64, "html": html_str,
                           "userinfo": engine.fmt_userinfo(data.get("userinfo") or {}),
                           "test_url": data.get("test_url") or "默认防失真列表",
                           "emby_file": STATE.get("emby_file"),
                           "emby": data.get("emby", []), "rows": rows}
    except Exception as e:
        STATE["report"] = None
        STATE["last_result"] = {"error": f"报告生成失败: {e}"}
    finally:
        engine.cleanup_runtime(keep_progress=True)  # 结果不落盘：删临时文件，留进度供 UI 收尾轮询


def newest_result(after_ts=0):
    cands = []
    for n in os.listdir(engine.RUNTIME_DIR):
        if n.startswith("result_") and n.endswith(".json") and "report" not in n and "deep" not in n:
            p = os.path.join(engine.RUNTIME_DIR, n)
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
            rep = STATE.get("report")
            if not rep or not rep.get("ok"):
                self._send(200, json.dumps({"ok": False, "msg": "尚无结果"}, ensure_ascii=False))
                return
            self._send(200, json.dumps(rep, ensure_ascii=False))
        elif path == "/report.html":
            rep = STATE.get("report") or {}
            if rep.get("html"):
                self._send(200, rep["html"], "text/html; charset=utf-8")
            else:
                self._send(404, "no report")
        elif path == "/api/download":
            # 结果默认不落盘：报告想留就点下载，浏览器存到用户选择的位置
            rep = STATE.get("report") or {}
            if rep.get("html"):
                stamp = rep.get("timestamp", "")
                data = rep["html"].encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Disposition",
                                 f"attachment; filename=测速报告_{stamp}.html")
                self.end_headers()
                self.wfile.write(data)
            else:
                self._send(404, json.dumps({"error": "no report"}))
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
                kill_tree(p)   # 连同 mihomo 子进程整树清理，防孤儿实例抢带宽
                STATE["running"] = False
                self._send(200, json.dumps({"ok": True, "msg": "已停止"}))
            else:
                self._send(200, json.dumps({"ok": False, "msg": "没有运行中的测试"}))
        else:
            self._send(404, json.dumps({"error": "not found"}))


PAGE = r"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>机场节点测速 · Web 版</title>
<style>
:root{
  color-scheme:dark;
  --bg:#05070f; --panel:rgba(148,163,184,.055); --panel-brd:rgba(148,163,184,.13);
  --txt:#e7eef9; --dim:#8a97ac; --faint:#5c6a80;
  --acc:#38bdf8; --acc2:#8b5cf6; --ok:#34d399; --warn:#fbbf24; --bad:#f87171; --gold:#f5c451;
  --grad:linear-gradient(135deg,#38bdf8,#8b5cf6);
  --field:rgba(10,18,38,.60);
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{
  font-family:-apple-system,"Segoe UI Variable Display","Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  background:var(--bg);color:var(--txt);margin:0;padding:26px 18px 60px;line-height:1.55;
  background-image:
    radial-gradient(1000px 520px at -10% -12%,rgba(56,189,248,.20),transparent 62%),
    radial-gradient(1100px 600px at 110% 6%,rgba(139,92,246,.18),transparent 62%),
    radial-gradient(900px 560px at 50% 118%,rgba(52,211,153,.10),transparent 60%);
  background-attachment:fixed;
}
.wrap{max-width:1180px;margin:0 auto;position:relative}
.wrap::before{content:"";position:absolute;top:-22px;left:8%;right:8%;height:1px;
  background:linear-gradient(90deg,transparent,rgba(125,211,252,.55),rgba(167,139,250,.55),transparent)}
::selection{background:rgba(56,189,248,.30)}
::-webkit-scrollbar{width:9px;height:9px}
::-webkit-scrollbar-thumb{background:rgba(148,163,184,.22);border-radius:99px}
::-webkit-scrollbar-thumb:hover{background:rgba(148,163,184,.38)}
::-webkit-scrollbar-track{background:transparent}

/* ── 头部 ── */
header{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;margin:6px 2px 22px}
h1{
  margin:0;font-size:28px;font-weight:800;letter-spacing:.5px;
  background:linear-gradient(120deg,#7dd3fc 10%,#a78bfa 55%,#f0abfc 90%);
  -webkit-background-clip:text;background-clip:text;color:transparent;
  filter:drop-shadow(0 0 18px rgba(125,211,252,.25));
}
.ver{font-size:11px;font-weight:600;padding:4px 12px;border-radius:99px;
  background:rgba(56,189,248,.16);color:#9fdcff;border:1px solid rgba(125,211,252,.45);
  box-shadow:0 0 14px rgba(56,189,248,.15)}
.tagline{color:var(--dim);font-size:13px;flex-basis:100%;margin-top:-6px}

/* ── 卡片 ── */
.card{
  background:linear-gradient(170deg,rgba(148,163,184,.10),rgba(148,163,184,.045) 55%);
  border:1px solid rgba(148,163,184,.17);border-radius:18px;
  padding:22px 24px;margin:16px 0;backdrop-filter:blur(16px);
  box-shadow:0 20px 55px rgba(0,0,0,.45),inset 0 1px 0 rgba(255,255,255,.06);
}
h2{color:var(--txt);font-size:16px;font-weight:700;margin:26px 0 12px;display:flex;align-items:center;gap:10px}
h2::before{content:"";width:4px;height:16px;border-radius:4px;background:var(--grad)}

/* ── 表单 ── */
label{display:block;color:var(--dim);font-size:11.5px;font-weight:600;letter-spacing:.8px;
  text-transform:uppercase;margin:12px 0 6px}
textarea,input[type=text],input[type=number],select{
  width:100%;background:var(--field);color:var(--txt);
  border:1px solid rgba(148,163,184,.16);border-radius:11px;padding:10px 13px;font-size:14px;
  transition:border .18s,box-shadow .18s;font-family:inherit}
textarea{height:66px;resize:vertical}
textarea:focus,input:focus,select:focus{
  outline:none;border-color:rgba(56,189,248,.55);box-shadow:0 0 0 3px rgba(56,189,248,.14)}
select{appearance:none;-webkit-appearance:none;cursor:pointer;padding-right:32px;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8'%3E%3Cpath d='M1 1l5 5 5-5' stroke='%238a97ac' stroke-width='1.6' fill='none' stroke-linecap='round'/%3E%3C/svg%3E");
  background-repeat:no-repeat;background-position:right 12px center}
input[type=file]{width:100%;color:var(--dim);font-size:13px;margin-top:2px}
input[type=file]::file-selector-button{
  background:rgba(148,163,184,.10);color:var(--txt);border:1px solid rgba(148,163,184,.18);
  border-radius:9px;padding:7px 14px;margin-right:12px;cursor:pointer;font-family:inherit;transition:background .15s}
input[type=file]::file-selector-button:hover{background:rgba(148,163,184,.18)}
.row{display:grid;grid-template-columns:repeat(auto-fit,minmax(158px,1fr));gap:12px 14px}
.meta{color:var(--dim);font-size:12.5px}

/* ── 按钮 ── */
button{
  border:none;border-radius:12px;padding:12px 26px;font-size:14.5px;font-weight:600;
  cursor:pointer;font-family:inherit;letter-spacing:.3px;transition:transform .15s,box-shadow .15s,background .15s}
button:hover{transform:translateY(-1px)}
button:disabled{opacity:.45;cursor:not-allowed;transform:none}
.primary{
  background:linear-gradient(135deg,#22a8ee 5%,#6d7cf8 55%,#9d6cf6 95%);color:#fff;
  box-shadow:0 12px 32px rgba(90,120,250,.40),inset 0 1px 0 rgba(255,255,255,.25)}
.primary:hover{box-shadow:0 16px 40px rgba(90,120,250,.55),inset 0 1px 0 rgba(255,255,255,.30);filter:saturate(1.15)}
.ghost{background:rgba(148,163,184,.08);color:var(--txt);border:1px solid rgba(148,163,184,.16)}
.ghost:hover{background:rgba(148,163,184,.15)}
.danger{background:rgba(248,113,113,.12);color:#fca5a5;border:1px solid rgba(248,113,113,.30)}
.btnrow{display:flex;gap:12px;flex-wrap:wrap;margin-top:18px}
.hidden{display:none}

/* ── 进度 ── */
#statusline{font-size:13.5px;color:#9fd3f5;display:flex;align-items:center;gap:9px;flex-wrap:wrap}
#statusline::before{content:"";width:8px;height:8px;border-radius:99px;background:var(--ok);
  box-shadow:0 0 10px var(--ok);animation:pulse 1.6s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
.bar{height:12px;border-radius:99px;background:rgba(148,163,184,.10);overflow:hidden;margin:14px 0 8px;
  box-shadow:inset 0 1px 3px rgba(0,0,0,.4)}
.barfill{height:100%;width:0;border-radius:99px;transition:width .6s ease;
  background:linear-gradient(90deg,#38bdf8,#818cf8,#c084fc);
  background-size:200% 100%;animation:flow 2.4s linear infinite;
  box-shadow:0 0 16px rgba(99,140,250,.55)}
@keyframes flow{to{background-position:-200% 0}}
#pctline{font-size:13px;color:var(--dim);font-variant-numeric:tabular-nums}
#logbox{max-height:130px;overflow:auto;white-space:pre-wrap;font-size:11.5px;
  color:#6f7d94;background:rgba(2,6,16,.45);border:1px solid rgba(148,163,184,.09);
  border-radius:10px;padding:10px 12px;margin-top:12px;font-family:Consolas,Menlo,monospace}

/* ── 表格 ── */
.tblwrap{overflow:auto;border:1px solid rgba(148,163,184,.11);border-radius:13px;margin-top:10px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{
  background:rgba(8,13,26,.92);backdrop-filter:blur(8px);color:var(--dim);
  padding:9px 10px;text-align:center;font-size:11px;font-weight:700;letter-spacing:.7px;
  text-transform:uppercase;position:sticky;top:0;z-index:2;
  border-bottom:1px solid rgba(148,163,184,.14);white-space:nowrap}
td{padding:8px 10px;border-bottom:1px solid rgba(148,163,184,.075);text-align:center;
  font-variant-numeric:tabular-nums;white-space:nowrap}
tbody tr{transition:background .12s}
tbody tr:hover{background:rgba(56,189,248,.055)}
td.l{text-align:left;max-width:300px;overflow:hidden;text-overflow:ellipsis}
#detailTable th{cursor:pointer;user-select:none}
#detailTable th:hover{color:var(--txt)}
#detailTable th::after{content:"⇅";opacity:.35;font-size:10px;margin-left:3px}

/* ── 评分/质量徽章 ── */
.gS,.gA,.gB,.gC,.gD{display:inline-block;padding:2.5px 10px;border-radius:99px;
  font-size:11.5px;font-weight:700;letter-spacing:.4px}
.gS{background:rgba(245,196,81,.14);color:#f5c451;border:1px solid rgba(245,196,81,.35)}
.gA{background:rgba(52,211,153,.12);color:#34d399;border:1px solid rgba(52,211,153,.30)}
.gB{background:rgba(163,211,53,.12);color:#b8d43a;border:1px solid rgba(163,211,53,.28)}
.gC{background:rgba(251,191,36,.12);color:#fbbf24;border:1px solid rgba(251,191,36,.30)}
.gD{background:rgba(248,113,113,.12);color:#f87171;border:1px solid rgba(248,113,113,.30)}
.qA{color:#34d399;font-weight:700}.qB{color:#fbbf24;font-weight:700}.qC{color:#f87171;font-weight:700}

/* ── 概览统计 ── */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(136px,1fr));gap:12px;margin:14px 0}
.stat{
  background:linear-gradient(165deg,rgba(148,163,184,.09),rgba(148,163,184,.03));
  border:1px solid var(--panel-brd);border-radius:15px;padding:14px 10px;text-align:center;
  transition:transform .15s,border .15s}
.stat:hover{transform:translateY(-2px);border-color:rgba(56,189,248,.30)}
.sv{font-size:21px;font-weight:800;font-variant-numeric:tabular-nums;
  background:linear-gradient(120deg,#dbeafe,#7dd3fc);-webkit-background-clip:text;background-clip:text;color:transparent}
.sk{font-size:11px;color:var(--dim);margin-top:4px;letter-spacing:.6px}

/* ── Top3 领奖台 ── */
#topcards{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:14px;margin:14px 0}
.topcard{position:relative;border-radius:16px;padding:16px 16px 13px;
  border:1px solid transparent;
  background:
    linear-gradient(165deg,rgba(18,26,46,.92),rgba(9,14,28,.94)) padding-box,
    linear-gradient(135deg,rgba(148,163,184,.35),rgba(148,163,184,.10)) border-box;
  box-shadow:0 16px 40px rgba(0,0,0,.38)}
.t0{background:
    linear-gradient(165deg,rgba(38,30,14,.92),rgba(16,13,8,.94)) padding-box,
    linear-gradient(135deg,#f5c451,#92610a) border-box;
  box-shadow:0 18px 46px rgba(245,196,81,.14)}
.t1{background:
    linear-gradient(165deg,rgba(20,28,46,.92),rgba(9,14,28,.94)) padding-box,
    linear-gradient(135deg,#cbd5e1,#5c6a80) border-box}
.t2{background:
    linear-gradient(165deg,rgba(36,26,18,.92),rgba(14,10,8,.94)) padding-box,
    linear-gradient(135deg,#d19a5a,#7c4a1a) border-box}
.rank{position:absolute;top:12px;right:13px;font-size:10.5px;font-weight:800;padding:3px 10px;border-radius:99px}
.t0 .rank{background:rgba(245,196,81,.16);color:#f5c451}
.t1 .rank{background:rgba(203,213,225,.13);color:#cbd5e1}
.t2 .rank{background:rgba(209,154,90,.15);color:#d19a5a}
.tcname{font-size:14.5px;font-weight:700;margin:2px 0 8px;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;padding-right:52px}
.tcrow{font-size:12.5px;color:#a8b4c8;margin:4px 0;font-variant-numeric:tabular-nums}
.tcver{font-size:12px;color:#7dd3fc;margin-top:7px;font-weight:600}

/* ── 告警 / 图 ── */
.warn{
  border-left:3px solid;border-image:linear-gradient(180deg,#fbbf24,#f87171) 1;
  background:rgba(251,191,36,.055);padding:9px 14px;border-radius:8px;font-size:13px;margin:5px 0;
  color:#d9c58f}
img{max-width:100%;border-radius:14px;border:1px solid rgba(148,163,184,.12);margin-top:8px}

.empty{padding:44px 20px;text-align:center;color:var(--dim);font-size:13.5px}
.empty .big{font-size:34px;display:block;margin-bottom:8px;opacity:.6}
</style></head><body>
<div class="wrap">

<header>
  <h1>机场节点测速</h1>
  <span class="ver">v2.2 · 全新界面</span>
  <div class="tagline">Top5 精测 · 多轮汇总 · Emby 压测 · 数据防失真 —— 粘贴订阅，一键出综合报告</div>
</header>

<div class="card">
  <label>① 订阅来源</label>
  <textarea id="src" placeholder="粘贴机场订阅 URL（https://…）"></textarea>
  <label>或上传订阅文件</label>
  <input type="file" id="file" accept=".txt,.yaml,.yml,.conf,.raw">
  <label>② 测试参数</label>
  <div class="row">
    <div><label>测试模式</label><select id="mode">
      <option value="fast">快速（推荐）· 快扫+Top5精测</option>
      <option value="emby">Emby 压力 · 快扫+晚高峰压测</option>
      <option value="full">完整 · 全部节点逐个精测</option>
    </select></div>
    <div><label>快扫并发</label><input type="number" id="concurrency" value="6" min="1" max="12" title="并发越高快扫越快，但会分摊带宽（快扫仅初筛，精测不受影响）"></div>
    <div><label>精测时长(秒)</label><input type="number" id="duration" value="15" min="5" max="120"></div>
    <div><label>Top N 精测</label><input type="number" id="fast_top" value="5" min="1" max="30" title="快扫后对评分前 N 的节点做权威精测"></div>
    <div><label>测试轮数</label><input type="number" id="rounds" value="1" min="1" max="12" title="精测重复轮数：多轮覆盖晚高峰波动，最后自动汇总均值/一致性出综合报告"></div>
    <div><label>轮间隔(分)</label><input type="number" id="interval_min" value="5" min="0" max="180"></div>
    <div><label>节点上限(0=全部)</label><input type="number" id="limit" value="0" min="0"></div>
    <div><label>Ookla 深测(0=关)</label><input type="number" id="ookla" value="0" min="0" max="10"></div>
    <div><label>测速源</label><select id="test_source" title="机场可能对不同源限速不同：测速源数字低但 Emby 快，就换源。最准是 Emby 服务器模式——直接测你看片走的真实路径">
      <option value="default">默认 · Google CDN 防失真列表</option>
      <option value="emby">Emby 服务器（自动找大影片，最准）</option>
      <option value="cachefly">Cachefly 100MB</option>
      <option value="ovh">OVH 10GB</option>
      <option value="tele2">Tele2 200MB</option>
      <option value="custom">自定义 URL</option>
    </select></div>
  </div>
  <div class="row" id="embySourceRow" style="display:none">
    <div><label>Emby 服务器地址</label><input type="text" id="emby_server" placeholder="https://你的emby服务器（不用填视频地址，自动找）"></div>
    <div><label>用户名</label><input type="text" id="emby_user" placeholder="Emby 账号（公益服常见无密码）"></div>
    <div><label>密码（可空）</label><input type="password" id="emby_pass" placeholder="没有就留空"></div>
  </div>
  <div class="row" id="customUrlRow" style="display:none">
    <div><label>自定义测速 URL</label><input type="text" id="test_url" placeholder="http://大文件直链（建议 ≥500MB；此源将成为唯一测速源）"></div>
  </div>
  <div class="row" id="embyRow" style="display:none">
    <div><label>Emby 并发路数</label><input type="number" id="emby_streams" value="2" min="1" max="4" title="= 同时观看的设备数"></div>
    <div><label>压测时长(秒)</label><input type="number" id="emby_duration" value="60" min="30" max="180" title="建议≥60s：晚高峰QoS限速常在持续流量几十秒后才触发"></div>
  </div>
  <div class="meta" id="est" style="margin-top:12px">快速：40 节点约 4~6 分钟 · Emby 压力 +15~25 分钟 · 完整约 25~40 分钟 · 多轮按轮数叠加 · 结果页可下载综合报告</div>
  <div class="btnrow">
    <button class="primary" id="runbtn" onclick="startRun()">🚀 开始测速</button>
    <button class="danger hidden" id="cancelbtn" onclick="cancelRun()">■ 停止</button>
  </div>
</div>

<div class="card hidden" id="progressCard">
  <h2>测试进度</h2>
  <div id="statusline">准备中...</div>
  <div class="bar"><div class="barfill" id="barfill"></div></div>
  <div id="pctline"></div>
  <div id="nodeProgress"></div>
  <pre id="logbox"></pre>
</div>

<div class="card hidden" id="resultCard">
  <h2>测速结果 <span id="quality" class="meta"></span></h2>
  <div class="meta" id="uinfo" style="margin:0 0 4px"></div>
  <div class="stats" id="overview"></div>
  <div id="topcards"></div>
  <div id="warnings"></div>
  <img id="chart" alt="测速图">
  <div class="btnrow">
    <button class="ghost" onclick="window.open('/report.html','_blank')">📄 完整报告</button>
    <button class="ghost" onclick="location.href='/api/download'">⬇ 下载报告</button>
    <button class="ghost" id="copybtn" onclick="copyTop()">📋 复制 Top 节点</button>
  </div>
  <div id="embyWrap"></div>
  <div id="tableWrap"></div>
</div>

</div>

<script>
let timer=null, lastRows=null;
document.getElementById('mode').addEventListener('change',e=>{
  document.getElementById('embyRow').style.display=e.target.value==='emby'?'grid':'none';
});
document.getElementById('test_source').addEventListener('change',e=>{
  document.getElementById('customUrlRow').style.display=e.target.value==='custom'?'grid':'none';
  document.getElementById('embySourceRow').style.display=e.target.value==='emby'?'grid':'none';
});
function startRun(){
  const params={duration:+document.getElementById('duration').value||15,
    limit:+document.getElementById('limit').value||0,
    mode:document.getElementById('mode').value,
    concurrency:+document.getElementById('concurrency').value||6,
    ookla:+document.getElementById('ookla').value||0,
    fast_top:+document.getElementById('fast_top').value||5,
    rounds:+document.getElementById('rounds').value||1,
    interval_min:+document.getElementById('interval_min').value||0,
    emby_streams:+document.getElementById('emby_streams').value||2,
    emby_duration:+document.getElementById('emby_duration').value||60,
    test_source:document.getElementById('test_source').value,
    test_url:document.getElementById('test_url').value.trim(),
    emby_server:document.getElementById('emby_server').value.trim(),
    emby_user:document.getElementById('emby_user').value.trim(),
    emby_pass:document.getElementById('emby_pass').value,
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
     lastRows=null;
     timer=setInterval(poll,1000);
   }).catch(()=>{alert('无法连接测速服务：这是旧标签页或服务已停止。\n请关闭本标签页，重新双击 exe 后使用新弹出的页面。');});
}
function cancelRun(){
  fetch('/api/cancel',{method:'POST'}).then(r=>r.json()).then(j=>{if(j.ok){clearInterval(timer);stopUI();}});
}
function poll(){
  fetch('/api/status').then(r=>r.json()).then(s=>{
    const p=s.progress||{};
    const sl=document.getElementById('statusline');
    if(s.error){
      sl.innerHTML='<span style="color:#f87171;font-weight:bold">✖ '+s.error+'</span>';
    }else{
      const stageTxt=s.stage?` · 阶段 ${s.stage_idx}/${s.stage_total}：${s.stage}`:'';
      sl.textContent=(s.running?'▶ 运行中':'⏹ 已结束')+stageTxt+' · '+(p.phase||'…')+' · '+(p.message||'')
        +((p.total_rounds>1)?` · 第 ${p.round}/${p.total_rounds} 轮`:'');
    }
    document.getElementById('barfill').style.width=(p.pct||0)+'%';
    document.getElementById('pctline').textContent=
      '进度 '+p.done+'/'+p.total+' · '+p.pct+'% · 已用 '+p.elapsed_s+'s'+(p.eta_s?' · 预计剩余 '+p.eta_s+'s':'');
    const rows=Object.entries(p.nodes||{}).map(([n,d])=>
      `<tr><td class=l>${n}</td><td>${d.latency??'-'}</td><td>${d.loss??'-'}</td><td>${d.avg??'-'}</td><td>${d.multi??'-'}</td><td>${d.stalls??'-'}</td><td>${d.score??'-'}</td><td>${d.status}</td></tr>`).join('');
    document.getElementById('nodeProgress').innerHTML=
      rows?`<div class=tblwrap><table><thead><tr><th>节点</th><th>延迟ms</th><th>丢包%</th><th>单MB/s</th><th>多MB/s</th><th>断流</th><th>评分</th><th>状态</th></tr></thead><tbody>${rows}</tbody></table></div>`:'';
    document.getElementById('logbox').textContent=(s.log||[]).join('\n');
    if(!s.running){
      clearInterval(timer);stopUI();
      loadResult();
    }
  }).catch(()=>{
    document.getElementById('statusline').innerHTML=
      '<span style="color:#f87171;font-weight:bold">⚠ 连不上测速服务——若是旧标签页请刷新本页（或重新双击 exe）</span>';
  });
}
function stopUI(){
  document.getElementById('runbtn').disabled=false;
  document.getElementById('cancelbtn').classList.add('hidden');
}
function loadResult(){
  fetch('/api/result').then(r=>r.json()).then(d=>{
    if(!d.ok){const sl=document.getElementById('statusline');
      if(!sl.innerHTML.includes('✖')){sl.textContent='无结果'+(d.msg?': '+d.msg:'');}
      return;}
    lastRows=d.rows||[];
    document.getElementById('resultCard').classList.remove('hidden');
    document.getElementById('quality').innerHTML=`数据质量 <span class="q${d.quality}">${d.quality}级 · ${d.qmsg}</span>`;
    const ef=d.emby_file;
    document.getElementById('uinfo').textContent=(d.userinfo?d.userinfo+' · ':'')+
      '测速源: '+(ef?`Emby·${ef.name} (${ef.size_gb}GB)`:(''+(d.test_url||'默认')));
    renderOverview(d);
    document.getElementById('warnings').innerHTML=(d.warnings||[]).map(w=>`<div class="warn">${w}</div>`).join('');
    if(d.png_b64){document.getElementById('chart').src='data:image/png;base64,'+d.png_b64;}
    const emby=(d.emby||[]).filter(e=>e.phaseA);
    document.getElementById('embyWrap').innerHTML=emby.length?
      `<h2>Emby 晚高峰压力测试</h2><div class=tblwrap><table><thead><tr><th>节点</th><th>单路avg</th><th>P10卡顿线</th><th>限速比</th><th>每路MB/s</th><th>最差路</th><th>判定</th></tr></thead><tbody>`+
      emby.slice().sort((a,b)=>b.phaseA.avg-a.phaseA.avg).map(e=>{
        const pa=e.phaseA,pb=e.phaseB||{};
        const per=(pb.per_stream||[]).map(x=>x.toFixed(1)).join(' / ')||'-';
        const th=pa.throttle!=null?(pa.throttle<0.7
          ?`<span style="color:#f87171;font-weight:bold">⚠${Math.round(pa.throttle*100)}%</span>`
          :Math.round(pa.throttle*100)+'%'):'-';
        return `<tr><td class=l>${e.name}</td><td><b>${pa.avg.toFixed(1)}</b></td>`+
          `<td>${pa.p10!=null?pa.p10.toFixed(1):'-'}</td><td>${th}</td>`+
          `<td>${per}</td><td><b>${pb.worst!=null?pb.worst.toFixed(1):'-'}</b></td><td class=l>${e.verdict||''}</td></tr>`;
      }).join('')+`</tbody></table></div><div class="meta" style="margin-top:8px">限速比<70% = 持续流量被限速（长片源越看越卡）；最差一路决定多设备同时观看的档位</div>`:'';
    const rows=(d.rows||[]).map((r,i)=>`<tr><td>${i+1}</td><td class=l>${r.name}</td>
      <td>${r.lat??'-'}</td><td>${r.loss??'-'}</td><td><b>${r.avg.toFixed(2)}</b></td>
      <td>${r.multi?r.multi.toFixed(2):'-'}</td><td>${r.min.toFixed(2)}</td><td>${r.stalls}</td>
      <td>${r.cons!=null?r.cons.toFixed(2):'-'}</td><td>${r.rounds??'-'}</td>
      <td><span class="g${r.grade}">${r.score} ${r.grade}</span></td><td class=l>${r.verdict}</td></tr>`).join('');
    document.getElementById('tableWrap').innerHTML=
      `<h2>明细（多轮数据为各轮汇总均值，点击表头排序）</h2><div class=tblwrap><table id="detailTable"><thead><tr><th>#</th><th>节点</th><th>延迟ms</th><th>丢包%</th><th>单MB/s</th><th>多MB/s</th><th>最低MB/s</th><th>断流</th><th>一致性</th><th>轮次</th><th>评分</th><th>评估</th></tr></thead><tbody>${rows}</tbody></table></div>`;
    makeSortable(document.getElementById('detailTable'));
  });
}
function renderOverview(d){
  const rows=d.rows||[];
  if(!rows.length){document.getElementById('overview').innerHTML='';document.getElementById('topcards').innerHTML='';return;}
  const roundsMax=Math.max(...rows.map(r=>r.rounds||1));
  const top=rows[0];
  const avgAll=(rows.reduce((s,r)=>s+r.avg,0)/rows.length).toFixed(1);
  const embyBest=(d.emby||[]).filter(e=>e.phaseA).sort((a,b)=>b.phaseA.avg-a.phaseA.avg)[0];
  const stats=[['可用节点',rows.length],['测试轮数',roundsMax],['Top1 速度',top.avg.toFixed(1)+' MB/s'],['平均速度',avgAll+' MB/s']];
  if(embyBest){stats.push(['Emby 最佳',embyBest.verdict||'-']);}
  stats.push(['数据质量',d.quality+'级']);
  document.getElementById('overview').innerHTML=stats.map(([k,v])=>
    `<div class=stat><div class=sv>${v}</div><div class=sk>${k}</div></div>`).join('');
  document.getElementById('topcards').innerHTML=rows.slice(0,3).map((r,i)=>`<div class="topcard t${i}">
    <div class=rank>#${i+1}</div><div class=tcname title="${r.name}">${r.name}</div>
    <div><span class="badge g${r.grade}">${r.grade}${r.score}</span></div>
    <div class=tcrow>单线程 <b>${r.avg.toFixed(1)}</b> MB/s · 最低 ${r.min.toFixed(1)}</div>
    <div class=tcrow>延迟 ${r.lat??'-'} ms · 丢包 ${r.loss??'-'}% · 断流 ${r.stalls}</div>
    <div class=tcrow>${(r.rounds||1)>1?`多轮 ×${r.rounds} 一致性 ${(r.cons!=null?r.cons.toFixed(2):'-')} · `:''}${r.multi?'多线程 '+r.multi.toFixed(1)+' MB/s':'无多线程数据'}</div>
    <div class=tcver>${r.verdict}</div></div>`).join('');
}
function makeSortable(t){
  if(!t)return;
  let sortIdx=-1,sortDir=1;
  t.querySelectorAll('th').forEach((th,i)=>th.onclick=()=>{
    if(sortIdx===i){sortDir*=-1;}else{sortIdx=i;sortDir=1;}
    const tb=t.tBodies[0];
    [...tb.rows].sort((a,b)=>{
      const nx=parseFloat(a.cells[i].innerText),ny=parseFloat(b.cells[i].innerText);
      return (isNaN(nx)||isNaN(ny))?a.cells[i].innerText.localeCompare(b.cells[i].innerText)*sortDir:(nx-ny)*sortDir;
    }).forEach(r=>tb.appendChild(r));
  });
}
function copyTop(){
  if(!lastRows){alert('暂无结果');return;}
  const names=lastRows.slice(0,10).map(r=>r.name).join('\n');
  navigator.clipboard.writeText(names).then(()=>{alert('已复制 Top10 节点名：\n'+names);},()=>{alert('复制失败，请手动选择');});
}
</script></body></html>"""


def pick_port(start, tries=10):
    """端口被占用时自动向后找空闲端口（旧实例还在跑时，双击 exe 不会崩）"""
    for p in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return start + tries


def _apply_runtime_dir(port):
    """按端口隔离运行目录：多实例（多开 exe / 旧实例未退）互不清理对方的进度/结果。
    环境变量注入后，子进程（main.py --cli）与父进程共享同一隔离目录。"""
    import tempfile
    rd = os.path.join(tempfile.gettempdir(), f"airport-speedtest-{port}")
    os.environ["AST_RUNTIME_DIR"] = rd
    engine.RUNTIME_DIR = rd
    engine.WORK_DIR = os.path.join(rd, "work")
    return rd


def main():
    # exe 模式子进程入口：exe --cli <main.py 的参数> → 转发给命令行版主流程
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        sys.argv = ["main.py"] + sys.argv[2:]
        import main as cli_main
        cli_main.main()
        return
    global WORK, LOG, PROGRESS, UPLOAD
    port = pick_port(PORT)
    if port != PORT:
        print(f"[!] 端口 {PORT} 被占用（可能已有测速实例在运行），改用 {port}")
    # 防互删：按端口隔离运行目录（多实例各自独立），并注入环境变量让子进程共享同一目录
    rd = _apply_runtime_dir(port)
    WORK = engine.WORK_DIR
    LOG = os.path.join(rd, "web_run.log")
    PROGRESS = os.path.join(rd, "progress.json")
    UPLOAD = os.path.join(rd, "uploads")
    os.makedirs(WORK, exist_ok=True)
    os.makedirs(UPLOAD, exist_ok=True)
    print("=" * 58)
    print("  机场节点测速 · Web 版 v2.1  (Top5精测 · 多轮汇总 · Emby压力 · 防失真)")
    print("=" * 58)
    print("\n  >> 测速服务已启动：")
    print(f"\n      http://127.0.0.1:{port}\n")
    print("  在浏览器打开上面的链接，粘贴订阅链接/上传文件即可测速。")
    print("  关闭本窗口 = 停止服务。\n")
    threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        kill_tree(STATE.get("proc"))
        print("\n服务已停止")


if __name__ == "__main__":
    main()
