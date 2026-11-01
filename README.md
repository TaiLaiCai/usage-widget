# usage-widget

macOS 桌面小组件：一屏看全各个 **coding plan** 的额度余量、账户余额和 token 消耗。

原生 AppKit 绘制，**不嵌 WebView**，常驻约 50 MB、空闲 CPU 接近 0。数据由独立的采集脚本按小时快照，组件只读文件——所以渲染进程永远不联网、不启子进程。

```
┌──────────────────────────────────┐
│ 用量 17:20                       │
│ Grok Bot   7d ████████████  93%  │
│ Codex      5h ▁▁▁▁▁   0%  7d ████████ 100% │
│ Claude     5h ▁▁▁▁▁   0%  7d ███████    88% │
│ DeepSeek   ¥132.86               │
│ 智谱 GLM   5h █▁▁▁▁   5%  7d ▁▁▁▁▁   1%  │
│ MiniMax    5h ███████ 100% 7d ████░  49% │
│ Gemini     免费层                │
│ ─────────────────────────────    │
│ 烧掉 今日 1.76 亿 · 本月 1.76 亿  │
└──────────────────────────────────┘
```

## 支持的方案

| 方案 | 数据来源 | 说明 |
|---|---|---|
| **Codex** (ChatGPT) | `~/.codex/auth.json` → `chatgpt.com/backend-api/wham/usage` | 5 小时 + 7 天双窗口 |
| **Claude** (Pro/Max) | Keychain `Claude Code-credentials` → `api.anthropic.com/api/oauth/usage` | 5 小时 / 7 天 / 月度预算 |
| **智谱 GLM** | `~/.herdr-keys/glm.txt`、`$ZHIPU_API_KEY` 或 cc-switch | 小时窗 + 周窗 |
| **MiniMax** | cc-switch 数据库里的 coding plan token | general / video 双模型 |
| **DeepSeek** | `~/.deepseek/config.toml` 的 `api_key` | 只有余额，无配额窗口 |
| **Gemini** | `~/.gemini/oauth_creds.json` | 需 gemini-cli 登录过；免费层无配额桶 |
| **Grok Bot** | `~/.grokbot/usage.json` | 由 Grok Bot 自己写入，无公开接口 |

没有凭据的方案显示「缺凭据」，不影响其他行。

## 为什么是这种形态

| 方案 | 常驻内存 | CPU | 说明 |
|---|---|---|---|
| 静态壁纸 | 0 | 0 | 每天重新渲染一次，峰值 220 MB / 4 秒 |
| **本项目组件** | **约 50 MB** | **累计 0.002 秒** | 只读文件，空闲每 60 秒一次 `stat` |
| Übersicht / Plash | 150–300 MB | 更高 | 底下是完整 WebKit |

渲染用 AppKit 直接画文字和圆角矩形，没有浏览器引擎，也没有毛玻璃（毛玻璃走 GPU 合成）。

## 安装

### 用发布包

```bash
curl -LO https://github.com/TaiLaiCai/usage-widget/releases/latest/download/usage-widget.tar.gz
tar -xzf usage-widget.tar.gz && cd usage-widget-*
./install.sh
```

### 从源码

```bash
git clone https://github.com/TaiLaiCai/usage-widget.git
cd usage-widget
./install.sh          # 会自动调用 scripts/build.sh 编译
```

需要 Xcode Command Line Tools（`xcode-select --install`）。

### 安装参数

```bash
./install.sh --refresh-seconds 7200   # 两小时刷新一次
./install.sh --no-agents              # 只装文件，不注册开机自启
./install.sh --dry-run                # 只看会做什么
```

安装内容：

```
~/.local/bin/usage-widget         组件二进制（通用，x86_64 + arm64）
~/.local/bin/usage-monitor        采集与渲染（Python 3 标准库）
~/.local/bin/usage-widget-data    数据快照
~/.local/bin/usage-wallpaper      可选：渲染成壁纸
~/.local/share/usage-monitor/     数据与日志
~/Library/LaunchAgents/com.local.usage-widget*.plist
```

## 命令行

