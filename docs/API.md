# Coding Plan 额度接口速查

实测环境：macOS 26 / 2026-11-01。全部接口只需普通 HTTP，不需要官方 SDK。
所有 `Authorization` 均为 `Bearer <token>`，token 一律从本机凭据文件读取，**不要硬编码**。

## 一览

| 方案 | 方法 | 端点 | 凭据来源 |
|---|---|---|---|
| Codex (ChatGPT) | GET | `chatgpt.com/backend-api/wham/usage` | `~/.codex/auth.json` |
| Claude (Pro/Max) | GET | `api.anthropic.com/api/oauth/usage` | macOS Keychain / `~/.claude/.credentials.json` |
| 智谱 GLM | GET | `open.bigmodel.cn/api/monitor/usage/quota/limit` | `~/.herdr-keys/glm.txt` |
| MiniMax | GET | `api.minimaxi.com/v1/coding_plan/remains` | cc-switch 数据库 |
| Antigravity | POST ×2 | `cloudcode-pa.googleapis.com/v1internal:*` | `~/.gemini/oauth_creds.json`（配额需官方凭据） |
| DeepSeek | GET | `api.deepseek.com/user/balance` | `~/.deepseek/config.toml` |
| Grok Bot | — | 无接口 | 密封存储，不可读 |

---

## 1. Codex（ChatGPT 订阅）

### 请求

```
GET https://chatgpt.com/backend-api/wham/usage
Authorization: Bearer <access_token>
chatgpt-account-id: <account_id>
User-Agent: codex-cli
Accept: application/json
```

### 凭据

`~/.codex/auth.json`

```json
{
  "auth_mode": "chatgpt",
  "tokens": {
    "access_token": "<jwt>",
    "refresh_token": "<...>",
    "id_token": "<...>",
    "account_id": "<uuid>"
  }
}
```

`auth_mode` 若是 `api_key` 而没有 `tokens`，表示走按量计费，**没有额度接口可用**。

### 脱敏样例

```json
{
  "user_id": "user-<masked>",
  "account_id": "<masked-uuid>",
  "email": "<masked>@88.com",
  "plan_type": "plus",
  "rate_limit": {
    "allowed": false,
    "limit_reached": true,
    "primary_window": {
      "used_percent": 0,
      "limit_window_seconds": 18000,
      "reset_after_seconds": 18000,
      "reset_at": 1793532052
    },
    "secondary_window": {
      "used_percent": 100,
      "limit_window_seconds": 604800,
      "reset_after_seconds": 260830,
      "reset_at": 1793774882
    }
  },
  "model_usage": {
    "gpt-6-astra": { "available": false, "available_at": "2026-11-04T06:48:02Z", "credits_would_enable": true }
  },
  "credits": { "has_credits": false, "unlimited": false, "balance": "0" },
  "rate_limit_upsell": { "banner_type": "plus_rate_limit_reached" }
}
```

### 字段与坑

- `primary_window` = 5 小时窗口（18000 秒），`secondary_window` = 7 天窗口（604800 秒）
- `reset_at` 是 **epoch 秒**
- 已用百分比直接取 `used_percent`，100 表示真的耗尽
- `model_usage.<model>.available=false` 要在 UI 上单独提示，别混进额度条

---

## 2. Claude（Pro / Max）

### 请求

```
GET https://api.anthropic.com/api/oauth/usage
Authorization: Bearer <oauth_access_token>
anthropic-beta: oauth-2025-04-20
User-Agent: claude-cli/2.0.0
Accept: application/json
```

### 凭据

macOS Keychain，服务名 `Claude Code-credentials`：

```bash
security find-generic-password -s "Claude Code-credentials" -w
```

输出是 JSON，取 `claudeAiOauth.accessToken`。非 macOS 读 `~/.claude/.credentials.json`，结构相同。

### 脱敏样例

