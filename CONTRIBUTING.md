# 参与贡献

先读 [AGENTS.md](AGENTS.md)：那里写了架构不变量和数据契约，改代码前必须知道。

## 环境

- macOS 13+，Xcode Command Line Tools（`xcode-select --install`）用于编译组件
- Python 3.9+，**只用标准库**，不要引入第三方包
- 想验证真实接口，需要有对应方案的登录态（没有也能跑 `--local-only`）

## 本地验证

```bash
python3 -m py_compile src/usage-monitor.py     # 采集层
bash -n install.sh uninstall.sh scripts/*.sh    # 脚本
scripts/build.sh                                # 组件（x86_64 + arm64）
python3 src/usage-monitor.py --local-only       # 不联网跑一遍
./install.sh --dry-run                          # 看安装动作
```

提交前自查，确保没把个人路径和凭据写进仓库：

```bash
grep -rn "/Users/" --include="*.sh" --include="*.py" --include="*.swift" . | grep -v '\$HOME'
grep -rn -E "ya29\.|sk-[a-zA-Z0-9]{16,}|Bearer [A-Za-z0-9._-]{20,}" .
```

## 加一个新数据源

1. 在 `src/usage-monitor.py` 写 `collect_xxx(fetch=True)`，用 `_blank("xxx", "显示名")` 起头
2. 注册到 `PROVIDERS` 与 `DISPLAY_ORDER`
3. 凭据读取单独抽一个函数，找不到时 `status="missing"` 并给出可操作提示
4. 组件端不用改：它按 `windows` 数组渲染，窗口标签含 `5 小时`/`5h` 归短窗，含 `周`/`7 天`/`7d` 归长窗

## 接口契约要实测

`docs/API.md` 里的每一段都得是打过的真实请求，格式：

- 请求（方法、端点、必要 header）
- 凭据位置
- **脱敏后**的真实响应样例
- 「字段与坑」：单位、方向（已用 vs 剩余）、错误码含义

凭据一律运行时读取，不许硬编码；样例里邮箱、token、项目号都要打码。

## 提交

- 前缀用 `feat:` / `fix:` / `docs:` / `ci:`，正文写清动机
- 改行为就同步文档（README 能力表、`docs/API.md`、AGENTS 契约）
- 发版：改 `VERSION` 后打 `v*` tag，CI 会自动打包并发布 Release
