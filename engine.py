#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
airport_speedtest.py — 机场订阅节点提取 + 延迟/下行速度测试工具
================================================================
原理：
  1. 下载机场订阅（支持 base64 URI 订阅 和 Clash YAML 订阅）
  2. 解析提取全部节点（ss / vmess / vless / trojan / ssr）
  3. 自动下载 mihomo (Clash Meta) 内核加载节点
  4. 逐个节点：先测延迟（generate_204），再真实下载测速文件测下行速度
  5. 输出表格 + 保存 JSON/CSV 结果

用法：
  python airport_speedtest.py <订阅链接> [选项]

选项：
  --size-mb N        每个节点测速下载量，默认 20 MB
  --max-time S       每个节点测速最长耗时，默认 40 秒
  --test-url URL     自定义测速文件 URL（默认按 Cloudflare/Tele2/OVH/Cachefly 依次尝试）
  --latency-timeout  延迟测试超时(ms)，默认 5000
  --latency-url URL  延迟测试 URL，默认 http://www.gstatic.com/generate_204
  --ua STR           请求订阅时用的 User-Agent
  --list-only        只提取并列出节点，不做测速（无需 mihomo）
  --keep             测速结束后保留 mihomo 进程与工作目录（默认清理）
  --no-download      不自动下载 mihomo（已存在时使用）
  --selftest         离线自检：解析内置样例 URI，验证解析器

示例：
  python airport_speedtest.py "https://example.com/api/v1/client/subscribe?token=xxxx"
  python airport_speedtest.py "https://example.com/sub?token=xxx" --size-mb 50 --list-only
"""
import argparse
import base64
import csv
import datetime
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import zipfile
from concurrent.futures import ThreadPoolExecutor

try:
    import requests
    import yaml
except ImportError:
    print("[错误] 需要安装 requests 和 pyyaml：pip install requests pyyaml")
    sys.exit(1)

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR = os.path.join(TOOL_DIR, "tools")
WORK_DIR = os.path.join(TOOL_DIR, "work")
MIHOMO_EXE = os.path.join(TOOLS_DIR, "mihomo.exe")
DEFAULT_UA = "ClashForWindows/0.20.39"
MIHOMO_API = "https://api.github.com/repos/MetaCubeX/mihomo/releases/latest"

SAMPLE_URIS = [
    "ss://YWVzLTI1Ni1nY206cGFzc3dvcmQxMjM=@1.2.3.4:8388#SS%E5%9F%BA%E7%A1%80",
    "ss://YWVzLTI1Ni1nY206cGFzc3dvcmQxMjM=@1.2.3.4:8388?plugin=obfs-local%3Bobfs%3Dhttp%3Bobfs-host%3Dwww.bing.com#SS-ObFS",
    "vmess://eyJ2IjoiMiIsInBzIjoiVk1lc3Mt6IGU6YCaIiwiYWRkIjoiNS42LjcuOCIsInBvcnQiOiI0NDMiLCJpZCI6InV1aWQtdXVpZC11dWlkLXV1aWQiLCJhaWQiOiIwIiwic2N5IjoiYXV0byIsIm5ldCI6IndzIiwidHlwZSI6Im5vbmUiLCJob3N0IjoiY2RuLmV4YW1wbGUuY29tIiwicGF0aCI6Ii92MnJheSIsInRscyI6InRscyIsInNuaSI6ImNkbi5leGFtcGxlLmNvbSJ9",
    "trojan://password123@9.9.9.9:443?sni=trojan.example.com&type=ws&path=%2Ftr&host=trojan.example.com#Trojan-WS",
    "vless://uuid-1111@8.8.8.8:443?encryption=none&security=reality&sni=www.microsoft.com&fp=chrome&pbk=publickey&sid=shortid&flow=xtls-rprx-vision&type=tcp#VLESS-Reality",
    "vless://uuid-2222@7.7.7.7:8443?encryption=none&security=tls&type=grpc&serviceName=test&sni=grpc.example.com#VLESS-gRPC",
    "ssr://MS4yLjMuNDo0NDM6YXV0aF9hZXMxMjhfbWQ1OmFlcy0yNTYtY2ZiOnRsczEuMl90aWNrZXRfYXV0aDpZV1J0YVdVeVpYUXhNakF3TncvP29iZnNwYXJhbT1iM0JsYmkxMGVYQmwmcmVtYXJrcz1TUlLmtYHkuqTmg4XlhrXkv53lkbw=",
]

# ---------------------------------------------------------------- 订阅下载

def fetch_subscription(url, ua):
    headers = {"User-Agent": ua}
    r = requests.get(url, headers=headers, timeout=30, allow_redirects=True)
    r.raise_for_status()
    return r.text, r.content

def load_subscription(arg, ua):
    """输入可以是订阅链接(http/https) 或本地文件路径(也支持 file:// 前缀)"""
    if arg.startswith(("http://", "https://")):
        return fetch_subscription(arg, ua)
    path = arg
    if arg.startswith("file://"):
        path = arg[len("file://"):]
        if path.startswith("/") and len(path) > 2 and path[2] == ":":
            path = path[1:]  # file:///D:/x → D:/x
    if not os.path.exists(path):
        raise FileNotFoundError(f"文件不存在: {path}")
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read(), None

# ---------------------------------------------------------------- 节点解析

def b64decode_safe(s):
    s = s.strip()
    if not s:
        return ""
    try:
        if isinstance(s, str):
            s = s.encode("utf-8")
        s += b"=" * (-len(s) % 4)
        return base64.b64decode(s).decode("utf-8", "ignore")
    except Exception:
        return ""

def parse_ss(uri, name):
    rest = uri[len("ss://"):]
    frag = ""
    if "#" in rest:
        rest, frag = rest.split("#", 1)
    frag = urllib.parse.unquote(frag)
    plugin = None
    if "?" in rest:
        rest, qs = rest.split("?", 1)
        params = urllib.parse.parse_qs(qs)
        if params.get("plugin"):
            plugin = params["plugin"][0]
    # base64 形式: base64(method:password)@host:port
    if "@" in rest:
        userinfo, hostport = rest.rsplit("@", 1)
        dec = b64decode_safe(userinfo)
        if ":" not in dec:
            dec = userinfo  # 明文形式 method:password@host:port
        if ":" not in dec:
            return None
        method, password = dec.split(":", 1)
    else:
        # 明文形式 method:password@host:port 已在上面处理；这里处理纯 base64 整串
        dec = b64decode_safe(rest)
        if ":" not in dec or "@" not in dec:
            return None
        method, password = dec.split(":", 1)
        hostport = dec.split("@", 1)[1]
    if ":" not in hostport:
        return None
    host, port = hostport.rsplit(":", 1)
    node = {"name": name or frag or f"{host}:{port}", "type": "ss",
            "server": host, "port": int(port), "cipher": method, "password": password,
            "udp": True}
    if plugin:
        parts = plugin.split(";")
        pname = parts[0].strip()
        if pname in ("obfs-local", "simple-obfs"):
            node["plugin"] = "obfs"
            opts = {}
            for p in parts[1:]:
                if "=" in p:
                    k, v = p.split("=", 1)
                    opts[k] = v
            node["plugin-opts"] = {"mode": opts.get("obfs", "http"), "host": opts.get("obfs-host", "")}
    return node

def parse_vmess(uri, name):
    raw = uri[len("vmess://"):]
    try:
        data = json.loads(b64decode_safe(raw))
    except Exception:
        try:
            data = json.loads(raw)
        except Exception:
            return None
    host = data.get("add", "")
    port = data.get("port", "")
    if not host or not port:
        return None
    node = {"name": name or data.get("ps") or f"{host}:{port}", "type": "vmess",
            "server": host, "port": int(port), "uuid": data.get("id", ""),
            "alterId": int(data.get("aid", 0) or 0),
            "cipher": data.get("scy") or "auto", "udp": True}
    net = data.get("net", "tcp")
    tls = data.get("tls") == "tls"
    node["tls"] = tls
    if tls:
        node["servername"] = data.get("sni") or host
    if net == "ws":
        node["network"] = "ws"
        opts = {}
        if data.get("path"):
            opts["path"] = data["path"]
        h = data.get("host")
        if h:
            opts["headers"] = {"Host": h}
        if opts:
            node["ws-opts"] = opts
    elif net == "grpc":
        node["network"] = "grpc"
        if data.get("path"):
            node["grpc-opts"] = {"grpc-service-name": data["path"]}
    elif net == "h2":
        node["network"] = "h2"
        opts = {}
        if data.get("path"):
            opts["path"] = data["path"]
        if data.get("host"):
            opts["host"] = data["host"]
        node["h2-opts"] = opts
    else:
        node["network"] = "tcp"
    return node

def parse_vless(uri, name):
    rest = uri[len("vless://"):]
    frag = ""
    if "#" in rest:
        rest, frag = rest.split("#", 1)
    frag = urllib.parse.unquote(frag)
    if "@" not in rest:
        return None
    uuid, hostport = rest.rsplit("@", 1)
    if "?" in hostport:
        hostport, q = hostport.split("?", 1)
    else:
        q = ""
    host, port = hostport.rsplit(":", 1)
    params = urllib.parse.parse_qs(q)
    get = lambda k, d="": params.get(k, [d])[-1]
    node = {"name": name or frag or f"{host}:{port}", "type": "vless",
            "server": host, "port": int(port), "uuid": uuid, "udp": True}
    sec = get("security", "")
    if sec in ("tls", "reality", "xtls"):
        node["tls"] = True
        if get("sni"):
            node["servername"] = get("sni")
    if sec == "reality":
        ro = {}
        if get("pbk"):
            ro["public-key"] = get("pbk")
        if get("sid"):
            ro["short-id"] = get("sid")
        if get("fp"):
            ro["fingerprint"] = get("fp")
        if ro:
            node["reality-opts"] = ro
    if get("flow"):
        node["flow"] = get("flow")
    if get("alpn"):
        node["alpn"] = [a for a in get("alpn").split(",") if a]
    net = get("type", "tcp")
    if net == "ws":
        node["network"] = "ws"
        opts = {}
        if get("path"):
            opts["path"] = get("path")
        if get("host"):
            opts["headers"] = {"Host": get("host")}
        if opts:
            node["ws-opts"] = opts
    elif net == "grpc":
        node["network"] = "grpc"
        if get("serviceName"):
            node["grpc-opts"] = {"grpc-service-name": get("serviceName")}
    elif net == "h2":
        node["network"] = "h2"
        opts = {}
        if get("path"):
            opts["path"] = get("path")
        if get("host"):
            opts["host"] = get("host")
        node["h2-opts"] = opts
    else:
        node["network"] = "tcp"
    return node

def parse_trojan(uri, name):
    rest = uri[len("trojan://"):]
    frag = ""
    if "#" in rest:
        rest, frag = rest.split("#", 1)
    frag = urllib.parse.unquote(frag)
    if "@" not in rest:
        return None
    password, hostport = rest.rsplit("@", 1)
    if "?" in hostport:
        hostport, q = hostport.split("?", 1)
    else:
        q = ""
    host, port = hostport.rsplit(":", 1)
    params = urllib.parse.parse_qs(q)
    get = lambda k, d="": params.get(k, [d])[-1]
    node = {"name": name or frag or f"{host}:{port}", "type": "trojan",
            "server": host, "port": int(port), "password": password, "udp": True}
    if get("sni"):
        node["sni"] = get("sni")
    if get("allowInsecure", "").lower() == "1":
        node["skip-cert-verify"] = True
    net = get("type", "tcp")
    if net == "ws":
        node["network"] = "ws"
        opts = {}
        if get("path"):
            opts["path"] = get("path")
        if get("host"):
            opts["headers"] = {"Host": get("host")}
        if opts:
            node["ws-opts"] = opts
    elif net == "grpc":
        node["network"] = "grpc"
        if get("serviceName"):
            node["grpc-opts"] = {"grpc-service-name": get("serviceName")}
    if get("alpn"):
        node["alpn"] = [a for a in get("alpn").split(",") if a]
    return node

def parse_ssr(uri, name):
    raw = uri[len("ssr://"):]
    dec = b64decode_safe(raw)
    if not dec or ":" not in dec:
        return None
    parts = dec.split(":")
    if len(parts) < 6:
        return None
    host, port, protocol, method, obfs = parts[0], parts[1], parts[2], parts[3], parts[4]
    pass_b64 = ":".join(parts[5:]).split("/?")[0]
    password = b64decode_safe(pass_b64)
    node = {"name": name or f"{host}:{port}", "type": "ssr", "server": host,
            "port": int(port), "cipher": method, "password": password,
            "protocol": protocol, "obfs": obfs, "udp": True}
    if "/?" in dec:
        qs = dec.split("/?", 1)[1]
        params = urllib.parse.parse_qs(qs)
        if params.get("obfsparam"):
            node["obfs-param"] = b64decode_safe(params["obfsparam"][0])
        if params.get("protoparam"):
            node["protocol-param"] = b64decode_safe(params["protoparam"][0])
        if params.get("remarks"):
            node["name"] = name or b64decode_safe(params["remarks"][0])
    return node

def parse_subscription_text(text):
    """解析订阅文本，返回 clash 格式节点列表"""
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            lines.append(line)
    # 单行 base64（整体解码后再分行）
    if len(lines) == 1 and not lines[0].lower().startswith(("ss://", "vmess://", "trojan://", "vless://", "ssr://", "http://", "https://", "proxies:")):
        decoded = b64decode_safe(lines[0])
        if decoded:
            lines = [l.strip() for l in decoded.splitlines() if l.strip()]
    nodes = []
    for line in lines:
        if line.lower().startswith("ss://"):
            n = parse_ss(line, None)
        elif line.lower().startswith("vmess://"):
            n = parse_vmess(line, None)
        elif line.lower().startswith("vless://"):
            n = parse_vless(line, None)
        elif line.lower().startswith("trojan://"):
            n = parse_trojan(line, None)
        elif line.lower().startswith("ssr://"):
            n = parse_ssr(line, None)
        else:
            n = None
        if n:
            nodes.append(n)
    return nodes

def dedupe_names(nodes):
    seen = {}
    for n in nodes:
        base = n["name"]
        if base in seen:
            seen[base] += 1
            n["name"] = f"{base} #{seen[base]}"
        else:
            seen[base] = 1
    return nodes

# ---------------------------------------------------------------- mihomo 内核

def pick_free_port(preferred):
    for p in preferred:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

def ensure_mihomo(allow_download=True):
    if os.path.exists(MIHOMO_EXE):
        return MIHOMO_EXE
    if not allow_download:
        print("[错误] 未找到 mihomo 内核，且 --no-download 已指定。")
        sys.exit(1)
    os.makedirs(TOOLS_DIR, exist_ok=True)
    print("[*] 下载 mihomo (Clash Meta) 内核...")
    r = requests.get(MIHOMO_API, headers={"User-Agent": DEFAULT_UA}, timeout=30)
    r.raise_for_status()
    tag = r.json()["tag_name"]  # 如 v1.19.29
    ver = tag.lstrip("v")
    want = f"mihomo-windows-amd64-v{ver}.zip"
    asset = next((a for a in r.json()["assets"] if a["name"] == want), None)
    if not asset:
        print(f"[错误] 未找到资产 {want}")
        sys.exit(1)
    zip_path = os.path.join(TOOLS_DIR, asset["name"])
    if not os.path.exists(zip_path):
        print(f"[*] 下载 {asset['name']} ({asset['size']//1048576} MB)...")
        dl = requests.get(asset["browser_download_url"], stream=True, timeout=60)
        dl.raise_for_status()
        with open(zip_path, "wb") as f:
            for chunk in dl.iter_content(65536):
                f.write(chunk)
    print("[*] 解压内核...")
    with zipfile.ZipFile(zip_path) as z:
        target = next((m for m in z.namelist()
                       if m.lower().endswith(".exe") and "mihomo" in m.lower()), None)
        if target:
            with z.open(target) as src, open(MIHOMO_EXE, "wb") as dst:
                dst.write(src.read())
        else:
            print("[错误] 压缩包内未找到 mihomo exe")
            sys.exit(1)
    print(f"[+] mihomo 就绪: {MIHOMO_EXE}")
    # GeoIP 库自动补位（mihomo 缺它无法启动）
    geo = os.path.join(TOOLS_DIR, "geoip.metadb")
    if not os.path.exists(geo):
        print("[*] 下载 GeoIP 库 (geoip.metadb)...")
        try:
            r = requests.get("https://github.com/MetaCubeX/meta-rules-dat/releases/download/latest/geoip.metadb",
                             stream=True, timeout=120)
            r.raise_for_status()
            with open(geo, "wb") as f:
                for chunk in r.iter_content(65536):
                    f.write(chunk)
            print(f"[+] GeoIP 就绪: {geo}")
        except Exception as e:
            print(f"[!] GeoIP 下载失败（{str(e)[:60]}），部分功能可能受限")
    return MIHOMO_EXE

# ---------------------------------------------------------------- 配置生成

def build_config(proxies, mixed_port, api_port, listeners=None):
    cfg = {
        "mixed-port": mixed_port,
        "allow-lan": False,
        "bind-address": "*",
        "mode": "global",
        "log-level": "warning",
        "ipv6": False,
        "unified-delay": True,
        "tcp-concurrent": True,
        "external-controller": f"127.0.0.1:{api_port}",
        "profile": {"store-selected": False, "store-fake-ip": False},
        # DNS 模块必须启用，proxy-server-nameserver 才会生效：
        # 节点服务器域名（多为 Cloudflare 前置 punycode 域名）走 DoH 解析，绕过本地 DNS 污染
        "dns": {
            "enable": True,
            "ipv6": False,
            "enhanced-mode": "fake-ip",
            "fake-ip-range": "198.18.0.1/16",
            # 目标域名走国内 DNS（快且稳），国际 DoH 作补充——避免国际线路差时本地解析超时
            "nameserver": [
                "223.5.5.5",
                "119.29.29.29",
                "https://1.1.1.1/dns-query",
                "https://dns.google/dns-query",
            ],
            "fallback": [
                "https://223.5.5.5/dns-query",
                "https://1.12.12.12/dns-query",
            ],
            "proxy-server-nameserver": [
                "https://1.1.1.1/dns-query",
                "https://dns.google/dns-query",
                "https://223.5.5.5/dns-query",      # 阿里 DoH：国际线路差时的国内兜底
                "https://1.12.12.12/dns-query",    # 腾讯 DoH
            ],
        },
        "proxies": proxies,
    }
    if listeners:
        cfg["listeners"] = listeners
    return yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False, default_flow_style=False)

