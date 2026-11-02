# AGENTS.md

给要修改或复用本项目的 agent 的说明。人类读 [README.md](README.md) 就够。

## 仓库结构

```
src/usage-monitor.py     采集 + 渲染（终端 / HTML / JSON），只依赖标准库
src/usage-widget.swift   AppKit 小组件，读一个 JSON 文件画面板
scripts/usage-widget-data  采集一次并原子写入快照
scripts/usage-wallpaper    可选：渲染成 PNG 当壁纸
scripts/build.sh         编译通用二进制到 bin/
scripts/package.sh       打发布包到 release/
install.sh / uninstall.sh  安装与卸载（路径全部基于 $HOME）
docs/API.md              七家接口的完整契约（含脱敏样例）
```

## 两层架构与不变量

**采集层**（`usage-monitor.py`，一次性进程）负责所有 I/O：

- 六家 HTTP 接口并发拉取（`ThreadPoolExecutor`，各家独立 try/except，互不影响）
- 接口结果按 URL 缓存 90 秒；429/5xx/网络错误时回退上次成功结果
- 本机会话统计按文件 `mtime + size` 增量缓存到 `local-cache.json`
- 三个本地来源口径不同：claude / codex 有逐条 usage（按消息归日）；deepseek-tui 只有整会话 `total_tokens`，按**增量记账**（首次落回会话创建日，之后每次扫描把增量记到今天），且不分输入输出，统一记入 `in_total`
- 输出写到 `widget-data.json`，必须**原子写**（先写 `.tmp` 再 `rename`）

**渲染层**（`usage-widget`，常驻进程）遵守三条硬约束：

1. **不发起任何网络请求**
2. **不启动任何子进程**
3. **只读 `~/.local/share/usage-monitor/widget-data.json`**，每 60 秒 `stat` 一次，mtime 变了才重读重绘

改动时不要破坏这三条——它们是「50 MB / 0 CPU」的来源。

## 数据契约

`widget-data.json` 就是 `usage-monitor --json` 的输出：

```jsonc
{
  "generated_at": "2026-11-01T09:20:00+00:00",
  "providers": [
    {
      "id": "claude",
      "name": "Claude",
      "status": "ok",            // ok | missing | error | local_only
      "plan": "subscription",
      "account": null,
      "metric": null,            // 金额型额度：{label, value, sub}
      "windows": [
        { "label": "5 小时", "used_pct": 0.0,  "detail": null, "reset_at": null },
        { "label": "7 天",   "used_pct": 88.0, "detail": null, "reset_at": "2026-11-02T04:00:00Z" }
      ],
      "notes": ["额外用量（extra usage）未启用"]
    }
  ],
  "summary": {
    "rows": [ { "label": "今日", "burned": 175898700,
                "in_total": 175458710, "cache_read": 151426744, "out": 439990 } ]
  }
}
```

关键点：

- `used_pct` 一律是**已用**百分比（0–100）。接口给「剩余」的要在采集层换算
- `reset_at` 可以是 ISO 8601 / epoch 秒 / epoch 毫秒，渲染层用 `parse_ts` 统一处理
- `burned = in_total + out`。注意各家口径不同：Anthropic 的 `input_tokens` **不含**缓存，OpenAI/Codex 的 `input_tokens` **已含** `cached_input_tokens`——加错会翻倍
- `status != ok` 时 `windows` 通常为空，渲染成错误提示

## 新增一个 provider

在 `src/usage-monitor.py` 里：

1. 写 `def collect_xxx(fetch=True) -> dict`，用 `_blank("xxx", "显示名")` 起头，填 `windows` / `metric` / `notes`
2. 注册到 `PROVIDERS` 元组
3. 加到 `DISPLAY_ORDER` 元组（决定面板顺序，与采集顺序解耦）
4. 凭据读取放在独立函数里，找不到时 `status="missing"` 并给出可操作的提示

组件端**不需要改**——它按 `windows` 数组渲染。窗口标签的归类规则在 `src/usage-widget.swift` 的 `classify()`：含 `5 小时`/`5h` 归短窗，含 `周`/`7 天`/`7d` 归长窗，两者都有就画双段，只有一种就画整条。

## 渲染层布局

界面用**固定槽位**而不是逐项测量，避免字体度量漂移导致重叠：

```
每段 = 标签槽 15 + 4 + 进度条 34 + 4 + 百分比槽 27 = 84
两段 = 84 × 2 + 段间距 12 = 180
```

百分比在槽内右对齐，`100%` 顶满也不会越过段间距。面板宽度 = 最宽内容 + 两侧各 12，所以右侧不会留空档。

改尺寸只动文件顶部的常量；`layoutContent()` 和 `drawSegment()` 共用同一组常量，不要在绘制里另算。

## 构建与验证

```bash
scripts/build.sh        # 需要 swiftc，产出 bin/usage-widget（x86_64 + arm64）
python3 -m py_compile src/usage-monitor.py
./install.sh --dry-run  # 不落盘，检查动作
scripts/package.sh      # 打 release/usage-widget-<version>.tar.gz
```

无 GUI 环境下验证组件是否真的起来了：

```bash
launchctl print gui/$(id -u)/com.local.usage-widget | grep -E "state|pid"
```

要确认窗口落在桌面层级（而不只是进程活着），用 CoreGraphics 列窗口：`layer` 应为 `kCGDesktopIconWindow - 1`（约 `-2147483604`）。

## 环境坑（实测踩过）

- **`StartInterval` 在睡眠期不推进**：笔记本上用它会导致合盖数小时后数据仍是旧的。整小时间隔一律用 `StartCalendarInterval`，唤醒后会补跑
- **launchd label 会卡死**：`bootout` 被中断后，同名 label 再 `bootstrap` 会稳定报 `Input/output error`。换 label 或重启机器
- **`/usr/bin/top`、`ps`、`log` 在沙箱里不可用**：测内存用 `proc_pid_rusage` 的 `ri_resident_size`
- **macOS 的 `/usr/bin/time -l` 报字节**，不是 KB
- **无头 Chrome 启动会被沙箱拒**（`sandbox_extension_issue_file_to_process`）；但由 LaunchAgent 拉起的进程不受此限制
- **Chrome 默认会加载日常 profile**：无头截图必须指定 `--user-data-dir` 到临时目录，否则会带起扩展/同步

## 提交前自检

```bash
bash -n install.sh uninstall.sh scripts/*.sh
python3 -m py_compile src/usage-monitor.py
grep -rn "/Users/" --include="*.sh" --include="*.py" --include="*.swift" . | grep -v "\$HOME"
```

最后一条确保没有把个人路径写死进仓库。
