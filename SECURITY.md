# 安全说明

这个工具会读你本机的登录凭据，所以把边界写清楚。

## 会读哪些凭据

全部来自本机文件或 Keychain，**只读，不写回**：

| 来源 | 凭据 |
|---|---|
| Codex | `~/.codex/auth.json` 的 `tokens.access_token` |
| Claude | Keychain `Claude Code-credentials`，其次 `~/.claude/.credentials.json` |
| 智谱 GLM | `~/.herdr-keys/glm.txt`、`$ZHIPU_API_KEY`、cc-switch 数据库 |
| MiniMax | cc-switch 数据库里的 coding plan token |
| DeepSeek | `~/.deepseek/config.toml` 的 `api_key` |
| Antigravity | `~/.gemini/oauth_creds.json` |

## 数据流向

只向各家官方端点发请求，没有任何中间服务器：

- `chatgpt.com/backend-api/wham/usage`
- `api.anthropic.com/api/oauth/usage`
- `open.bigmodel.cn/api/monitor/usage/quota/limit`
- `api.minimaxi.com/v1/coding_plan/remains`
- `api.deepseek.com/user/balance`
- `cloudcode-pa.googleapis.com/v1internal:*`

组件进程（`usage-widget`）**不联网、不启子进程**，只读 `~/.local/share/usage-monitor/widget-data.json`。
数据快照只含额度数值、模型名、账号邮箱，不含 token。

## 不会做的事

- 不写回任何凭据文件，不刷新 OAuth token（见 README「已知限制」）
- 不上传凭据或用量数据
- 不在日志里打印 token：错误信息统一截断，输出前打码

## 报告漏洞

凭据泄漏、越权读取、命令注入一类问题，请用
[GitHub Security Advisory](https://github.com/TaiLaiCai/usage-widget/security/advisories/new)
私下报告，不要开 public issue 贴 token。

普通 bug 请走 [Issues](https://github.com/TaiLaiCai/usage-widget/issues)，贴日志前先把 `ya29.`、`sk-`、`Bearer ` 开头的串删掉。