# ---------------------------------------------------------------- 测速引擎

def stream_download(url, proxies, duration, size_bytes, max_time, warmup=0):
    """通用流下载（精准模式含预热）：warmup 秒内数据丢弃（慢启动不计入），
    之后持续下载 duration 秒并每秒采样。返回 (res_dict, err)"""
    read_timeout = max(duration + warmup + 10, max_time)
    t0 = time.time()
    m_start = None          # 测量起点（预热结束后）
    total = 0
    buckets = []
    bucket_bytes = 0
    bucket_start = None
    last_err = None
    reconnects = 0
    r = None
    try:
        r = requests.get(url, proxies=proxies, stream=True, timeout=(10, read_timeout))
        while True:
            if r.status_code != 200:
                last_err = f"HTTP {r.status_code}"
                break
            for chunk in r.iter_content(65536):
                if not chunk:
                    break
                now = time.time()
                if m_start is None:
                    if now - t0 < warmup:
                        continue  # 预热期：数据丢弃
                    m_start = now
                    bucket_start = now
                total += len(chunk)
                bucket_bytes += len(chunk)
                if now - bucket_start >= 1.0:
                    buckets.append(bucket_bytes / (now - bucket_start) / 1048576)
                    bucket_bytes = 0
                    bucket_start = now
                if duration > 0 and now - m_start >= duration:
                    break
                if total >= size_bytes:
                    break
                if now - m_start > max_time:
                    break
            if m_start is None:
                break  # 预热期就断流
            if duration <= 0 or total == 0:
                break
            if time.time() - m_start >= duration:
                break
            # 文件读完但时长未到 → 重新请求继续
            r.close()
            try:
                r = requests.get(url, proxies=proxies, stream=True, timeout=(10, read_timeout))
            except Exception as e:
                last_err = str(e)[:120]
                reconnects += 1
                break
    except Exception as e:
        last_err = str(e)[:120]
    finally:
        try:
            if r is not None:
                r.close()
        except Exception:
            pass
    if m_start is None:
        return None, last_err or "warmup failed"
    elapsed = time.time() - m_start
    if bucket_bytes and elapsed > 0:
        buckets.append(bucket_bytes / max(0.001, time.time() - bucket_start) / 1048576)
    if total == 0:
        return None, last_err or "0 bytes"
    avg = total / 1048576 / elapsed
    # 最低速度排除首秒（连接建立/慢启动的假低谷），对持续播放更有参考意义
    if len(buckets) > 1:
        mn = min(buckets[1:])
    else:
        mn = buckets[0] if buckets else avg
    mn = min(mn, avg)  # 防止异常采样导致 min>avg
    # 断流统计：首秒之后吞吐 <1MB/s 的秒数（模拟播放时的卡顿秒）
    stalls = sum(1 for b in buckets[1:] if b < 1.0) if len(buckets) > 1 else 0
    return {"avg": avg, "min": mn, "max": max(buckets) if buckets else avg,
            "samples": [round(s, 2) for s in buckets],
            "stalls": stalls, "reconnects": reconnects}, None