```json
{
  "five_hour": { "utilization": 0.0, "resets_at": null, "limit_dollars": null, "used_dollars": null, "remaining_dollars": null, "locked_reason": null },
  "seven_day": { "utilization": 88.0, "resets_at": "2026-11-02T03:59:59Z", "limit_dollars": null, "used_dollars": null, "remaining_dollars": null, "locked_reason": null },
  "seven_day_opus": null,
  "seven_day_sonnet": null,
  "iguana_necktie": {
    "utilization": 85.480715,
    "resets_at": "2026-11-05T07:59:00Z",
    "limit_dollars": 100,
    "used_dollars": 85.480715,
    "remaining_dollars": 14.519285,
    "locked_reason": null
  },
  "extra_usage": { "is_enabled": false }
}
```

### 字段与坑

- `utilization` 是**已用百分比**（0–100），不是剩余
- `resets_at` 是 ISO 8601
- 返回里混着大量内部代号（`tangelo`、`iguana_necktie`、`starry_waterfall`、`granite_teacup`…），**不要按名字硬编码**。推荐规则：凡是非 null 且含 `utilization` 的对象都渲染；带 `limit_dollars` 的按金额型额度展示
- 已知窗口名映射：`five_hour`→5 小时，`seven_day`→7 天，`seven_day_opus`/`seven_day_sonnet`/`seven_day_oauth_apps`/`seven_day_cowork` 同理
- **限流极其敏感**：短时间内连续调用会返回 `429 rate_limit_error`。必须缓存，并把 429 视为可回退（用上次成功结果），不要当致命错误

---

## 3. 智谱 GLM Coding Plan

### 请求

```
GET https://open.bigmodel.cn/api/monitor/usage/quota/limit
Authorization: Bearer <key>
Accept: application/json
```

### 凭据

`~/.herdr-keys/glm.txt`（单行明文 key），或环境变量 `ZHIPU_API_KEY` / `GLM_API_KEY`。

### 脱敏样例

```json
{
  "code": 200,
  "msg": "操作成功",
  "data": {
    "limits": [
      { "type": "CREDIT_LIMIT", "unit": 3, "number": 5, "usage": 12000,
        "currentValue": 286, "remaining": 11713, "percentage": 2,
        "nextResetTime": 1793528427865 },
      { "type": "CREDIT_LIMIT", "unit": 6, "number": 1, "usage": 60000,
        "currentValue": 286, "remaining": 59713, "percentage": 1,
        "nextResetTime": 1794109151984 }
    ],
    "level": "pro"
  },
  "success": true
}
```

### 字段与坑

- `unit`：`3` = 小时，`6` = 周；配合 `number` 拼标签（`5` + `3` → "5 小时"）
- `usage` = 窗口总量，`currentValue` = 已用，`remaining` = 剩余
- `percentage` 是**整数取整**，只有 2% 这种粗精度。要准确就自己算 `currentValue / usage * 100`
- `nextResetTime` 是**毫秒**
- `success=false` 时 body 里没有 `data`

---

## 4. MiniMax Coding Plan

### 请求

```
GET https://api.minimaxi.com/v1/coding_plan/remains
Authorization: Bearer <coding_plan_token>
Accept: application/json
```

### 凭据

cc-switch 数据库 `~/.cc-switch/cc-switch.db`，表 `providers`，`name='MiniMax'` 那一行的 `settings_config`：

```sql
SELECT settings_config FROM providers WHERE name = 'MiniMax';
```

解析出的 JSON 里取 `env.ANTHROPIC_AUTH_TOKEN`（`sk-cp-` 前缀即 coding plan 令牌）。

### 脱敏样例

```json
{
  "model_remains": [
    {
      "start_time": 1793498400000,
      "end_time": 1793516400000,
      "remains_time": 2345632,
      "current_interval_total_count": 0,
      "current_interval_usage_count": 0,
      "model_name": "general",
      "current_weekly_total_count": 0,
      "current_weekly_usage_count": 0,
      "weekly_start_time": 1792944000000,
      "weekly_end_time": 1793548800000,
      "weekly_remains_time": 34745632,
      "current_interval_status": 2,
      "current_interval_remaining_percent": 0,
      "current_weekly_status": 1,
      "current_weekly_remaining_percent": 51,
      "weekly_boost_permille": 1500
    }
  ],
  "base_resp": { "status_code": 0, "status_msg": "success" }
}
```

