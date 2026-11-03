## 改了什么

<!-- 一句话说清动机，别只贴 diff -->

## 影响面

- [ ] 采集层（`src/usage-monitor.py`）
- [ ] 渲染层（`src/usage-widget.swift`）
- [ ] 安装 / 打包脚本
- [ ] 仅文档

## 自检

- [ ] `python3 -m py_compile src/usage-monitor.py`
- [ ] `bash -n install.sh uninstall.sh scripts/*.sh`
- [ ] `python3 src/usage-monitor.py --local-only` 能跑通
- [ ] 改了渲染层就跑过 `scripts/build.sh`
- [ ] 没有硬编码个人路径 / 凭据（`grep -rn "/Users/" ... | grep -v '\$HOME'`）
- [ ] 改行为就同步了 README / `docs/API.md` / AGENTS.md

## 接口类改动

<!-- 新增或纠正 provider 时填，贴脱敏后的真实响应 -->