def stream_download_multi(url, proxies, duration, size_bytes, max_time, threads=4, warmup=0):
    """多线程并行下载（模块级，供串行/并发扫描共用），支持预热丢弃"""
    if duration <= 0:
        duration = max(15.0, size_bytes / 1048576 / 5.0)
    read_timeout = max(duration + warmup + 10, max_time)
    t0 = time.time()
    deadline = t0 + duration + warmup
    buckets = []
    totals = [0] * threads
    lock = threading.Lock()

    def worker(wi):
        local = 0
        m_start = None
        try:
            r = requests.get(url, proxies=proxies, stream=True, timeout=(10, read_timeout))
            while True:
                if r.status_code != 200:
                    break
                for chunk in r.iter_content(65536):
                    if not chunk:
                        break
                    now = time.time()
                    if m_start is None:
                        if now - t0 < warmup:
                            continue
                        m_start = now
                    local += len(chunk)
                    with lock:
                        sec = int(now - t0 - warmup)
                        if sec < 0:
                            sec = 0
                        if sec >= len(buckets):
                            buckets.extend([0.0] * (sec + 1 - len(buckets)))
                        buckets[sec] += len(chunk) / 1048576
                    if now >= deadline:
                        break
                if time.time() >= deadline:
                    break
                r.close()
                try:
                    r = requests.get(url, proxies=proxies, stream=True, timeout=(10, read_timeout))
                except Exception:
                    break
        except Exception:
            pass
        finally:
            totals[wi] = local

    ths = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    elapsed = time.time() - t0 - warmup
    total = sum(totals)
    if total == 0 or elapsed <= 0:
        return None, "multi 0 bytes"
    avg = total / 1048576 / elapsed
    if len(buckets) > 1:
        mn = min(buckets[1:])
    else:
        mn = buckets[0] if buckets else avg
    mn = min(mn, avg)
    stalls = sum(1 for b in buckets[1:] if b < 1.0) if len(buckets) > 1 else 0
    return {"avg": avg, "min": mn, "max": max(buckets) if buckets else avg,
            "samples": [round(s, 2) for s in buckets],
            "stalls": stalls, "reconnects": 0}, None

class SpeedTester:
    def __init__(self, config_path, workdir, mixed_port, api_port, size_mb, max_time,
                 latency_timeout, latency_url, test_urls, threads=4, probes=5):
        self.mixed_port = mixed_port
        self.api_port = api_port
        self.api = f"http://127.0.0.1:{api_port}"
        self.size_bytes = int(size_mb * 1024 * 1024)
        self.max_time = max_time
        self.latency_timeout = latency_timeout
        self.latency_url = latency_url
        self.test_urls = test_urls
        self.threads = threads
        self.probes = probes
        self.proc = None
        self.log_path = os.path.join(workdir, "mihomo.log")
        self.config_path = config_path
        self.workdir = workdir

    def start(self):
        # 防启动失败：确保 GeoIP 库就位（mihomo 缺它会在无网时启动失败）
        if not os.path.exists(os.path.join(self.workdir, "geoip.metadb")):
            bundled = os.path.join(TOOL_DIR, "tools", "geoip.metadb")
            if os.path.exists(bundled):
                try:
                    import shutil
                    shutil.copy(bundled, os.path.join(self.workdir, "geoip.metadb"))
                except Exception:
                    pass
        logf = open(self.log_path, "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [MIHOMO_EXE, "-f", self.config_path, "-d", self.workdir],
            stdout=logf, stderr=subprocess.STDOUT, cwd=self.workdir)
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                r = requests.get(f"{self.api}/version", timeout=2)
                if r.status_code == 200:
                    return True
            except Exception:
                pass
            if self.proc.poll() is not None:
                print("[错误] mihomo 进程退出，日志：")
                print(open(self.log_path, encoding="utf-8", errors="ignore").read()[-2000:])
                return False
            time.sleep(0.5)
        print("[错误] mihomo API 启动超时")
        return False

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def get_proxies(self):
        r = requests.get(f"{self.api}/proxies", timeout=10)
        r.raise_for_status()
        data = r.json()["proxies"]
        group = data.get("GLOBAL", {})
        names = group.get("all", [])
        rows = []
        for name in names:
            if name in ("DIRECT", "REJECT"):
                continue
            info = data.get(name, {})
            rows.append({"name": name, "type": info.get("type", "?"),
                         "server": info.get("server", ""), "port": info.get("port", "")})
        return rows

    def latency_test(self, proxies, jobs=8, probes=None, timeout=None):
        """多次探测测延迟 + 丢包率：每个节点探测 probes 次，统计成功率"""
        probes = probes or self.probes
        timeout = timeout or self.latency_timeout

        def one(p):
            enc = urllib.parse.quote(p["name"], safe="")
            url = f"{self.api}/proxies/{enc}/delay?timeout={timeout}&url={urllib.parse.quote(self.latency_url, safe='')}"
            delays = []
            fails = 0
            for _ in range(probes):
                try:
                    r = requests.get(url, timeout=timeout / 1000 + 5)
                    if r.status_code == 200:
                        d = r.json().get("delay")
                        if d:
                            delays.append(d)
                            continue
                except Exception:
                    pass
                fails += 1
                if fails >= 2 and not delays:
                    break  # 已判死，提前结束
            if delays:
                loss = round(fails / probes * 100)
                return p["name"], {"delay": round(sum(delays) / len(delays)),
                                   "min": min(delays), "max": max(delays),
                                   "loss": loss, "probes": probes}
            return p["name"], None
        result = {}
        with ThreadPoolExecutor(max_workers=jobs) as ex:
            for name, info in ex.map(one, proxies):
                result[name] = info
        return result

    def select(self, name):
        """切换 GLOBAL 到指定节点并【验证生效】。

        实战教训：mihomo 的 GLOBAL 组默认指向 DIRECT——若切换失败，
        测速流量会悄悄走直连，产生“节点很快/很慢”的假数据。
        因此 PUT 之后必须 GET 回读确认 now == name，失败即报错，绝不含糊。
        """
        def _put():
            enc = urllib.parse.quote(name, safe="")
            return requests.put(f"{self.api}/proxies/GLOBAL",
                                json={"name": name}, timeout=10).status_code in (200, 204)

        def _now():
            try:
                return requests.get(f"{self.api}/proxies/GLOBAL", timeout=5).json().get("now")
            except Exception:
                return None

        if not _put():
            return False
        time.sleep(0.3)   # 提速：缩短验证等待（仍强制回读验证，真实性不变）
        if _now() == name:
            return True
        # 重试一次，仍失败则判定为选择未生效（防失真：宁可不测，不可测错）
        if not _put():
            return False
        time.sleep(0.4)
        return _now() == name

    def download_once(self, url, via_proxy=True, duration=0, warmup=0):
        if via_proxy:
            proxies = {"http": f"http://127.0.0.1:{self.mixed_port}",
                       "https": f"http://127.0.0.1:{self.mixed_port}"}
        else:
            proxies = None
        return stream_download(url, proxies, duration, self.size_bytes, self.max_time, warmup=warmup)

    def download_multi(self, url, duration=0, threads=4):
        """多线程并行下载（StairSpeedTest 风格），N 个连接同时拉取，测节点最大吞吐"""
        proxies = {"http": f"http://127.0.0.1:{self.mixed_port}",
                   "https": f"http://127.0.0.1:{self.mixed_port}"}
        return stream_download_multi(url, proxies, duration, self.size_bytes,
                                     self.max_time, threads)

    def speed_test(self, name, duration=0, multi=True):
        if not self.select(name):
            return None, "select failed"
        time.sleep(0.6)
        last_err = None
        for url in self.test_urls:
            res, err = self.download_once(url, duration=duration)
            if res is not None:
                # 单线程为主（贴近流媒体真实场景），额外测多线程最大吞吐
                if self.threads > 1 and multi:
                    mres, merr = self.download_multi(url, duration=duration, threads=self.threads)
                    if mres:
                        res["multi_avg"] = mres["avg"]
                        res["multi_min"] = mres["min"]
                        res["multi_samples"] = mres["samples"]
                    else:
                        res["multi_avg"] = None
                return res, None
            last_err = err
        return None, last_err or "all urls failed"

    def direct_test(self, duration=0, warmup=0):
        """直连基线：不走代理，依次尝试所有测速 URL"""
        last_err = None
        for url in self.test_urls:
            res, err = self.download_once(url, via_proxy=False, duration=duration, warmup=warmup)
            if res is not None:
                return res, None
            last_err = err
        return None, last_err or "all urls failed"

    def accurate_test(self, name, duration=20, warmup=3, threads=4, probes=10):
        """精准模式：串行单节点。10次延迟探测 → 单线程(预热+稳态) → 多线程(预热+稳态)。
        单/多线程先后分开测，互不干扰，得到最真实的数据。"""
        if not self.select(name):
            return None, None, "select failed"
        time.sleep(0.5)
        lats = self.latency_test([{"name": name}], probes=probes)
        info = lats.get(name)
        if not info:
            return None, None, "延迟不通"
        proxies = {"http": f"http://127.0.0.1:{self.mixed_port}",
                   "https": f"http://127.0.0.1:{self.mixed_port}"}
        last_err = None
        res = None
        for url in self.test_urls:
            res, err = stream_download(url, proxies, duration, self.size_bytes,
                                       self.max_time, warmup=warmup)
            if res is not None:
                res["source"] = url
                break
            last_err = err
        if res is not None and threads > 1:
            for url in self.test_urls:
                mres, _merr = stream_download_multi(url, proxies, duration, self.size_bytes,
                                                    self.max_time, threads=threads, warmup=warmup)
                if mres is not None:
                    res["multi_avg"] = mres["avg"]
                    res["multi_min"] = mres["min"]
                    break
        return info, res, last_err