### 字段与坑

- 给的是**剩余**百分比：`已用 = 100 - current_interval_remaining_percent`
- `model_name` 可能有多个（`general`、`video`），逐个渲染
- `weekly_boost_permille=1500` 表示周额度 1.5× 加成，除 1000 得到倍数
- 所有时间字段是**毫秒**
- `base_resp.status_code != 0` 时没有数据，`status_msg` 是错误原因

---

## 5. Antigravity（Google AI Pro）

Antigravity 是 Gemini 的官方 IDE。它的额度挂在 cloudcode-pa 上，但**套餐层级能查、模型配额查不到**——两件事分开说，别混成一个「失败」。

### 第一步：查套餐层级（可用）

```
POST https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist
Authorization: Bearer <oauth_access_token>
Content-Type: application/json
User-Agent: Antigravity/1.138.0
```

```json
{
  "metadata": {
    "ideName": "ANTIGRAVITY",
    "pluginType": "GEMINI",
    "ideVersion": "1.138.0",
    "platform": "DARWIN_ARM64",
    "updateChannel": "stable"
  },
  "mode": "HEALTH_CHECK"
}
```

- `ideName` 取 `ClientMetadata.IdeType` 枚举（`ANTIGRAVITY` / `JETSKI` / `VSCODE` / `GEMINI_CLI` …）
- `pluginType` 取 `ClientMetadata.PluginType` 枚举，Antigravity 用的是 **`GEMINI`**（不是 `ANTIGRAVITY`，那个是 IdeType）
- `ideVersion` 用本机 `/Applications/Gemini.app/Contents/Info.plist` 的 `CFBundleShortVersionString`
- `platform` 用 `DARWIN_ARM64` / `DARWIN_AMD64`

### 脱敏样例（实测，Google AI Pro 账号）

```json
{
  "currentTier": {
    "id": "standard-tier",
    "name": "Antigravity",
    "description": "Unlimited coding assistant with the most powerful Gemini models",
    "userDefinedCloudaicompanionProject": true,
    "usesGcpTos": true
  },
  "paidTier": {
    "id": "g1-pro-tier",
    "name": "Google AI Pro",
    "upgradeSubscriptionUri": "https://antigravity.google/g1-upgrade"
  },
  "cloudaicompanionProject": "aicode-consumers",
  "gcpManaged": false,
  "ineligibleTiers": [
    {
      "reasonCode": "UNSUPPORTED_LOCATION",
      "reasonMessage": "Your current account is not eligible for Antigravity, because it is not currently available in your location.",
      "tierId": "free-tier",
      "tierName": "Antigravity"
    }
  ],
  "manageSubscriptionUri": "https://accounts.google.com/AccountChooser?Email=<masked>&continue=https://one.google.com/settings"
}
```

### 第二步：查模型配额（403，第三方读不到）

```
POST https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary
{ "project": "<第一步的 cloudaicompanionProject>" }
```

```json
{ "error": { "code": 403, "message": "The caller does not have permission",
             "status": "PERMISSION_DENIED" } }
```

同一份 token 调 `fetchAvailableModels` 也是 403，而 `loadCodeAssist` / `fetchUserInfo` 都正常——
说明不是请求写错，是**权限不够**：这两个接口要 Antigravity 自己的 OAuth 客户端（`auth/aicode` scope）。

Antigravity 的凭据在 `~/Library/Application Support/com.google.GeminiMacOS/Data/user1/auth`，
是加密容器（文件头 `2ebafeca`），由本机 host bridge（`--use_host_auth`，直连 401）转交给 `agy` 子进程，
第三方既读不到也解不开。gemini-cli 的 token 只有 `cloud-platform` scope，所以配额这条路是封死的。

