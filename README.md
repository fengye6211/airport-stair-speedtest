# 机场节点测速（Airport Stair Speedtest）

一个 **StairSpeedTest 风格** 的机场节点测速工具 —— 粘贴订阅链接或拖入订阅文件，
自动解析全部节点，测出 **下行速度 / 延迟 / 丢包 / 断流**，输出 **一张测速图（PNG）+ 单文件 HTML 报告**，
并对测试数据做 **防失真自检**，专为「Emby / Jellyfin 高码率视频」场景设计。

> 本项目由真实测速实战（3 个机场、98+ 节点、整晚压测）沉淀而来，所有防失真措施都来自踩过的坑，详见 [LESSONS.md](LESSONS.md)。

## 🖥 Web 版（推荐 · 应用式体验）

```bash
# Windows：双击「启动测速.bat」
# 或命令行：
python webapp.py            # 默认端口 8787
python webapp.py 9000       # 自定义端口
```

启动后控制台显示端口链接 → 浏览器自动打开 **http://127.0.0.1:8787**：
粘贴订阅链接 / 上传订阅文件 → 选模式 → 点「开始测速」→ 实时进度 → 页面下方直接出结果
（测速图 + 明细表 + 数据质量评级 + 防失真告警 + 完整报告下载）。

**两种测试模式（速度 vs 精度的选择，数据都真实）：**
- **快速（推荐）**：先并发快扫全部节点（并发数可调 1~12）→ 自动对 Top 10 做权威精测。
  40 节点机场约 **8 分钟**（完整模式约 30 分钟）。快扫数字在报告中标注"初筛（分摊带宽）"，精测为最终数据。
- **完整**：全部节点逐个精测（30 秒级持续下载，最慢但最全面）。

## 命令行版

### 快速开始

```bash
# ① 安装依赖（仅需 Python 3.8+）
pip install -r requirements.txt

# ② 测速：粘贴订阅链接 或 本地订阅文件（base64 txt / Clash yaml 均可）
python main.py "https://你的机场订阅链接"
python main.py "D:\订阅\my_sub.yaml"

# ③ 常用参数
python main.py <链接或文件> --duration 30 --limit 20   # 每节点持续 30 秒（模拟流媒体播放），只测前 20 个
python main.py <链接或文件> --accurate                 # 精准模式：延迟探测 + 预热 + 单/多线程分开测（推荐）
python main.py <链接或文件> --ookla 3                  # 对 Top3 节点追加 Ookla / trevor.speedtestcustom.com 深测
python main.py <链接或文件> --loop 3 --interval-min 20 # 多轮循环，覆盖晚高峰（19:00 后跑最佳）
python main.py <链接或文件> --list-only                # 只列出节点
```

运行结束自动生成：
- `result_report_时间戳.png` —— **StairSpeedTest 同款测速图**（Top 速度柱状 + 延迟 + 全节点表格 + 数据质量自检）
- `result_report_时间戳.html` —— 单文件 HTML 报告（内嵌图片、可排序表格、防失真告警，可直接发人）
- `result_时间戳.json` —— 完整原始数据（**含逐秒采样**，可复核）

## 数据可信度（防失真，本项目的核心）

测速数据「真实不失真」是第一原则，内置以下自检（自动运行并标注在报告中）：

| 失真源 | 本项目措施 |
|---|---|
| speed.cloudflare.com 单连接限流（首秒爆发后骤降） | 主测速源改用 **Google CDN 1.1GB 大文件**，CF 仅作最后兜底 |
| 小文件下完→频繁重连→触发机场连接限流 | 时长模式自动按 `duration×55MB` 预留文件大小上限，单连接零重连 |
| mihomo GLOBAL 默认 DIRECT（切换失败会悄悄走直连） | 节点切换后 **API 回读验证** `now == 目标节点`，失败即报错不硬测 |
| 多实例并发互抢节点带宽 | 启动前检测 mihomo 实例数并告警；精测串行进行 |
| 晚高峰 vs 非高峰数据不可比 | 报告中标注测试时段；`--loop` 多轮覆盖 |
| 全节点速度异常均匀（限流特征） | 模式识别：单线程均匀 + 多线程远高 → 标记「单线程数据不可信」 |
| 文件提前下完 / 重连 | 逐秒采样数量与重连计数自检，异常即告警 |
| 直连快于全部节点（选择未生效） | 直连基线对比检查 |

报告给出 **数据质量评级（A/B/C）**：A=可信，B=有干扰因素，C=存在失真风险仅供参考。

## 测速图（与 StairSpeedTest 一致 + 升级）

StairSpeedTest 输出一张评分/速度图，本项目的图：

- ✅ 一致：Top 节点横向速度柱状（按评分排序、等级配色）、全节点表格、PNG 输出
- ⬆️ 升级：
  - 三面板：速度柱状（单线程实心 + 多线程浅色）+ **延迟柱状** + 全节点表格
  - 柱上直接标注 **评分/等级/丢包/断流**
  - 图上绘制 **数据真实性自检告警区** 与 **质量评级**
  - HTML 单文件（内嵌图片 base64），手机/同事随时可看，表头点击排序

## 测速原理

mihomo (Clash Meta) 内核加载节点 → 单实例串行切换节点 → 经本地端口真实下载测速文件（每节点独立监听端口用于并发快扫）→ 每秒采样统计。
内核 `tools/mihomo.exe` 已内置；其他平台可自行替换（首次运行也可自动从 GitHub 下载）。

## 目录

```
airport-stair-speedtest/
├── main.py          入口（链接/文件 → 测速 → 报告 → 可选深测）
├── engine.py        测速引擎（解析/内核管理/单多线程/评分）
├── report.py        StairSpeedTest 风格测速图（PNG + HTML 升级版）
├── integrity.py     数据真实性自检（防失真）
├── ookla.py         Ookla / trevor.speedtestcustom.com 服务器测速
├── tools/mihomo.exe 内核（Windows x64 内置）
├── work/            运行时配置与日志
├── requirements.txt
├── README.md
└── LESSONS.md       实战经验教训（为什么这么设计）
```

## 常见问题

- **为什么主测速源是 Google 的 NDK 文件？** speed.cloudflare.com 对单连接限流（首秒 14-19MB/s 后骤降至 0.01），会把所有节点测成"均匀的慢"；Google CDN 1.1GB 文件实测单连接 380Mbps 持续稳定。
- **fast.com 呢？** Netflix 已封禁 fast.com API 的自动化调用（403 Not Available）。trevor.speedtestcustom.com 经逆向确认是官方 Ookla 引擎，`--ookla` 即按该站同款方式测速。
- **为什么单线程为主？** Emby/Jellyfin 直连播放是单连接，单线程速度才是真实观看体验；多线程反映节点最大吞吐。
- **速度波动大？** 先看报告中的时段标注与质量评级；晚高峰建议 `--loop 3` 多轮对比。