# ---------------------------------------------------------------- 单实例多监听引擎（StairSpeedTest 同架构）

class ListenerPool:
    """一个 mihomo 实例 + 每节点一个独立监听端口（listener 直连该节点）。
    零进程开销：测速=直接往对应端口发请求，4~6 节点纯并发，
    与 StairSpeedTest（单内核多 outbound）同思路。"""

    def __init__(self, workdir, nodes, size_mb, max_time, latency_timeout,
                 latency_url, test_urls, probes=3, concurrency=6, threads=4,
                 base_port=7800):
        self.workdir = workdir
        self.nodes = nodes
        self.size_bytes = int(size_mb * 1024 * 1024)
        self.max_time = max_time
        self.latency_timeout = latency_timeout
        self.latency_url = latency_url
        self.test_urls = test_urls
        self.probes = probes
        self.concurrency = concurrency
        self.threads = threads
        self.base_port = base_port
        self.ports = {}
        self.proc = None
        self.api_port = None
        self.logf = None

    def start(self):
        self.api_port = pick_free_port([9090])
        listeners = []
        for idx, node in enumerate(self.nodes):
            port = self.base_port + idx
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.bind(("127.0.0.1", port))
                except OSError:
                    port = pick_free_port([port])
            self.ports[node["name"]] = port
            listeners.append({"name": f"L{idx}", "type": "mixed",
                              "port": port, "proxy": node["name"]})
        inst_dir = os.path.join(self.workdir, "listener_pool")
        os.makedirs(inst_dir, exist_ok=True)
        geo = os.path.join(self.workdir, "geoip.metadb")
        if os.path.exists(geo) and not os.path.exists(os.path.join(inst_dir, "geoip.metadb")):
            try:
                import shutil
                shutil.copy(geo, os.path.join(inst_dir, "geoip.metadb"))
            except Exception:
                pass
        cfg = build_config(self.nodes, 7890, self.api_port, listeners=listeners)
        cfg_path = os.path.join(inst_dir, "config.yaml")
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write(cfg)
        self.logf = open(os.path.join(inst_dir, "mihomo.log"), "w", encoding="utf-8")
        self.proc = subprocess.Popen([MIHOMO_EXE, "-f", cfg_path, "-d", inst_dir],
                                     stdout=self.logf, stderr=subprocess.STDOUT, cwd=inst_dir)
        deadline = time.time() + 25
        while time.time() < deadline:
            try:
                if requests.get(f"http://127.0.0.1:{self.api_port}/version",
                                timeout=1).status_code == 200:
                    return
            except Exception:
                pass
            if self.proc.poll() is not None:
                raise RuntimeError(open(os.path.join(inst_dir, "mihomo.log"),
                                        encoding="utf-8", errors="ignore").read()[-500:])
            time.sleep(0.3)
        raise RuntimeError("mihomo API 启动超时")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=6)
            except Exception:
                self.proc.kill()
        if self.logf:
            try:
                self.logf.close()
            except Exception:
                pass

    def _test_one(self, node, duration):
        name = node["name"]
        port = self.ports[name]
        api = f"http://127.0.0.1:{self.api_port}"
        enc = urllib.parse.quote(name, safe="")
        delays, fails = [], 0
        for _ in range(self.probes):
            try:
                r = requests.get(f"{api}/proxies/{enc}/delay?timeout={self.latency_timeout}"
                                 f"&url={urllib.parse.quote(self.latency_url, safe='')}",
                                 timeout=self.latency_timeout / 1000 + 5)
                if r.status_code == 200 and r.json().get("delay"):
                    delays.append(r.json()["delay"])
                    continue
            except Exception:
                pass
            fails += 1
            if fails >= 2 and not delays:
                break
        if not delays:
            return name, None, None, "延迟不通"
        info = {"delay": round(sum(delays) / len(delays)), "loss": round(fails / self.probes * 100),
                "min": min(delays), "max": max(delays), "probes": self.probes}
        proxies = {"http": f"http://127.0.0.1:{port}",
                   "https": f"http://127.0.0.1:{port}"}
        last_err = None
        for url in self.test_urls:
            single_box, multi_box = [None], [None]

            def do_single():
                single_box[0], _ = stream_download(url, proxies, duration,
                                                   self.size_bytes, self.max_time)

            def do_multi():
                multi_box[0], _ = stream_download_multi(url, proxies, duration,
                                                        self.size_bytes, self.max_time,
                                                        threads=self.threads)

            t1 = threading.Thread(target=do_single)
            t2 = threading.Thread(target=do_multi)
            t1.start()
            t2.start()
            t1.join()
            t2.join()
            res, mres = single_box[0], multi_box[0]
            if res is not None or mres is not None:
                if res is None:
                    res = mres
                if mres:
                    res["multi_avg"] = mres["avg"]
                    res["multi_min"] = mres["min"]
                return name, info, res, None
            last_err = "all urls failed"
        return name, info, None, last_err

    def sweep(self, nodes, duration, on_progress=None, concurrency=None):
        conc = concurrency or self.concurrency
        results = {}
        total = len(nodes)
        done = 0
        for start in range(0, total, conc):
            group = nodes[start:start + conc]
            with ThreadPoolExecutor(max_workers=len(group)) as ex:
                futures = [ex.submit(self._test_one, node, duration) for node in group]
                for f in futures:
                    name, info, res, err = f.result()
                    results[name] = (info, res, err)
            done += len(group)
            print(f"    [扫描 {done}/{total}]", flush=True)
            if on_progress:
                on_progress(done, total, results)
        return results

# ---------------------------------------------------------------- 输出

def emby_verdict(avg, mn=None):
    """按 Emby 播放需求评估节点（单位 MB/s）"""
    if avg is None:
        return "不可用"
    if avg >= 50:
        v = "旗舰·4K原盘"
    elif avg >= 30:
        v = "优秀·4K高码率"
    elif avg >= 15:
        v = "良好·1080p高码/4K低码"
    elif avg >= 8:
        v = "可用·1080p"
    else:
        v = "不推荐·低于8M"
    if mn is not None and mn < 2:
        v += "⚠波动"
    return v

def calc_consistency(avgs):
    """多轮一致性：各轮平均速度的变异系数（越小越稳，0=完全一致）"""
    if len(avgs) < 2:
        return None
    m = sum(avgs) / len(avgs)
    if m <= 0:
        return 0.0
    var = sum((a - m) ** 2 for a in avgs) / len(avgs)
    return (var ** 0.5) / m

def compute_score(res, lat, loss=None, stalls=None, consistency=None):
    """综合评分 0-100：
    单线程速度60% + 多线程15% + 稳定度(最低÷平均,钳制0~1)15% + 延迟10%
    + 丢包5% + 多轮一致性5% − 断流惩罚(每断流1次扣2分,上限10)"""
    avg = res["avg"]
    multi = res.get("multi_avg") or avg
    stab = min(res["min"] / avg, 1.0) if avg > 0 else 0
    lat_part = max(0.0, 1 - (lat or 500) / 500) * 10
    speed_part = min(avg / 25, 1.0) * 60
    multi_part = min(multi / 50, 1.0) * 15
    stab_part = stab * 15
    loss_part = max(0.0, 1 - (loss or 0) / 100) * 5
    stall_penalty = min(stalls or 0, 5) * 2
    cons_part = 0.0
    if consistency is not None:
        cons_part = max(0.0, 1 - min(consistency, 0.4) / 0.4) * 5
    return min(round(speed_part + multi_part + stab_part + lat_part + loss_part + cons_part - stall_penalty), 100)

def grade_of(score):
    if score >= 90:
        return "S"
    if score >= 80:
        return "A"
    if score >= 70:
        return "B"
    if score >= 60:
        return "C"
    return "D"

def print_table(rows):
    hdr = f"{'#':>3}  {'节点名称':<30} {'延迟ms':>7} {'丢包%':>5} {'单MB/s':>8} {'多MB/s':>8} {'最低MB/s':>8} {'断流':>4} {'稳定':>6} {'评分':>5}  评估/状态"
    print(hdr)
    print("-" * 140)
    scored = []
    for row in rows:
        if row["res"]:
            r = row["res"]
            st = r.get("stalls", 0) + r.get("reconnects", 0)
            sc = compute_score(r, row["latency"], row.get("loss"), st)
            scored.append((sc, row))
        else:
            scored.append((-1, row))
    scored.sort(key=lambda x: -x[0])
    for i, (sc, row) in enumerate(scored, 1):
        if row["res"]:
            r = row["res"]
            lat = f"{row['latency']}" if row["latency"] else "-"
            loss = f"{row['loss']}" if row.get("loss") is not None else "-"
            stab = min(r["min"] / r["avg"], 1.0) if r["avg"] > 0 else 0
            multi = f"{r['multi_avg']:.2f}" if r.get("multi_avg") else "-"
            st = r.get("stalls", 0) + r.get("reconnects", 0)
            print(f"{i:>3}  {row['name'][:30]:<30} {lat:>7} {loss:>5} {r['avg']:>8.2f} {multi:>8} {r['min']:>8.2f} {st:>4} {stab:>6.2f} {sc:>4} {grade_of(sc)}  {emby_verdict(r['avg'], r['min'])}")
        else:
            lat = f"{row['latency']}" if row["latency"] else "-"
            loss = f"{row['loss']}" if row.get("loss") is not None else "-"
            print(f"{i:>3}  {row['name'][:30]:<30} {lat:>7} {loss:>5} {'-':>8} {'-':>8} {'-':>8} {'-':>4} {'-':>6} {'-':>4} -  {row['status']}")