**结论：第三方只能拿到套餐层级，拿不到 Antigravity 的模型额度。** 想要真配额只能用官方 App 看。

### 配额结构（有权限的凭据时才用得上）

`retrieveUserQuotaSummary` 的 proto 形状（`google/internal/cloud/code/v1internal/quota_summary.proto`）：

```
RetrieveUserQuotaSummaryRequest { project }
RetrieveUserQuotaSummaryResponse {
  groups: [{ display_name, description, buckets: [QuotaSummaryBucket] }]
  buckets: [QuotaSummaryBucket]        // 无分组时的平铺形式
  description
}
QuotaSummaryBucket { bucket_id, display_name, description, window,
                     remaining_fraction, remaining_amount, disabled, reset_time }
```

- `remainingFraction` 是**剩余**比例（0–1），已用 = `1 - remainingFraction`
- `window` 是字符串，值域未确认；归一化时把 `5h` / `5-hour` / `five_hour` 映射成「5 小时」，
  `weekly` / `7d` / `7-day` 映射成「7 天」，否则对不上组件的窗口分类
- `disabled=true` 表示该桶当前不可用，要单独标出来，别当 0%
- 旧的 `retrieveUserQuota`（`buckets[{modelId, remainingAmount, remainingFraction, resetTime}]`）
  同样 403，作为兜底解析保留

### 本地活跃度（拿不到配额时的替代口径）

退而统计本地真实使用量，数据在 agy 维护的会话索引
`~/.gemini/antigravity/conversation_summaries.db`（sqlite）：

```sql
SELECT conversation_id, step_count, last_modified_time, last_user_input_time
FROM conversation_summaries;
```

- 一张 `conversation_summaries` 就是全部会话，`step_count` 是代理步数，按 `last_modified_time` 聚合到日
- 没有 token 口径，只能给「会话数 / 步数」，别伪装成 token
- 刚装完没聊过时表是空的，要渲染成「索引还是空的」，不是 0

---

## 6. DeepSeek（按 token 计费，只有余额）

### 请求

```
GET https://api.deepseek.com/user/balance
Authorization: Bearer <api_key>
Accept: application/json
```

### 凭据

`~/.deepseek/config.toml` 的 `api_key` 字段，或环境变量 `DEEPSEEK_API_KEY`。

### 脱敏样例

```json
{
  "is_available": true,
  "balance_infos": [
    { "currency": "CNY", "total_balance": "138.33",
      "granted_balance": "0.00", "topped_up_balance": "138.33" }
  ]
}
```

### 字段与坑

- 所有金额字段都是**字符串**，需要自己转数字
- 多币种时 `balance_infos` 有多项，逐项展示
- `is_available=false` 表示账户不可用（欠费/冻结），要显著提示
- 这是**按量计费**的余额，没有额度窗口，不要硬套百分比进度条

---

## 7. Grok Bot（文件对接，无 HTTP 接口）

凭据是密封存储，没有可调用的额度接口。改为**由 grokbot 自己查完写文件，监视器读取**。

约定：用户对 grokbot 说「更新额度」→ grokbot 查询 → 写入 `~/.grokbot/usage.json` → 监视器刷新即可见。

### 文件位置（按优先级）

| 顺序 | 路径 |
|---|---|
| 1 | 环境变量 `GROKBOT_USAGE_FILE` 指向的文件 |
| 2 | `~/.grokbot/usage.json` |
| 3 | `~/.grokbot/usage/current.json` |

### 字段 schema

只有 `updated_at` 是强烈建议必填，其余全部可选；`windows` 和 `metric` 至少给一个。