```bash
usage-monitor                    # 终端面板
usage-monitor --json             # 机器可读
usage-monitor --html out.html    # 生成自包含网页
usage-monitor --html --wallpaper # 壁纸排版（铺满视口）
usage-monitor --watch 60         # 持续刷新
usage-monitor --local-only       # 不联网，只看本机会话统计
usage-monitor --days 30          # 本地统计回看天数
usage-monitor --units si         # 用 K/M/G 代替 万/亿
```

日常维护：

```bash
# 立即刷新数据
launchctl kickstart -k gui/$(id -u)/com.local.usage-widget-data

# 看日志
tail -5 ~/.local/share/usage-monitor/widget.log

# 改刷新间隔：编辑 plist 里的 StartInterval 后重新加载
launchctl bootout gui/$(id -u)/com.local.usage-widget-data
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.local.usage-widget-data.plist
```

## 笔记本睡眠时的行为

数据采集用 **`StartCalendarInterval` 每小时整点**触发，不是 `StartInterval`。

原因：`StartInterval` 只在系统清醒时累积计时，合盖睡眠期间不推进——实测睡眠 5 小时后 `launchctl print` 里 `runs` 仍是 1，面板会显示几小时前的旧数据。calendar 触发有补跑语义，错过的时间点会在唤醒后执行一次。

睡眠期间不产生任何网络请求；唤醒后几十秒内面板就会更新。

## 架构

```
LaunchAgent (每小时)
      │
      ▼
usage-monitor --json ──► ~/.local/share/usage-monitor/widget-data.json
      │                          │  原子写（tmp + rename）
  各家 HTTP 接口                  ▼
                          usage-widget（常驻）
                          · 每 60 秒 stat 一次文件
                          · 变了才重绘
                          · 零网络、零子进程
```

拆成两层的原因：网络请求有失败、限流、超时，把它们和渲染隔离后，组件进程永远不会因为某个接口挂了而卡住或退出。

## 扩展一个新的数据源

在 `src/usage-monitor.py` 里加一个 `collect_xxx(fetch)` 函数，返回统一结构，然后注册进 `PROVIDERS` 和 `DISPLAY_ORDER`。组件侧不需要改动——它按 JSON 里的 `windows` 数组渲染。

完整契约、字段同义词、渲染规则见 [AGENTS.md](AGENTS.md) 与 [docs/API.md](docs/API.md)。

## 已知限制

- **只能 macOS**：窗口用了 `kCGDesktopWindowLevel`，没有跨平台计划
- **凭据不共享**：读的是本机各 CLI 的登录态，别人装了也得各自登录
- **不会自动刷新 token**：OAuth 过期后需跑一次对应 CLI（`claude -p ok` / `gemini`）。自动刷会轮换 refresh_token，可能导致 CLI 掉登录
- **Gemini 免费层查不到配额**：Google 已停止对 CLI 提供个体免费层，需要付费 Code Assist 许可
- **Grok Bot 靠文件交接**：它没有公开接口，用量由它自己写入 `~/.grokbot/usage.json`
- **未签名未公证**：首次运行可能被 Gatekeeper 拦，见下

## 故障排查

**提示「无法验证开发者」**

```bash
xattr -dr com.apple.quarantine ~/.local/bin/usage-widget
codesign --force --sign - ~/.local/bin/usage-widget
```

**某行显示「取数失败」**

大概率是 token 过期，跑一次对应的 CLI 即可：

```bash
claude -p ok     # 刷新 Claude OAuth
gemini           # 刷新 Gemini OAuth
codex            # 刷新 ChatGPT token
```

**LaunchAgent 装不上，报 `Input/output error`**

launchd 的 label 卡死（常见于 `bootout` 中途被中断）。换个 label 或重启机器即可：

```bash
sed -i '' 's/com.local.usage-widget/com.local.usage-widget2/g' ~/Library/LaunchAgents/com.local.usage-widget*.plist
```

## 卸载

```bash
./uninstall.sh           # 保留数据
./uninstall.sh --purge   # 连数据一起删
```

## 许可

MIT