def print_summary(summary):
    items = []
    for name, e in summary.items():
        avg = sum(e["avgs"]) / len(e["avgs"])
        mn = min(e["mins"])
        lat = sum(e["lats"]) / len(e["lats"]) if e["lats"] else None
        multi = sum(e["multis"]) / len(e["multis"]) if e["multis"] else None
        loss = sum(e["losses"]) / len(e["losses"]) if e["losses"] else None
        st = sum(e["stalls"])
        cons = calc_consistency(e["avgs"])
        score = compute_score({"avg": avg, "min": mn, "multi_avg": multi},
                              lat, loss, st, cons)
        items.append((name, e, avg, mn, lat, multi, loss, st, cons, score))
    items.sort(key=lambda x: -x[9])
    print(f"\n=== 多轮汇总（按综合评分从高到低，共 {len(items)} 个被测对象）===")
    print(f"{'节点名称':<30} {'轮次':>4} {'单MB/s':>8} {'多MB/s':>8} {'最低MB/s':>8} {'断流':>4} {'稳定':>6} {'均延迟':>7} {'丢包%':>5} {'一致性':>6} {'评分':>5}  评估")
    print("-" * 150)
    for name, e, avg, mn, lat, multi, loss, st, cons, score in items[:25]:
        stab = min(mn / avg, 1.0) if avg > 0 else 0
        lat_s = f"{lat:.0f}" if lat else "-"
        multi_s = f"{multi:.2f}" if multi else "-"
        loss_s = f"{loss:.0f}" if loss is not None else "-"
        cons_s = f"{cons:.2f}" if cons is not None else "-"
        print(f"{name[:30]:<30} {len(e['avgs']):>4} {avg:>8.2f} {multi_s:>8} {mn:>8.2f} {st:>4} {stab:>6.2f} {lat_s:>7} {loss_s:>5} {cons_s:>6} {score:>4} {grade_of(score)}  {emby_verdict(avg, mn)}")

def save_results(rounds, summary, out_base, size_mb, duration, test_url):
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    data = {"timestamp": stamp, "size_mb": size_mb, "duration_s": duration,
            "test_url": test_url, "rounds": rounds,
            "summary": {k: {"rounds": len(v["avgs"]),
                            "avg_mbps": round(sum(v["avgs"]) / len(v["avgs"]), 2),
                            "multi_mbps": (round(sum(v["multis"]) / len(v["multis"]), 2) if v["multis"] else None),
                            "min_mbps": round(min(v["mins"]), 2),
                            "avg_latency_ms": (round(sum(v["lats"]) / len(v["lats"]), 0) if v["lats"] else None),
                            "loss_pct": (round(sum(v["losses"]) / len(v["losses"]), 0) if v["losses"] else None),
                            "stalls": sum(v["stalls"]),
                            "consistency": (round(calc_consistency(v["avgs"]), 3) if len(v["avgs"]) > 1 else None),
                            "score": compute_score({"avg": sum(v["avgs"]) / len(v["avgs"]),
                                                    "min": min(v["mins"]),
                                                    "multi_avg": (sum(v["multis"]) / len(v["multis"]) if v["multis"] else None)},
                                                   (sum(v["lats"]) / len(v["lats"]) if v["lats"] else None),
                                                   (sum(v["losses"]) / len(v["losses"]) if v["losses"] else None),
                                                   sum(v["stalls"]),
                                                   calc_consistency(v["avgs"])),
                            "grade": grade_of(compute_score(
                                {"avg": sum(v["avgs"]) / len(v["avgs"]),
                                 "min": min(v["mins"]),
                                 "multi_avg": (sum(v["multis"]) / len(v["multis"]) if v["multis"] else None)},
                                (sum(v["lats"]) / len(v["lats"]) if v["lats"] else None),
                                (sum(v["losses"]) / len(v["losses"]) if v["losses"] else None),
                                sum(v["stalls"]), calc_consistency(v["avgs"]))),
                            "verdict": emby_verdict(sum(v["avgs"]) / len(v["avgs"]), min(v["mins"]))}
                        for k, v in summary.items()}}
    jpath = f"{out_base}_{stamp}.json"
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    cpath = f"{out_base}_{stamp}.csv"
    with open(cpath, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["round", "time", "name", "type", "server", "port",
                    "latency_ms", "loss_pct", "single_mbps", "multi_mbps",
                    "min_mbps", "max_mbps", "stalls", "score", "grade", "status"])
        for rd in rounds:
            for row in rd["rows"]:
                res = row["res"]
                sc = compute_score(res, row["latency"], row.get("loss"),
                                   (res.get("stalls", 0) + res.get("reconnects", 0))) if res else ""
                w.writerow([rd["round"], rd["time"], row["name"], row["type"],
                            row["server"], row["port"], row["latency"],
                            row.get("loss") if row.get("loss") is not None else "",
                            f"{res['avg']:.2f}" if res else "",
                            f"{res.get('multi_avg'):.2f}" if res and res.get("multi_avg") else "",
                            f"{res['min']:.2f}" if res else "",
                            f"{res['max']:.2f}" if res else "",
                            (res.get("stalls", 0) + res.get("reconnects", 0)) if res else "",
                            sc, grade_of(sc) if res else "", row["status"]])
    spath = f"{out_base}_summary_{stamp}.csv"
    with open(spath, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["name", "rounds", "avg_mbps", "multi_mbps", "min_mbps",
                    "stability", "avg_latency_ms", "loss_pct", "stalls",
                    "consistency", "score", "grade", "verdict"])
        srows = []
        for name, e in summary.items():
            avg = sum(e["avgs"]) / len(e["avgs"])
            mn = min(e["mins"])
            lat = sum(e["lats"]) / len(e["lats"]) if e["lats"] else ""
            multi = sum(e["multis"]) / len(e["multis"]) if e["multis"] else ""
            loss = sum(e["losses"]) / len(e["losses"]) if e["losses"] else ""
            st = sum(e["stalls"])
            cons = calc_consistency(e["avgs"])
            sc = compute_score({"avg": avg, "min": mn, "multi_avg": multi or None},
                               lat or None, loss or None, st, cons)
            srows.append((sc, [name, len(e["avgs"]), f"{avg:.2f}",
                               f"{multi:.2f}" if multi else "", f"{mn:.2f}",
                               f"{mn / avg:.2f}" if avg > 0 else "",
                               f"{lat:.0f}" if lat else "", f"{loss:.0f}" if loss else "",
                               st, f"{cons:.2f}" if cons is not None else "",
                               sc, grade_of(sc), emby_verdict(avg, mn)]))
        srows.sort(key=lambda x: -x[0])
        for _, r in srows:
            w.writerow(r)
    return jpath, cpath, spath