```json
{
  "provider": "grokbot",
  "updated_at": "2026-11-01T07:05:00Z",
  "plan": "Grok Pro",
  "account": "demo@example.com",
  "windows": [
    {
      "label": "5 小时",
      "used_pct": 42.5,
      "reset_at": "2026-11-01T09:05:00Z",
      "detail": "可选补充说明"
    },
    {
      "label": "7 天",
      "remaining_percent": 18,
      "reset_at": "2026-11-05T00:00:00Z"
    }
  ],
  "metric": {
    "label": "剩余额度",
    "value": "$12.30",
    "sub": "订阅 $30/月"
  },
  "notes": ["grokbot 自己查的，非公开接口"]
}
```

### 字段同义词（已做兼容，任选其一即可）

| 用途 | 可用字段名 |
|---|---|
| 窗口名 | `label` / `name` / `model` / `model_name` / `window` |
| 已用百分比 | `used_pct` / `used_percent` / `usedPercent` / `percent` / `percentage` / `utilization` |
| 剩余百分比 | `remaining_pct` / `remaining_percent` / `remainingPercent` / `remaining`（自动换算成已用） |
| 重置时间 | `reset_at` / `reset` / `resets_at` / `reset_time` / `resetTime` / `end_time` |
| 补充说明 | `detail` / `note` / `description` / `text` |
| 时间戳 | `updated_at` / `generated_at` / `ts` / `timestamp` / `time` |
| 金额型额度 | 顶层 `metric` 或 `balance`，内含 `label` / `value` / `sub` |
| 备注 | `notes`（数组）或 `note`（字符串） |
| 套餐 / 账号 | `plan` / `tier` / `plan_type`，`account` / `email` / `user` |

### 写入要求

- **原子写**：先写 `usage.json.tmp` 再 `rename` 覆盖，避免监视器读到半截 JSON
- `updated_at` 用 ISO 8601（带时区）或 epoch 秒/毫秒，三种都认
- 百分比方向不用纠结，给已用或剩余都行
- 数据超过 6 小时会提示「可能已过期」
- 校验方式：`usage-monitor --local-only`（不打网络，只读文件），或 `usage-monitor --json` 看 `providers[id=grokbot]`

### 为什么不用原有的密封存储

- `~/.grokbot/local-exec-daemon-credential.json` = `{"sandSealedFile": 1, "data": "<密文>"}`
- `~/Library/Application Support/Grok Bot/` 下是 Electron 的 `box-secrets` 与 Cookie 库，同样密封
- `~/.grokbot/local-exec-daemon.log` 只有连接日志，无用量数据

所以「文件交接」是唯一可行且稳定的方式。

## 实现建议

### 时间戳单位不统一

| 来源 | 字段 | 单位 |
|---|---|---|
| Codex | `reset_at` | epoch 秒 |
| 智谱 | `nextResetTime` | epoch 毫秒 |
| MiniMax | `end_time` / `weekly_end_time` | epoch 毫秒 |
| Claude | `resets_at` | ISO 8601 |
| Antigravity | `resetTime` | ISO 8601 |

统一转成 UTC 再算「还剩多久」。解析函数要能接受秒、毫秒、ISO 三种输入。

### 必须做的防御

- **缓存**：建议 90 秒 TTL。Claude 的接口频繁调用必 429
- **回退**：429 / 5xx / 网络错误时用上次成功结果，并在 UI 上标注「数据取自 N 秒前的缓存」
- **单点容错**：一家失败不能影响其他家，各家独立 try/catch
- **耗尽判定**：`used_pct >= 100` 就是真的耗尽，Codex 和 MiniMax 都会出现 100%

### 展示语义

- 进度条表示**已用**百分比，颜色阈值建议 60% 转黄、85% 转红
- 金额型（DeepSeek 余额、Claude `limit_dollars`）不要画进度条，用大字号数字 + 拆分说明
- 三家给的是「已用」（Codex / Claude / 智谱），MiniMax 和 Antigravity 给的是「剩余」，记得转换

### 凭据安全

- 一律运行时从本机文件读，不要写进代码或提交到仓库
- 日志里禁止打印 token，输出前统一打码