def generate_report(summary, out_base, title="机场节点测速报告"):
    """生成 PNG 评分图 + HTML 报告（StairSpeedTest 风格）"""
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

    def _clean(s):
        # 图表字体不含 emoji/部分符号，剥离后在图上显示
        return "".join(ch for ch in s
                       if not (0x1F000 <= ord(ch) <= 0x1FAFF)      # emoji/旗帜
                       and not (0x2600 <= ord(ch) <= 0x27BF)       # ⚠ 等符号
                       and not (0x2B00 <= ord(ch) <= 0x2BFF)
                       and ord(ch) != 0x200D).strip()

    items = []
    for k, v in summary.items():
        avg = sum(v["avgs"]) / len(v["avgs"])
        lat = sum(v["lats"]) / len(v["lats"]) if v["lats"] else None
        loss = sum(v["losses"]) / len(v["losses"]) if v["losses"] else None
        st = sum(v["stalls"])
        cons = calc_consistency(v["avgs"])
        sc = compute_score({"avg": avg, "min": min(v["mins"]),
                            "multi_avg": (sum(v["multis"]) / len(v["multis"]) if v["multis"] else None)},
                           lat, loss, st, cons)
        items.append((k, v, sc))
    items.sort(key=lambda x: -x[2])
    nodes = [(k, v) for k, v, _ in items if k != "直连(无代理)"]
    top = nodes[:12][::-1]

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    png = f"{out_base}_report_{stamp}.png"
    html = f"{out_base}_report_{stamp}.html"

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(13, max(8, 5 + len(nodes) * 0.28)))
    fig.suptitle(f"{title}  {stamp[:4]}-{stamp[4:6]}-{stamp[6:8]} {stamp[8:10]}:{stamp[10:12]}",
                 fontsize=14, fontweight="bold")

    # 上图：Top 节点横向柱状（单线程速度 + 评分标注）
    if top:
        names = [_clean(k)[:22] for k, _ in top]
        avgs = [sum(v["avgs"]) / len(v["avgs"]) for _, v in top]
        multis = [sum(v["multis"]) / len(v["multis"]) if v["multis"] else None for _, v in top]
        scores = []
        for (_, v) in top:
            lat = sum(v["lats"]) / len(v["lats"]) if v["lats"] else None
            loss = sum(v["losses"]) / len(v["losses"]) if v["losses"] else None
            scores.append(compute_score({"avg": sum(v["avgs"]) / len(v["avgs"]),
                                         "min": min(v["mins"]),
                                         "multi_avg": (sum(v["multis"]) / len(v["multis"]) if v["multis"] else None)},
                                        lat, loss, sum(v["stalls"])))
        colors = ["#2ecc71" if s >= 80 else "#f1c40f" if s >= 60 else "#e74c3c" for s in scores]
        y = range(len(top))
        ax1.barh(list(y), avgs, color=colors, alpha=0.85, label="单线程")
        if any(multis):
            ax1.barh(list(y), [m or 0 for m in multis], color="#3498db", alpha=0.45, label="多线程")
        for yi, (nm, avg, sc) in zip(y, zip(names, avgs, scores)):
            ax1.text(avg + 0.3, yi, f"{avg:.1f}MB/s  {grade_of(sc)}{sc}", va="center", fontsize=8)
        ax1.set_yticks(list(y))
        ax1.set_yticklabels(names, fontsize=8)
        ax1.set_xlabel("MB/s")
        ax1.set_title("Top 节点下行速度（单线程=实心，多线程=浅色）", fontsize=11)
        ax1.grid(axis="x", alpha=0.3)
        ax1.legend(fontsize=8)
    else:
        ax1.text(0.5, 0.5, "无可用节点", ha="center")

    # 下图：全节点表格
    ax2.axis("off")
    rows_data = []
    for k, v in nodes[:25]:
        avg = sum(v["avgs"]) / len(v["avgs"])
        mn = min(v["mins"])
        lat = sum(v["lats"]) / len(v["lats"]) if v["lats"] else None
        multi = sum(v["multis"]) / len(v["multis"]) if v["multis"] else None
        loss = sum(v["losses"]) / len(v["losses"]) if v["losses"] else None
        st = sum(v["stalls"])
        cons = calc_consistency(v["avgs"])
        sc = compute_score({"avg": avg, "min": mn, "multi_avg": multi}, lat, loss, st, cons)
        rows_data.append([_clean(k)[:24], f"{lat:.0f}" if lat else "-",
                          f"{loss:.0f}" if loss is not None else "-",
                          f"{avg:.2f}", f"{multi:.2f}" if multi else "-",
                          f"{mn:.2f}", f"{st}",
                          f"{cons:.2f}" if cons is not None else "-",
                          f"{sc} {grade_of(sc)}",
                          _clean(emby_verdict(avg, mn))])
    if rows_data:
        tbl = ax2.table(cellText=rows_data,
                        colLabels=["节点", "延迟ms", "丢包%", "单MB/s", "多MB/s",
                                   "最低MB/s", "断流", "一致性", "评分", "评估"],
                        loc="center", cellLoc="left")
        tbl.auto_set_font_size(False)
        tbl.set_fontsize(8)
        tbl.scale(1, 1.15)
    else:
        ax2.text(0.5, 0.5, "无可用节点", ha="center", transform=ax2.transAxes, fontsize=12)
    ax2.set_title(f"全部节点（共 {len(nodes)} 个，前 25 展示）", fontsize=11)

    plt.tight_layout()
    fig.savefig(png, dpi=130, bbox_inches="tight")
    plt.close(fig)

    with open(html, "w", encoding="utf-8") as f:
        f.write(f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>{title}</title></head><body style="font-family:Microsoft YaHei,sans-serif;background:#f5f6fa">
<h2 style="text-align:center">{title}（{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]} {stamp[8:10]}:{stamp[10:12]}）</h2>
<img src="{os.path.basename(png)}" style="max-width:100%">
</body></html>""")
    return png, html

# ---------------------------------------------------------------- 主流程

def do_selftest():
    print("=== 离线自检：解析内置样例 ===")
    all_ok = True
    for uri in SAMPLE_URIS:
        kind = uri.split("://")[0]
        fn = {"ss": parse_ss, "vmess": parse_vmess, "vless": parse_vless,
              "trojan": parse_trojan, "ssr": parse_ssr}[kind]
        node = fn(uri, None)
        if node:
            print(f"[OK] {kind:<7} -> {node.get('name')!r} @ {node['server']}:{node['port']} "
                  f"type={node['type']}")
        else:
            print(f"[FAIL] {kind}")
            all_ok = False
    return all_ok

# ---------------------------------------------------------------- 进度可视化

class ProgressTracker:
    """实时进度看板：写入 progress.json + progress.html（浏览器 3 秒自动刷新）"""

    def __init__(self, out_dir):
        self.out_dir = out_dir
        self.t0 = time.time()
        self.state = {"round": 0, "total_rounds": 0, "phase": "启动中",
                      "done": 0, "total": 0, "pct": 0, "elapsed_s": 0, "eta_s": 0,
                      "message": "", "nodes": {}}

    def set_round(self, r, total):
        self.state["round"] = r
        self.state["total_rounds"] = total

    def set_phase(self, phase, done=0, total=0, message=""):
        self.state["phase"] = phase
        self.state["done"] = done
        self.state["total"] = total
        self.state["message"] = message

    def update_nodes(self, results):
        """results: {name: (info, res, err)} → 转成可显示行"""
        for name, (info, res, err) in results.items():
            row = {"latency": info.get("delay") if info else None,
                   "loss": info.get("loss") if info else None,
                   "avg": round(res["avg"], 2) if res else None,
                   "multi": round(res.get("multi_avg"), 2) if res and res.get("multi_avg") else None,
                   "stalls": (res.get("stalls", 0) + res.get("reconnects", 0)) if res else None,
                   "status": "OK" if res else (err or "延迟不通")}
            row["score"] = None
            if res:
                row["score"] = compute_score(res, row["latency"], row["loss"], row["stalls"])
            self.state["nodes"][name] = row

    def save(self):
        st = self.state
        elapsed = time.time() - self.t0
        st["elapsed_s"] = int(elapsed)
        pct = (st["done"] / st["total"] * 100) if st["total"] else 0
        st["pct"] = round(pct)
        st["eta_s"] = int(elapsed / pct * (100 - pct)) if pct > 1 else 0
        try:
            with open(os.path.join(self.out_dir, "progress.json"), "w", encoding="utf-8") as f:
                json.dump(st, f, ensure_ascii=False, indent=1)
            with open(os.path.join(self.out_dir, "progress.html"), "w", encoding="utf-8") as f:
                f.write(self._render(st))
        except Exception:
            pass

    def _render(self, st):
        def fmt(v, d="-"):
            return f"{v}" if v is not None else d
        nodes = st["nodes"]
        ranked = sorted(nodes.items(),
                        key=lambda kv: -(kv[1].get("score") if kv[1].get("score") is not None else -1))
        rows = []
        for name, d in ranked[:40]:
            rows.append(
                f"<tr><td class='nm'>{name[:42]}</td>"
                f"<td>{fmt(d.get('latency'))}</td>"
                f"<td>{fmt(d.get('loss'))}</td>"
                f"<td>{fmt(d.get('avg'))}</td>"
                f"<td>{fmt(d.get('multi'))}</td>"
                f"<td>{fmt(d.get('stalls'))}</td>"
                f"<td>{fmt(d.get('score'))}</td>"
                f"<td>{d.get('status', '')}</td></tr>")
        table = "\n".join(rows) if rows else "<tr><td colspan=8 style='text-align:center'>等待数据...</td></tr>"
        mm, ss = int(st["elapsed_s"] // 60), st["elapsed_s"] % 60
        em, es = int(st["eta_s"] // 60), st["eta_s"] % 60
        return f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="3"><title>机场测速进度</title>
<style>
body{{font-family:'Microsoft YaHei',sans-serif;background:#0f172a;color:#e2e8f0;margin:16px}}
h2{{margin:4px 0}} .dim{{color:#94a3b8;font-size:13px;margin:2px 0}}
.bar{{background:#1e293b;border-radius:8px;height:22px;margin:10px 0;overflow:hidden}}
.bar>div{{background:linear-gradient(90deg,#38bdf8,#4ade80);height:100%;border-radius:8px;transition:width .5s}}
table{{border-collapse:collapse;width:100%;font-size:13px;margin-top:8px}}
th,td{{border:1px solid #334155;padding:4px 8px;text-align:left}}
th{{background:#1e293b;position:sticky;top:0}}
tr:nth-child(even){{background:#111c33}}
.nm{{max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
</style></head><body>
<h2>✈️ 机场节点测速进度</h2>
<div class="dim">第 <b>{st['round']}</b>/{st['total_rounds']} 轮 · 阶段：{st['phase']} · {st['message']}</div>
<div class="bar"><div style="width:{st['pct']}%"></div></div>
<div class="dim">本轮 {st['done']}/{st['total']}（{st['pct']}%）· 已用 {mm}分{ss}秒 · 预计剩余 {em}分{es}秒 · 已出成绩 {len(nodes)} 个节点</div>
<table><tr><th>节点</th><th>延迟ms</th><th>丢包%</th><th>单MB/s</th><th>多MB/s</th><th>断流</th><th>评分</th><th>状态</th></tr>
{table}
</table>
<div class="dim" style="margin-top:8px">页面每 3 秒自动刷新 · 完整结果跑完后见 result_*.json/csv 与评分图报告</div>
</body></html>"""

def main():
    ap = argparse.ArgumentParser(description="机场订阅节点测速工具")
    ap.add_argument("url", nargs="?", help="机场订阅链接")
    ap.add_argument("--size-mb", type=int, default=50)
    ap.add_argument("--max-time", type=int, default=60)
    ap.add_argument("--duration", type=int, default=0,
                    help="每节点每次持续下载秒数（0=按 --size-mb 限量）。模拟流媒体持续播放，推荐 20~60")
    ap.add_argument("--loop", type=int, default=1, help="循环测试轮数（测晚高峰稳定性，如 6~12 轮）")
    ap.add_argument("--interval-min", type=int, default=10, help="循环轮次之间的间隔分钟数")
    ap.add_argument("--threads", type=int, default=4,
                    help="多线程并行连接数（>1 时单线程+多线程都测；1=只测单线程），默认 4")
    ap.add_argument("--report", action="store_true",
                    help="生成 PNG 评分图 + HTML 报告（StairSpeedTest 风格）")
    ap.add_argument("--accurate", action="store_true",
                    help="精准模式：串行逐节点，预热+稳态，单/多线程分开测（数据最准，不追求速度）")
    ap.add_argument("--warmup", type=int, default=2, help="精准模式预热秒数（丢弃慢启动，默认 2）")
    ap.add_argument("--adaptive", action="store_true",
                    help="自适应淘汰：连续不达标节点降级/淘汰，轮次越跑越快（--loop>1 时默认开启）")
    ap.add_argument("--no-adaptive", action="store_true", help="禁用自适应淘汰")
    ap.add_argument("--min-speed", type=float, default=10.0,
                    help="自适应阈值：单线程平均低于该值(MB/s)视为不达标（默认 10.0）")
    ap.add_argument("--elim-rounds", type=int, default=2,
                    help="连续几轮不达标才降级/淘汰（默认 2，防误杀）")
    ap.add_argument("--reprobe-every", type=int, default=3,
                    help="死节点每隔几轮快速复探一次（默认 3，防永久误杀）")
    ap.add_argument("--sweep", action="store_true",
                    help="并发快扫模式（StairSpeedTest 风格）：多实例并发测全部节点 + 前N名串行精测")
    ap.add_argument("--no-sweep", action="store_true", help="禁用并发快扫（回到纯串行）")
    ap.add_argument("--concurrency", type=int, default=6,
                    help="并发快扫同时测几个节点（默认 6；并发越多越快但会分摊带宽，快扫仅用于初筛）")
    ap.add_argument("--last-round-concurrency", type=int, default=2,
                    help="最后一轮用的并发数（默认 2，节点少时更准）")
    ap.add_argument("--sweep-duration", type=int, default=6,
                    help="快扫时每节点短测秒数（默认 6）")
    ap.add_argument("--test-url", default=None)
    ap.add_argument("--latency-timeout", type=int, default=3000, help="延迟测试超时(ms)，默认 3000（提速：死节点更快判定）")
    ap.add_argument("--latency-url", default="http://www.gstatic.com/generate_204")
    ap.add_argument("--probes", type=int, default=5,
                    help="每节点丢包/延迟探测次数（默认 5，越大越准越慢）")
    ap.add_argument("--ua", default=DEFAULT_UA)
    ap.add_argument("--list-only", action="store_true")
    ap.add_argument("--limit", type=int, default=0,
                    help="最多测试前 N 个节点（0=全部；大订阅建议限制，如 --limit 50）")
    ap.add_argument("--filter", default="",
                    help="按名称关键字筛选节点，逗号分隔，如 --filter \"三网推荐,hy2\"")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--no-download", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        sys.exit(0 if do_selftest() else 1)
    if not args.url:
        ap.print_help()
        sys.exit(1)

    print(f"[*] 读取订阅: {args.url}")
    try:
        text, raw = load_subscription(args.url, args.ua)
    except Exception as e:
        print(f"[错误] 订阅读取失败: {e}")
        sys.exit(1)

    # 自动 UA 兜底：部分机场按 UA 分发内容，不支持的 UA 会返回提示信息而非节点
    AUTO_UAS = ["clash-verge/v2.0.2", "v2rayNG/1.8.10", "mihomo/1.19.29",
                "sing-box/1.9.0", "NekoBox/1.3.0"]
    if args.url.startswith(("http://", "https://")) and ("不支持" in text[:2000] or "请换用" in text[:2000]):
        for ua in AUTO_UAS:
            if ua == args.ua:
                continue
            print(f"[!] 当前 UA 被机场拒绝，尝试 UA: {ua}")
            try:
                text, raw = fetch_subscription(args.url, ua)
                if "不支持" not in text[:2000] and "请换用" not in text[:2000]:
                    print(f"[+] UA {ua} 可用")
                    break
            except Exception:
                continue

    proxies = None
    if "proxies:" in text[:2000]:
        try:
            doc = yaml.safe_load(text)
            if isinstance(doc, dict) and isinstance(doc.get("proxies"), list):
                proxies = doc["proxies"]
                print(f"[+] 识别为 Clash YAML 订阅，节点 {len(proxies)} 个")
        except Exception:
            proxies = None
    if proxies is None:
        proxies = parse_subscription_text(text)
        if proxies:
            print(f"[+] 识别为 URI/Base64 订阅，解析出节点 {len(proxies)} 个")
    if not proxies:
        print("[错误] 未能从订阅中解析出任何节点")
        sys.exit(1)
    proxies = dedupe_names(proxies)
    # 过滤机场配置里的提示性假节点（如 127.0.0.1:6666）
    before = len(proxies)
    proxies = [p for p in proxies
               if str(p.get("server", "")).strip() not in ("127.0.0.1", "localhost", "0.0.0.0")]
    if len(proxies) != before:
        print(f"[*] 已过滤 {before - len(proxies)} 个提示性假节点")
    # 过滤"请选择节点"类占位节点
    before = len(proxies)
    proxies = [p for p in proxies
               if not any(k in p.get("name", "") for k in ("请选择", "选择节点", "请换用"))]
    if len(proxies) != before:
        print(f"[*] 已过滤 {before - len(proxies)} 个占位节点")
    if args.filter:
        kws = [k.strip() for k in args.filter.split(",") if k.strip()]
        before = len(proxies)
        proxies = [p for p in proxies if any(k in p.get("name", "") for k in kws)]
        print(f"[*] --filter 命中 {len(proxies)}/{before} 个节点")
    if args.limit and len(proxies) > args.limit:
        print(f"[*] --limit {args.limit}：只取前 {args.limit} 个节点")
        proxies = proxies[:args.limit]
    elif len(proxies) > 300:
        print(f"[!] 节点多达 {len(proxies)} 个，测速会很久，建议加 --limit 50 只测前 50 个")
        if not args.list_only:
            pass

    for i, p in enumerate(proxies, 1):
        print(f"  {i:>3}. {p.get('name','?')}  ({p.get('type','?')})  {p.get('server','?')}:{p.get('port','?')}")
    print(f"[*] 共 {len(proxies)} 个节点")

    if args.list_only:
        return

    ensure_mihomo(allow_download=not args.no_download)

    os.makedirs(WORK_DIR, exist_ok=True)
    mixed_port = pick_free_port([7890])
    api_port = pick_free_port([9090])
    cfg_path = os.path.join(WORK_DIR, "config.yaml")
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write(build_config(proxies, mixed_port, api_port))
    print(f"[*] 配置文件: {cfg_path}  (mixed-port={mixed_port}, api={api_port})")

    # 测速文件大小：时长模式按 55MB/s 上限预留足够流量，避免文件提前读完
    if args.duration:
        need_mb = min(max(200, args.duration * 55), 1000)
    else:
        need_mb = args.size_mb

    def nearest(sizes, t):
        return min(sizes, key=lambda s: abs(s - t))

    if args.test_url:
        test_urls = [args.test_url]
    elif args.accurate:
        # ===== 防失真测速源（实战教训）=====
        # 1) speed.cloudflare.com 会对单连接限流（首秒爆发后骤降至 0.01），严禁作为单线程主源；
        # 2) 50MB 小文件 + 持续下载会在数秒内下完 → 频繁重连 → 触发机场“新建连接限流”假性变慢；
        # 3) Google CDN android-ndk-r21e 实测 1.1GB、单连接 380Mbps 稳定、无重连，作为首选源；
        # 4) 其余大厂 CDN 作为兜底（Cachefly/微软/CF 依次尝试，CF 仅最后）。
        test_urls = [
            "https://dl.google.com/android/repository/android-ndk-r21e-linux-x86_64.zip",
            "https://dl.google.com/android/repository/platform-34-ext7_r02.zip",
            "https://dl.google.com/android/studio/maven-google-com/stable/offline-gmaven-stable.zip",
            "http://cachefly.cachefly.net/200mb.test",
            "https://download.microsoft.com/download/2/0/E/20E90413-712F-438C-988E-FDAA79A8AC3D/dotnetfx35.exe",
            "https://speed.cloudflare.com/__down?bytes=52428800",
        ]
    else:
        # 各服务器实测可用档位：Google NDK 1.1GB 大文件最稳（单连接持续、无重连）；
        # cloudflare ≤50MB（单连接限流，仅兜底）；ovh 10Gb.dat 大文件持续稳但海外绕路；
        # tele2 100/200/1000MB；cachefly 100mb.test
        mb = nearest([100, 200, 1000], need_mb)
        if args.duration:
            # 时长模式：cloudflare 50MB 文件由 download_once 自动循环续传直到时长到，兼顾速度与任意时长
            test_urls = [
                "https://dl.google.com/android/repository/android-ndk-r21e-linux-x86_64.zip",
                f"https://speed.cloudflare.com/__down?bytes={min(need_mb, 50) * 1024 * 1024}",
                "http://proof.ovh.net/files/10Gb.dat",
                f"http://speedtest.tele2.net/{mb}MB.zip",
                "http://cachefly.cachefly.net/100mb.test",
            ]
        else:
            test_urls = ["https://dl.google.com/android/repository/android-ndk-r21e-linux-x86_64.zip"]
            test_urls.append(f"https://speed.cloudflare.com/__down?bytes={need_mb * 1024 * 1024}")
            if need_mb > 50:
                test_urls.append(f"http://speedtest.tele2.net/{mb}MB.zip")
            test_urls += ["http://proof.ovh.net/files/10Gb.dat",
                          "http://cachefly.cachefly.net/100mb.test"]

    # ===== 防失真：时长模式按 need_mb 预留文件大小上限，避免“文件下完→重连→被限流” =====
    eff_size_mb = need_mb if args.duration else args.size_mb

    tester = SpeedTester(cfg_path, WORK_DIR, mixed_port, api_port,
                         eff_size_mb, args.max_time,
                         args.latency_timeout, args.latency_url, test_urls,
                         threads=args.threads, probes=args.probes)
    if not tester.start():
        tester.stop()
        sys.exit(1)
    print("[+] mihomo 已启动，开始测试...")

    rounds = []
    summary = {}
    try:
        api_list = tester.get_proxies()
        # 注意：proxies 保持解析得到的完整节点（含 server/port/type），
        # 不要用 API 列表替换（API 的 type 被规范化、server 为空）
        print(f"[*] mihomo 加载节点 {len(api_list)} 个，共 {args.loop} 轮循环测试"
              f"（订阅解析节点 {len(proxies)} 个）")
        mode_desc = f"每节点持续 {args.duration}s" if args.duration else f"每节点 {args.size_mb}MB"
        adaptive = args.adaptive or (args.loop > 1 and not args.no_adaptive)
        elim = max(2, args.elim_rounds)
        node_states = {p["name"]: {"state": "full", "flags": 0, "since": 0} for p in proxies}
        tracker = ProgressTracker(TOOL_DIR)
        tracker.set_phase("初始化", message=f"共 {len(proxies)} 个节点")
        tracker.save()
        use_sweep = (args.sweep or args.loop > 1) and not args.no_sweep and not args.accurate
        pool = None
        if use_sweep:
            pool = ListenerPool(WORK_DIR, proxies, args.size_mb,
                                args.max_time, args.latency_timeout, args.latency_url,
                                test_urls, probes=args.probes,
                                concurrency=args.concurrency, threads=args.threads)
            print(f"[*] 启动单实例多监听引擎（1 个 mihomo + 每节点独立监听端口，"
                  f"并发 {args.concurrency}）...")
            try:
                pool.start()
                print(f"[+] 监听引擎就绪（{len(pool.ports)} 个节点端口）")
            except Exception as e:
                print(f"[!] 监听引擎启动失败（{e}），回退串行模式")
                try:
                    pool.stop()
                except Exception:
                    pass
                pool = None
                use_sweep = False
        for round_no in range(1, args.loop + 1):
            if round_no > 1:
                wait_s = args.interval_min * 60
                if wait_s > 0:
                    print(f"\n[*] 等待 {args.interval_min} 分钟后开始第 {round_no} 轮"
                          f"（当前 {datetime.datetime.now().strftime('%H:%M:%S')}）...")
                    time.sleep(wait_s)
            rtime = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"\n{'=' * 60}\n[第 {round_no}/{args.loop} 轮] {rtime}  ({mode_desc})"
                  f"{'  [精准模式]' if args.accurate else ('  [自适应淘汰]' if adaptive else '')}\n{'=' * 60}")
            if args.accurate:
                # ===== 精准模式：串行逐节点，预热+稳态，单/多线程先后分开测 =====
                tracker.set_round(round_no, args.loop)
                round_rows = []
                base, base_err = tester.direct_test(duration=args.duration, warmup=args.warmup)
                round_rows.append({"name": "直连(无代理)", "type": "-", "server": "-", "port": "",
                                   "latency": None, "loss": None, "res": base,
                                   "status": "OK(基线)" if base else f"FAIL {base_err}"})
                total_n = len(proxies)
                for idx, p in enumerate(proxies, 1):
                    name = p["name"]
                    print(f"  [{idx}/{total_n}] {name[:40]} "
                          f"（延迟{args.probes}次 + 单线程(预热{args.warmup}s+稳态{args.duration}s) + 多线程）...",
                          flush=True)
                    info, res, err = tester.accurate_test(name, duration=args.duration,
                                                          warmup=args.warmup,
                                                          threads=args.threads,
                                                          probes=args.probes)
                    if info is None:
                        round_rows.append({"name": name, "type": p["type"], "server": p["server"],
                                           "port": p["port"], "latency": None, "loss": 100,
                                           "res": None, "status": err or "延迟不通"})
                    else:
                        round_rows.append({"name": name, "type": p["type"], "server": p["server"],
                                           "port": p["port"], "latency": info.get("delay"),
                                           "loss": info.get("loss"), "res": res,
                                           "status": "OK" if res else f"FAIL {err}"})
                    tracker.set_phase("精准测速", idx, total_n,
                                      f"第 {round_no}/{args.loop} 轮 · 预热{args.warmup}s+稳态{args.duration}s")
                    tracker.update_nodes({name: (info, res, err)})
                    tracker.save()
                rounds.append({"round": round_no, "time": rtime, "rows": round_rows})
                for row in round_rows:
                    if row["res"]:
                        e = summary.setdefault(row["name"], {"avgs": [], "mins": [], "lats": [],
                                                             "multis": [], "losses": [], "stalls": []})
                        e["avgs"].append(row["res"]["avg"])
                        e["mins"].append(row["res"]["min"])
                        if row["res"].get("multi_avg"):
                            e["multis"].append(row["res"]["multi_avg"])
                        if row["latency"]:
                            e["lats"].append(row["latency"])
                        if row.get("loss") is not None:
                            e["losses"].append(row["loss"])
                        e["stalls"].append(row["res"].get("stalls", 0) + row["res"].get("reconnects", 0))
                tracker.set_phase(f"第 {round_no} 轮完成", total_n, total_n, "")
                tracker.save()
                print()
                print_table(round_rows)
                continue
            plan_full = [p for p in proxies if node_states[p["name"]]["state"] == "full"]
            plan_watch = [p for p in proxies if node_states[p["name"]]["state"] == "watch"]
            plan_dead = [p for p in proxies
                         if node_states[p["name"]]["state"] == "dead"
                         and node_states[p["name"]]["since"] >= args.reprobe_every - 1]
            for st in node_states.values():
                st["since"] += 1
            print(f"[*] 测试计划: 完整测速 {len(plan_full)} | 快速监测 {len(plan_watch)}"
                  f" | 死节点复探 {len(plan_dead)}")
            lats = {}
            sweep_res = {}
            if use_sweep and (plan_full or plan_watch):
                # 前几轮用 --concurrency，最后一轮用 --last-round-concurrency（更准）
                round_conc = args.last_round_concurrency if round_no == args.loop else args.concurrency
                print(f"[*] 第 1 遍：全量并发快扫 {len(plan_full) + len(plan_watch)} 个节点"
                      f"（并发 {round_conc}，每节点 {args.sweep_duration}s 单线程+多线程同时）...")
                tracker.set_round(round_no, args.loop)

                def _wave_cb(done, total, results):
                    tracker.set_phase("并发快扫", done, total,
                                      f"第 {round_no}/{args.loop} 轮 · 阈值 {args.min_speed} MB/s")
                    tracker.update_nodes(results)
                    tracker.save()

                tracker.set_phase("并发快扫", 0, len(plan_full) + len(plan_watch),
                                  f"第 {round_no}/{args.loop} 轮 · 阈值 {args.min_speed} MB/s")
                tracker.save()
                sweep_res = pool.sweep(plan_full + plan_watch, args.sweep_duration,
                                       on_progress=_wave_cb, concurrency=round_conc)
                lats = {n: info for n, (info, _r, _e) in sweep_res.items() if info}
            elif plan_full or plan_watch:
                lats.update(tester.latency_test(plan_full + plan_watch))
            if plan_dead:
                lats.update(tester.latency_test(plan_dead, probes=1,
                                                timeout=min(2000, args.latency_timeout)))
            round_rows = []
            base, base_err = tester.direct_test(duration=args.duration)
            round_rows.append({"name": "直连(无代理)", "type": "-", "server": "-", "port": "",
                               "latency": None, "loss": None, "res": base,
                               "status": "OK(基线)" if base else f"FAIL {base_err}"})
            if use_sweep:
                # 一遍扫描出结果（无复测），全部行来自 sweep_res
                for name, (info, res, err) in sweep_res.items():
                    p = next((q for q in proxies if q["name"] == name), None)
                    if p is None:
                        continue
                    if info is None:
                        round_rows.append({"name": name, "type": p["type"], "server": p["server"],
                                           "port": p["port"], "latency": None, "loss": 100,
                                           "res": None, "status": "延迟不通"})
                    else:
                        round_rows.append({"name": name, "type": p["type"], "server": p["server"],
                                           "port": p["port"], "latency": info.get("delay"),
                                           "loss": info.get("loss"), "res": res,
                                           "status": "OK" if res else f"FAIL {err}"})
            else:
                for idx, p in enumerate(plan_full + plan_watch, 1):
                    name = p["name"]
                    info = lats.get(name)
                    if not info:
                        round_rows.append({"name": name, "type": p["type"], "server": p["server"],
                                           "port": p["port"], "latency": None, "loss": 100,
                                           "res": None, "status": "延迟不通"})
                        continue
                    is_watch = node_states[name]["state"] == "watch"
                    print(f"  [{idx}/{len(plan_full) + len(plan_watch)}] {name[:40]} "
                          f"({'快速监测' if is_watch else '完整'}) ...", flush=True)
                    if is_watch:
                        res, err = tester.speed_test(name, duration=5, multi=False)
                    else:
                        res, err = tester.speed_test(name, duration=args.duration)
                    round_rows.append({"name": name, "type": p["type"], "server": p["server"],
                                       "port": p["port"], "latency": info.get("delay"),
                                       "loss": info.get("loss"), "res": res,
                                       "status": "OK" if res else f"FAIL {err}"})
            for p in proxies:
                name = p["name"]
                if node_states[name]["state"] == "dead" and not any(r["name"] == name for r in round_rows):
                    round_rows.append({"name": name, "type": p["type"], "server": p["server"],
                                       "port": p["port"], "latency": None, "loss": 100,
                                       "res": None, "status": "死节点(跳过)"})
            # 状态机更新：连续 elim 轮不达标 → 直接淘汰（防误杀由复探兜底），dead 复探复活
            trans = {"淘汰": 0, "恢复": 0}
            for p in proxies:
                name = p["name"]
                st = node_states[name]
                row = next((r for r in round_rows if r["name"] == name), None)
                if not row:
                    continue
                if row["latency"] is None:
                    if st["state"] != "dead":
                        st["flags"] += 1
                        if adaptive and st["flags"] >= elim:
                            st["state"] = "dead"; st["flags"] = 0; st["since"] = 0; trans["淘汰"] += 1
                    continue
                if st["state"] == "dead":
                    st["state"] = "watch"; st["since"] = 0; trans["恢复"] += 1
                    continue
                bad = False
                if row["res"]:
                    r = row["res"]
                    bad = r["avg"] < args.min_speed or (r.get("stalls", 0) + r.get("reconnects", 0)) > 3
                else:
                    bad = True
                if bad:
                    st["flags"] += 1
                    if adaptive and st["flags"] >= elim:
                        st["state"] = "dead"; st["flags"] = 0; st["since"] = 0; trans["淘汰"] += 1
                else:
                    st["flags"] = 0
                    if st["state"] == "watch":
                        st["state"] = "full"; trans["恢复"] += 1
            if adaptive:
                print(f"[*] 状态变化: 淘汰 {trans['淘汰']} | 恢复 {trans['恢复']}")
            tracker.set_phase(f"第 {round_no} 轮完成", len(round_rows), len(round_rows),
                              f"淘汰 {trans['淘汰']} · 恢复 {trans['恢复']}")
            tracker.update_nodes(sweep_res if sweep_res else {})
            tracker.save()
            rounds.append({"round": round_no, "time": rtime, "rows": round_rows})
            for row in round_rows:
                if row["res"]:
                    e = summary.setdefault(row["name"], {"avgs": [], "mins": [], "lats": [],
                                                         "multis": [], "losses": [], "stalls": []})
                    e["avgs"].append(row["res"]["avg"])
                    e["mins"].append(row["res"]["min"])
                    if row["res"].get("multi_avg"):
                        e["multis"].append(row["res"]["multi_avg"])
                    if row["latency"]:
                        e["lats"].append(row["latency"])
                    if row.get("loss") is not None:
                        e["losses"].append(row["loss"])
                    e["stalls"].append(row["res"].get("stalls", 0) + row["res"].get("reconnects", 0))
            print()
            print_table(round_rows)
    finally:
        if pool:
            pool.stop()
        if not args.keep:
            tester.stop()
        else:
            print(f"[*] --keep：mihomo 仍在运行 (mixed-port={mixed_port}, api={api_port})")
        try:
            tracker.set_phase("测试完成", message="全部轮次结束，见最终结果")
            tracker.save()
        except Exception:
            pass

    print_summary(summary)
    j, c, s = save_results(rounds, summary, os.path.join(TOOL_DIR, "result"),
                           args.size_mb, args.duration, test_urls[0])
    print(f"\n[+] 轮次明细: {j}\n[+] CSV: {c}\n[+] 汇总: {s}")
    if args.report:
        png, html = generate_report(summary, os.path.join(TOOL_DIR, "result"))
        print(f"[+] 评分图: {png}\n[+] HTML报告: {html}")

if __name__ == "__main__":
    main()
