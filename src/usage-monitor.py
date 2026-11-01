#!/usr/bin/env python3
"""
usage-monitor — 五个 coding plan 的额度监视器

覆盖：
  Codex (ChatGPT Plus / Pro)   chatgpt.com/backend-api/wham/usage
  Claude (Pro / Max)           api.anthropic.com/api/oauth/usage
  智谱 GLM Coding Plan          open.bigmodel.cn/api/monitor/usage/quota/limit
  MiniMax Coding Plan          api.minimaxi.com/v1/coding_plan/remains
  Gemini 订阅                   cloudcode-pa.googleapis.com（需 gemini-cli OAuth 登录）
  DeepSeek 余额                 api.deepseek.com/user/balance（按 token 计费，只有余额）
  Grok Bot                     无公开额度接口（凭据为 Electron 密封存储，仅显示状态）

用法：
  usage-monitor                终端面板
  usage-monitor --watch 60     每 60 秒刷新
  usage-monitor --json         机器可读
  usage-monitor --html         生成报告到 ~/.local/share/usage-monitor/report.html
  usage-monitor --html a.html --open   生成并打开
  usage-monitor --local-only   不打网络，只看本机会话统计
  usage-monitor --days 7       本地统计回看天数（默认 7）

只依赖 Python 标准库。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

HOME = Path.home()
CC_DB = HOME / ".cc-switch" / "cc-switch.db"
TIMEOUT = 12

# ── 颜色 ────────────────────────────────────────────────────────────────
R = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
_WANT_COLOR = sys.stdout.isatty()


def color(text, code):
    if not _WANT_COLOR or not code:
        return text
    return f"{code}{text}{R}"


def pct_color(pct):
    if pct is None:
        return ""
    if pct >= 85:
        return RED
    if pct >= 60:
        return YELLOW
    return GREEN


# ── 时间与文本工具 ──────────────────────────────────────────────────────
def parse_ts(value):
    """接受 epoch 秒 / epoch 毫秒 / ISO 字符串，返回 aware datetime 或 None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        x = float(value)
        if x > 1e12:
            x /= 1000.0
        if x <= 0:
            return None
        return datetime.fromtimestamp(x, tz=timezone.utc)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if s.isdigit():
            return parse_ts(int(s))
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def fmt_remaining(dt):
    if dt is None:
        return "—"
    delta = (dt - datetime.now(timezone.utc)).total_seconds()
    if delta <= 0:
        return "已重置"
    days, rem = divmod(int(delta), 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}天{hours}小时"
    if hours:
        return f"{hours}小时{minutes}分"
    return f"{minutes}分"


def fmt_ago(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} 秒前"
    if seconds < 3600:
        return f"{seconds // 60} 分钟前"
    return f"{seconds // 3600} 小时前"


def dwidth(s):
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def pad(s, width):
    return s + " " * max(0, width - dwidth(s))


_UNITS = "cn"  # cn = 万/亿，si = K/M/G
_WALLPAPER = False  # 壁纸排版：铺满视口、放大字号、去掉逐日明细


def human(n):
    """token 数量格式化。默认中文单位：万 / 亿。"""
    n = float(n or 0)
    if _UNITS == "cn":
        if abs(n) >= 1e8:
            value = n / 1e8
            return f"{value:.2f} 亿" if abs(value) < 100 else f"{value:.0f} 亿"
        if abs(n) >= 1e4:
            value = n / 1e4
            return f"{value:.1f} 万" if abs(value) < 1000 else f"{value:.0f} 万"
        return str(int(n))
    for unit, scale in (("G", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(n) >= scale:
            return f"{n / scale:.1f}{unit}"
    return str(int(n))


def render_bar(pct, width=14):
    pct = max(0.0, min(100.0, float(pct or 0)))
    filled = int(round(pct / 100.0 * width))
    return "█" * filled + "░" * (width - filled)


# ── HTTP ────────────────────────────────────────────────────────────────
def _http_raw(url, headers, method="GET", body=None):
    """返回 (status_code, payload)。网络错误时 status=0 且 payload 含 _error。"""
    payload = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers = dict(headers)
        headers.setdefault("Content-Type", "application/json")
    req = Request(url, headers=headers, data=payload, method=method)
    try:
        with urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw)
            except ValueError:
                return resp.status, {"_raw": raw[:400]}
    except HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        return exc.code, {"_error": body or str(exc)}
    except (URLError, TimeoutError, OSError) as exc:
        return 0, {"_error": str(getattr(exc, "reason", exc))}


# ── 缓存与错误文案 ──────────────────────────────────────────────────────
CACHE_PATH = HOME / ".local" / "share" / "usage-monitor" / "cache.json"
_DEFAULT_TTL = 90
_cache_ttl = _DEFAULT_TTL
_cache_hits = []
_CACHE_LOCK = threading.Lock()


def _load_cache():
    try:
        return json.loads(CACHE_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _save_cache(data):
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(data, ensure_ascii=False))
    except OSError:
        pass


def http_json(url, headers, method="GET", body=None):
    """带 TTL 缓存和限流兜底的请求。

    命中新鲜缓存直接返回；请求被限流（429/5xx/网络错误）时退回上次成功结果，
    避免 --watch 反复打爆官方接口。
    """
    key = f"{method} {url}"
    with _CACHE_LOCK:
        cache = _load_cache()
        entry = cache.get(key)
    now = time.time()
    if entry and _cache_ttl > 0 and now - entry.get("ts", 0) < _cache_ttl:
        _cache_hits.append(int(now - entry.get("ts", 0)))
        return entry.get("status", 200), entry.get("payload")
    status, payload = _http_raw(url, headers, method, body)
    if status == 200:
        with _CACHE_LOCK:
            fresh = _load_cache()  # 重读，别覆盖别的线程刚写的
            fresh[key] = {"ts": now, "status": status, "payload": payload}
            _save_cache(fresh)
        return status, payload
    if entry and status in (0, 429, 500, 502, 503, 504):
        age = int(now - entry.get("ts", 0))
        _cache_hits.append(age)
        payload = dict(entry.get("payload") or {})
        return entry.get("status", 200), payload
    return status, payload


def _fmt_err(status, data):
    """把接口错误整理成一行可读文案。"""
    if isinstance(data, dict):
        raw = data.get("_error") or data.get("_raw") or data.get("msg") or ""
    else:
        raw = str(data)
    msg = " ".join(str(raw).split())
    if status in (429, 503):
        return f"接口限流（HTTP {status}），稍后再试" + (f"：{msg[:90]}" if msg else "")
    if status == 0:
        return f"网络不可达：{msg[:120]}" if msg else "网络不可达"
    return f"HTTP {status}：{msg[:150]}" if msg else f"HTTP {status}"


# ── 凭据发现 ────────────────────────────────────────────────────────────
def _cc_providers():
    """读 cc-switch 的 provider 配置，用于兜底找国产模型 key。"""
    if not CC_DB.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{CC_DB}?mode=ro", uri=True, timeout=3)
        try:
            rows = con.execute("select name, settings_config from providers").fetchall()
        finally:
            con.close()
        out = []
        for name, cfg in rows:
            try:
                parsed = json.loads(cfg or "{}")
            except ValueError:
                continue
            out.append((name or "", parsed))
        return out
    except Exception:
        return []


def _cc_find_token(*keywords):
    """在 cc-switch 里按 provider 名字模糊匹配，返回 (token, provider_name)。"""
    for name, cfg in _cc_providers():
        low = name.lower()
        if not any(kw in low for kw in keywords):
            continue
        env = cfg.get("env") or {}
        for field in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "API_KEY"):
            token = env.get(field)
            if token:
                return token, name
    return None, None


def _claude_token():
    """优先 macOS Keychain，其次 ~/.claude/.credentials.json。"""
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            payload = json.loads(proc.stdout)
            token = (payload.get("claudeAiOauth") or {}).get("accessToken")
            if token:
                return token, "Keychain"
    except Exception:
        pass
    cred = HOME / ".claude" / ".credentials.json"
    if cred.exists():
        try:
            payload = json.loads(cred.read_text())
            token = (payload.get("claudeAiOauth") or {}).get("accessToken")
            if token:
                return token, str(cred)
        except Exception:
            pass
    return None, None


def _zhipu_token():
    for env_name in ("ZHIPU_API_KEY", "GLM_API_KEY", "BIGMODEL_API_KEY"):
        token = os.environ.get(env_name)
        if token:
            return token.strip(), f"env:{env_name}"
    for path in (HOME / ".herdr-keys" / "glm.txt", HOME / ".herdr-keys" / "zhipu.txt"):
        if path.exists():
            token = path.read_text().strip()
            if token:
                return token, str(path)
    return _cc_find_token("glm", "智谱", "zhipu", "bigmodel")


def _minimax_token():
    for env_name in ("MINIMAX_API_KEY", "MINIMAX_CN_API_KEY"):
        token = os.environ.get(env_name)
        if token:
            return token.strip(), f"env:{env_name}"
    return _cc_find_token("minimax")


# ── 各家采集 ────────────────────────────────────────────────────────────
def _blank(pid, name):
    return {"id": pid, "name": name, "status": "ok", "plan": None, "account": None,
            "windows": [], "notes": [], "source": None, "metric": None}


def collect_codex(fetch=True):
    item = _blank("codex", "Codex")
    auth_path = HOME / ".codex" / "auth.json"
    if not auth_path.exists():
        item.update(status="missing", notes=["未找到 ~/.codex/auth.json"])
        return item
    try:
        auth = json.loads(auth_path.read_text())
    except Exception as exc:
        item.update(status="error", notes=[f"auth.json 解析失败：{exc}"])
        return item
    tokens = auth.get("tokens") or {}
    token = tokens.get("access_token")
    if not token:
        item.update(status="missing",
                    notes=[f"auth_mode={auth.get('auth_mode')}，无 ChatGPT token（API key 模式没有额度接口）"])
        return item
    item["source"] = str(auth_path)
    if not fetch:
        return item
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "codex-cli",
        "Accept": "application/json",
    }
    if tokens.get("account_id"):
        headers["chatgpt-account-id"] = tokens["account_id"]
    status, data = http_json("https://chatgpt.com/backend-api/wham/usage", headers)
    if status != 200:
        item.update(status="error", notes=[_fmt_err(status, data)])
        if status in (401, 403):
            item["notes"].append("token 可能已过期，运行 codex 重新登录即可刷新")
        return item
    item["plan"] = data.get("plan_type")
    item["account"] = data.get("email")
    limits = data.get("rate_limit") or {}
    for key, label in (("primary_window", "5 小时"), ("secondary_window", "7 天")):
        window = limits.get(key)
        if isinstance(window, dict) and window.get("used_percent") is not None:
            item["windows"].append({
                "label": label,
                "used_pct": float(window["used_percent"]),
                "reset": parse_ts(window.get("reset_at")),
            })
    credits = data.get("credits") or {}
    if credits:
        item["notes"].append(
            f"credits 余额 {credits.get('balance')}（has_credits={credits.get('has_credits')}）")
    if limits.get("limit_reached"):
        item["notes"].append("当前已触发限额")
    for model, info in (data.get("model_usage") or {}).items():
        if isinstance(info, dict) and info.get("available") is False:
            when = parse_ts(info.get("available_at"))
            item["notes"].append(f"{model} 不可用，{fmt_remaining(when)}后恢复")
    return item


CLAUDE_WINDOW_LABELS = {
    "five_hour": "5 小时",
    "seven_day": "7 天",
    "seven_day_opus": "7 天 Opus",
    "seven_day_sonnet": "7 天 Sonnet",
    "seven_day_oauth_apps": "7 天 OAuth",
    "seven_day_cowork": "7 天 Cowork",
}


def collect_claude(fetch=True):
    item = _blank("claude", "Claude")
    token, source = _claude_token()
    if not token:
        item.update(status="missing",
                    notes=["未找到 OAuth 凭据（Keychain `Claude Code-credentials` 或 ~/.claude/.credentials.json）"])
        return item
    item["source"] = source
    if not fetch:
        return item
    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "claude-cli/2.0.0",
        "Accept": "application/json",
    }
    status, data = http_json("https://api.anthropic.com/api/oauth/usage", headers)
    if status != 200:
        item.update(status="error", notes=[_fmt_err(status, data)])
        return item
    item["plan"] = data.get("plan_type") or "subscription"
    for key, value in data.items():
        if not isinstance(value, dict):
            continue
        if value.get("utilization") is None:
            continue
        label = CLAUDE_WINDOW_LABELS.get(key)
        if label is None:
            label = "月度预算" if value.get("limit_dollars") else key.replace("_", " ").title()
        window = {
            "label": label,
            "used_pct": float(value["utilization"]),
            "reset": parse_ts(value.get("resets_at")),
        }
        if value.get("limit_dollars"):
            window["detail"] = (
                f"${float(value.get('used_dollars') or 0):.2f} / ${float(value['limit_dollars']):.2f}"
                f"（剩 ${float(value.get('remaining_dollars') or 0):.2f}）")
        item["windows"].append(window)
    extra = data.get("extra_usage") or {}
    if isinstance(extra, dict) and extra.get("is_enabled") is False:
        item["notes"].append("额外用量（extra usage）未启用")
    return item


ZHIPU_UNITS = {1: "分钟", 2: "天", 3: "小时", 4: "月", 6: "周"}


def collect_zhipu(fetch=True):
    item = _blank("zhipu", "智谱 GLM")
    token, source = _zhipu_token()
    if not token:
        item.update(status="missing", notes=["未找到 GLM key（~/.herdr-keys/glm.txt、环境变量或 cc-switch）"])
        return item
    item["source"] = source
    if not fetch:
        return item
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    status, data = http_json("https://open.bigmodel.cn/api/monitor/usage/quota/limit", headers)
    if status != 200:
        item.update(status="error", notes=[_fmt_err(status, data)])
        return item
    if data.get("success") is False:
        item.update(status="error", notes=[f"接口返回失败：{data.get('msg')}"])
        return item
    body = data.get("data") or {}
    item["plan"] = body.get("level")
    for limit in body.get("limits") or []:
        total = float(limit.get("usage") or 0)
        used = float(limit.get("currentValue") or 0)
        pct = float(limit.get("percentage") or 0)
        if total:
            pct = max(pct, used / total * 100.0)
        unit = ZHIPU_UNITS.get(limit.get("unit"), f"单位{limit.get('unit')}")
        number = limit.get("number")
        label = f"{number} {unit}" if number else unit
        item["windows"].append({
            "label": label,
            "used_pct": pct,
            "reset": parse_ts(limit.get("nextResetTime")),
            "detail": f"已用 {int(used)} / {int(total)}，剩 {int(limit.get('remaining') or 0)}",
        })
    return item


def collect_minimax(fetch=True):
    item = _blank("minimax", "MiniMax")
    token, source = _minimax_token()
    if not token:
        item.update(status="missing", notes=["未找到 MiniMax key（环境变量或 cc-switch provider）"])
        return item
    item["source"] = source
    if not fetch:
        return item
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    status, data = http_json("https://api.minimaxi.com/v1/coding_plan/remains", headers)
    if status != 200:
        item.update(status="error", notes=[_fmt_err(status, data)])
        return item
    base = data.get("base_resp") or {}
    if base.get("status_code") not in (0, None):
        item.update(status="error", notes=[f"接口返回失败：{base.get('status_msg')}"])
        return item
    remains = data.get("model_remains") or []
    item["plan"] = "coding plan"
    for entry in remains:
        model = entry.get("model_name") or "?"
        interval_left = entry.get("current_interval_remaining_percent")
        weekly_left = entry.get("current_weekly_remaining_percent")
        if interval_left is not None:
            item["windows"].append({
                "label": f"{model} 5 小时",
                "used_pct": max(0.0, 100.0 - float(interval_left)),
                "reset": parse_ts(entry.get("end_time")),
            })
        if weekly_left is not None:
            item["windows"].append({
                "label": f"{model} 7 天",
                "used_pct": max(0.0, 100.0 - float(weekly_left)),
                "reset": parse_ts(entry.get("weekly_end_time")),
            })
        boost = entry.get("weekly_boost_permille")
        if boost:
            item["notes"].append(f"{model} 周额度加成 {boost / 1000:.1f}×")
    if not remains:
        item["notes"].append("接口未返回 model_remains，可能该套餐未开通")
    return item


def _grokbot_usage_paths():
    """grokbot 侧写出的用量文件。第一个非空的生效。"""
    override = os.environ.get("GROKBOT_USAGE_FILE")
    paths = []
    if override:
        paths.append(Path(override))
    paths.append(HOME / ".grokbot" / "usage.json")
    paths.append(HOME / ".grokbot" / "usage" / "current.json")
    return paths


def _pick(mapping, *keys, default=None):
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return default


def _looks_quantitative(value):
    """判断是否适合放进大字号数值位：够短且含数字。

    对方可能把「见 设置 › 用量与账单」这类说明塞进 metric.value，
    那样会撑坏排版，降级成备注更合适。
    """
    text = str(value or "").strip()
    if not text or len(text) > 16:
        return False
    return any(ch.isdigit() for ch in text)


def _parse_grokbot_payload(data):
    """容错解析 grokbot 写出的用量 JSON。

    字段名做了同义词兼容，对方用 used_pct / used_percent / percentage 任一都可以，
    给 remaining 的会自动换算成已用。
    """
    windows = []
    raw_windows = _pick(data, "windows", "quotas", "limits", "buckets", default=[])
    if isinstance(raw_windows, dict):
        raw_windows = list(raw_windows.values())
    for entry in raw_windows or []:
        if not isinstance(entry, dict):
            continue
        label = str(_pick(entry, "label", "name", "model", "model_name", "window", default="额度"))
        used = _pick(entry, "used_pct", "used_percent", "usedPercent",
                     "percent", "percentage", "utilization")
        if used is None:
            remaining = _pick(entry, "remaining_pct", "remaining_percent",
                              "remainingPercent", "remaining")
            if remaining is not None:
                try:
                    used = 100.0 - float(remaining)
                except (TypeError, ValueError):
                    used = None
        try:
            used = None if used is None else float(used)
        except (TypeError, ValueError):
            used = None
        reset = parse_ts(_pick(entry, "reset_at", "reset", "resets_at",
                               "reset_time", "resetTime", "end_time"))
        window = {"label": label, "used_pct": used, "reset": reset}
        detail = _pick(entry, "detail", "note", "description", "text")
        if detail:
            window["detail"] = str(detail)
        windows.append(window)

    notes = []
    raw_notes = data.get("notes") or data.get("note")
    if isinstance(raw_notes, list):
        notes = [str(n) for n in raw_notes]
    elif raw_notes:
        notes = [str(raw_notes)]

    metric = None
    raw_metric = data.get("metric") or data.get("balance")
    if isinstance(raw_metric, dict):
        label = str(_pick(raw_metric, "label", "name", default="剩余"))
        value = str(_pick(raw_metric, "value", "amount", "total", "balance", default=""))
        sub = str(_pick(raw_metric, "sub", "detail", "note", default=""))
        if _looks_quantitative(value):
            metric = {"label": label, "value": value, "sub": sub}
        else:
            notes.append(f"{label}：{value}" + (f"（{sub}）" if sub else ""))
    elif raw_metric is not None:
        candidate = str(raw_metric)
        if _looks_quantitative(candidate):
            metric = {"label": "剩余", "value": candidate, "sub": ""}
        else:
            notes.append(f"剩余：{candidate}")

    return {
        "windows": windows,
        "metric": metric,
        "notes": notes,
        "plan": _pick(data, "plan", "tier", "plan_type"),
        "account": _pick(data, "account", "email", "user"),
        "updated": parse_ts(_pick(data, "updated_at", "generated_at",
                                  "ts", "timestamp", "time")),
    }


def collect_grokbot(fetch=True):
    """grokbot 没有公开接口，用量靠它自己写进 ~/.grokbot/usage.json。

    实际流程：对 grokbot 说「更新额度」→ 它查完写文件 → 这里读取。
    """
    item = _blank("grokbot", "Grok Bot")
    root = HOME / ".grokbot"
    if not root.exists():
        item.update(status="missing", notes=["未找到 ~/.grokbot"])
        return item

    payload = None
    source = None
    for path in _grokbot_usage_paths():
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text())
            source = str(path)
            break
        except (OSError, ValueError) as exc:
            item["notes"].append(f"{path.name} 解析失败：{exc}")
    if isinstance(payload, dict):
        parsed = _parse_grokbot_payload(payload)
        item["source"] = source
        item["windows"] = parsed["windows"]
        item["metric"] = parsed["metric"]
        item["notes"].extend(parsed["notes"])
        if parsed["plan"]:
            item["plan"] = parsed["plan"]
        if parsed["account"]:
            item["account"] = parsed["account"]
        updated = parsed["updated"]
        if updated:
            age = (datetime.now(timezone.utc) - updated).total_seconds()
            if age > 6 * 3600:
                item["notes"].append(f"数据写入于 {int(age // 3600)} 小时前，可能已过期")
            else:
                item["notes"].append(f"数据写入于 {fmt_ago(age)}")
        if not item["windows"] and not item["metric"]:
            item["notes"].append("usage.json 里没有可识别的额度字段，检查 schema")
        return item

    stamps = []
    for name in ("local-exec-daemon.log", "settings.json", "local-exec-daemon.json"):
        try:
            stamps.append((root / name).stat().st_mtime)
        except OSError:
            continue
    if stamps:
        item["windows"].append({
            "label": "最后活动",
            "used_pct": None,
            "reset": None,
            "detail": datetime.fromtimestamp(max(stamps)).strftime("%Y-%m-%d %H:%M"),
        })
    item["status"] = "local_only"
    if item["notes"]:
        item["notes"].append("usage.json 存在但解析失败，检查是否为合法 JSON")
    else:
        item["notes"].append(
            "还没有 ~/.grokbot/usage.json —— 对 grokbot 说「更新额度」，它写入后刷新即可看到")
    item["notes"].append("凭据为 Electron 密封存储（sand-secrets），没有公开额度接口")
    return item


def _deepseek_token():
    for env_name in ("DEEPSEEK_API_KEY", "DEEPSEEK_KEY"):
        token = os.environ.get(env_name)
        if token:
            return token.strip(), f"env:{env_name}"
    cfg = HOME / ".deepseek" / "config.toml"
    if cfg.exists():
        try:
            for line in cfg.read_text().splitlines():
                if line.strip().startswith("api_key"):
                    token = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if token:
                        return token, str(cfg)
        except OSError:
            pass
    key_file = HOME / ".herdr-keys" / "deepseek.txt"
    if key_file.exists():
        token = key_file.read_text().strip()
        if token:
            return token, str(key_file)
    return _cc_find_token("deepseek")


def collect_deepseek(fetch=True):
    """按 token 计费的 API 账户，只有余额，没有额度窗口。"""
    item = _blank("deepseek", "DeepSeek")
    token, source = _deepseek_token()
    if not token:
        item.update(status="missing", notes=["未找到 DeepSeek key（~/.deepseek/config.toml 或环境变量）"])
        return item
    item["source"] = source
    if not fetch:
        return item
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    status, data = http_json("https://api.deepseek.com/user/balance", headers)
    if status != 200:
        item.update(status="error", notes=[_fmt_err(status, data)])
        return item
    infos = data.get("balance_infos") or []
    if not infos:
        item.update(status="error", notes=["接口未返回 balance_infos"])
        return item
    usable = data.get("is_available")
    if usable is False:
        item["notes"].append("账户当前不可用（is_available=false）")
    symbols = {"CNY": "¥", "USD": "$"}
    main = infos[0]
    symbol = symbols.get(main.get("currency"), main.get("currency", "") + " ")
    item["metric"] = {
        "label": "剩余余额",
        "value": f"{symbol}{main.get('total_balance')}",
        # 只留余额本身，充值/赠送拆分不再展示
        "sub": " / ".join(
            f"{info.get('currency')} {symbols.get(info.get('currency'), '')}{info.get('total_balance')}"
            for info in infos) if len(infos) > 1 else "",
    }
    return item


GEMINI_TIER_LABELS = {
    "free-tier": "免费层",
    "standard-tier": "标准层",
    "legacy-tier": "旧版层",
}


def _gemini_oauth():
    """gemini-cli 登录后留下的 OAuth 凭据。"""
    for path in (HOME / ".gemini" / "oauth_creds.json",
                 HOME / ".config" / "gemini" / "oauth_creds.json"):
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        token = data.get("access_token")
        if token:
            return token, str(path), data
    return None, None, None


def _gemini_api_key():
    for env_name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_API_KEY"):
        token = os.environ.get(env_name)
        if token:
            return token.strip(), f"env:{env_name}"
    return None, None


def collect_gemini(fetch=True):
    """Gemini 订阅额度：走 gemini-cli 的 cloudcode-pa 接口，需要 OAuth 登录。

    真实契约（取自 gemini-cli 0.66.0 源码）：
      1) POST v1internal:loadCodeAssist
         body: {metadata: {ideName, pluginType: "GEMINI", ideVersion, platform, updateChannel},
                mode: "HEALTH_CHECK"}
         → currentTier.id（free-tier / standard-tier / legacy-tier）+ cloudaicompanionProject
      2) POST v1internal:retrieveUserQuota
         body: {project: <上一步的 project>}
         → buckets[{modelId, remainingAmount, remainingFraction, resetTime}]
       免费层账号调用第 2 步会返回 403（无该产品的有效许可 #3501）。
    """
    item = _blank("gemini", "Gemini")
    oauth, source, raw = _gemini_oauth()
    if not oauth:
        api_key, key_source = _gemini_api_key()
        if api_key:
            item.update(status="local_only", source=key_source, notes=[
                "当前是 API key 模式，Gemini API 没有订阅额度接口（只有速率限制）",
                "若要查订阅额度，装 gemini-cli 并用 Google 账号登录一次",
            ])
            return item
        item.update(status="missing", notes=[
            "未找到 Gemini 凭据：~/.gemini/oauth_creds.json 不存在，也没有 GEMINI_API_KEY",
            "装 gemini-cli 并登录后，这里会自动显示订阅额度",
        ])
        return item
    item["source"] = source
    accounts = HOME / ".gemini" / "google_accounts.json"
    if accounts.exists():
        try:
            item["account"] = json.loads(accounts.read_text()).get("active")
        except (OSError, ValueError):
            pass
    expiry = raw.get("expiry_date")
    if isinstance(expiry, (int, float)) and expiry / 1000.0 < time.time():
        item["notes"].append("OAuth token 已过期，运行一次 gemini 会自动刷新")
    if not fetch:
        return item

    version = "0.66.0"
    headers = {
        "Authorization": f"Bearer {oauth}",
        "User-Agent": f"GeminiCLI/{version}",
        "Accept": "application/json",
    }
    metadata = {
        "ideName": "IDE_UNSPECIFIED",
        "pluginType": "GEMINI",
        "ideVersion": version,
        "platform": "PLATFORM_UNSPECIFIED",
        "updateChannel": "stable",
    }
    status, tier = http_json(
        "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist",
        headers, method="POST", body={"metadata": metadata, "mode": "HEALTH_CHECK"})
    if status != 200:
        item.update(status="error", notes=[f"loadCodeAssist {_fmt_err(status, tier)}"])
        return item

    current = tier.get("currentTier") or {}
    tier_id = current.get("id")
    if tier_id:
        item["plan"] = GEMINI_TIER_LABELS.get(tier_id, tier_id)
    tier_name = current.get("name")
    if tier_name:
        item["notes"].append(tier_name)
    if tier_id == "free-tier":
        item["notes"].append(
            "免费层个体用户已被 Google 停用于 CLI（UNSUPPORTED_CLIENT），"
            "官方要求迁移 antigravity.google；换有 Code Assist 许可的账号才会出配额")
    project = tier.get("cloudaicompanionProject")

    status, quota = http_json(
        "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota",
        headers, method="POST", body={"project": project} if project else {})
    if status != 200:
        reason = ""
        if isinstance(quota, dict):
            reason = " ".join(str(quota.get("_error") or "").split())
        if status == 403:
            item["notes"].append("配额桶接口 HTTP 403（#3501 无有效许可），只能看到套餐层级")
        elif status != 0:
            item["notes"].append(f"配额接口不可用（HTTP {status}）" + (f"：{reason[:110]}" if reason else ""))
        else:
            item["notes"].append("配额接口不可用：网络不可达")
        return item

    buckets = quota.get("buckets") or []
    for bucket in buckets:
        model = bucket.get("modelId")
        fraction = bucket.get("remainingFraction")
        amount = bucket.get("remainingAmount")
        if not model:
            continue
        window = {
            "label": model,
            "used_pct": None if fraction is None else max(0.0, (1.0 - float(fraction)) * 100.0),
            "reset": parse_ts(bucket.get("resetTime")),
        }
        if amount is not None:
            try:
                remaining = int(amount)
                total = round(remaining / float(fraction)) if fraction else remaining
                window["detail"] = f"剩余 {remaining} / {total}"
            except (TypeError, ValueError, ZeroDivisionError):
                window["detail"] = f"剩余 {amount}"
        item["windows"].append(window)
    if not buckets:
        item["notes"].append("接口未返回配额桶，可能该套餐没有日配额")
    return item


PROVIDERS = (collect_codex, collect_claude, collect_zhipu, collect_minimax,
             collect_gemini, collect_deepseek, collect_grokbot)

# 面板/JSON 里的显示顺序：采集顺序与展示顺序解耦
DISPLAY_ORDER = ("grokbot", "codex", "claude", "deepseek", "zhipu", "minimax", "gemini")


# ── 本机会话统计 ────────────────────────────────────────────────────────
def _cutoff_date(days):
    return (datetime.now() - timedelta(days=days)).date()


LOCAL_CACHE_PATH = HOME / ".local" / "share" / "usage-monitor" / "local-cache.json"
_LOCAL_CACHE_LOCK = threading.Lock()
_ZERO_DAY = {"msgs": 0, "in_total": 0, "cache_read": 0, "out": 0}


def _load_local_cache():
    try:
        return json.loads(LOCAL_CACHE_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _save_local_section(name, section):
    """只替换自己那一段，避免两个线程互相覆盖。"""
    with _LOCAL_CACHE_LOCK:
        data = _load_local_cache()
        data[name] = section
        try:
            LOCAL_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            LOCAL_CACHE_PATH.write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            pass


def _new_day():
    return dict(_ZERO_DAY)


def _merge_days(agg, per_file, cutoff_str):
    for day, data in per_file.items():
        if day < cutoff_str:
            continue
        bucket = agg.setdefault(day, _new_day())
        for key in _ZERO_DAY:
            bucket[key] += int(data.get(key) or 0)


def _scan_claude_file(path):
    """解析单个 Claude 会话文件，返回 {日期: 计数}。"""
    per_day = {}
    try:
        handle = open(path, errors="ignore")
    except OSError:
        return per_day
    with handle:
        for line in handle:
            if '"usage"' not in line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            message = record.get("message") or {}
            usage = message.get("usage") if isinstance(message, dict) else None
            if not isinstance(usage, dict):
                continue
            stamp = parse_ts(record.get("timestamp"))
            if stamp is None:
                continue
            day = str(stamp.astimezone().date())
            cache_read = int(usage.get("cache_read_input_tokens") or 0)
            cache_write = int(usage.get("cache_creation_input_tokens") or 0)
            bucket = per_day.setdefault(day, _new_day())
            bucket["msgs"] += 1
            bucket["in_total"] += int(usage.get("input_tokens") or 0) + cache_read + cache_write
            bucket["cache_read"] += cache_read
            bucket["out"] += int(usage.get("output_tokens") or 0)
    return per_day


def local_claude(days):
    root = HOME / ".claude" / "projects"
    if not root.is_dir():
        return None
    cutoff_str = str(_cutoff_date(days))
    section = _load_local_cache().get("claude", {})
    agg = {}
    files = 0
    seen = set()
    for path in root.rglob("*.jsonl"):
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_mtime < (time.time() - days * 86400 - 86400):
            continue
        files += 1
        key = str(path)
        seen.add(key)
        entry = section.get(key)
        if entry and entry.get("mtime") == stat.st_mtime and entry.get("size") == stat.st_size:
            per_file = entry.get("days") or {}
        else:
            per_file = _scan_claude_file(path)
            section[key] = {"mtime": stat.st_mtime, "size": stat.st_size, "days": per_file}
        _merge_days(agg, per_file, cutoff_str)
    for key in [k for k in section if k not in seen]:
        section.pop(key, None)
    _save_local_section("claude", section)
    return {"source": "claude", "files": files, "days": agg}


def _scan_codex_file(path):
    """解析单个 Codex rollout，返回 {日期: 计数}。取最后一次累计值。"""
    totals = None
    try:
        handle = open(path, errors="ignore")
    except OSError:
        return {}
    with handle:
        for line in handle:
            if '"total_token_usage"' not in line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            info = (record.get("payload") or {}).get("info") or {}
            usage = info.get("total_token_usage")
            if isinstance(usage, dict):
                totals = usage
    if not totals:
        return {}
    day_key = _codex_day_key(path)
    if not day_key:
        return {}
    bucket = _new_day()
    bucket["msgs"] = 1
    # OpenAI/Codex 的 input_tokens 已包含 cached_input_tokens
    bucket["in_total"] = int(totals.get("input_tokens") or 0)
    bucket["cache_read"] = int(totals.get("cached_input_tokens") or 0)
    bucket["out"] = int(totals.get("output_tokens") or 0)
    return {day_key: bucket}


def _codex_day_key(path):
    parts = path.parts
    try:
        idx = parts.index("sessions")
        return f"{int(parts[idx + 1]):04d}-{int(parts[idx + 2]):02d}-{int(parts[idx + 3]):02d}"
    except (ValueError, IndexError):
        return None


def local_codex(days):
    root = HOME / ".codex" / "sessions"
    if not root.is_dir():
        return None
    cutoff_str = str(_cutoff_date(days))
    section = _load_local_cache().get("codex", {})
    agg = {}
    files = 0
    seen = set()
    for path in root.rglob("*.jsonl"):
        day_key = _codex_day_key(path)   # 先用路径日期粗筛，省掉无谓 stat
        if not day_key or day_key < cutoff_str:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        files += 1
        key = str(path)
        seen.add(key)
        entry = section.get(key)
        if entry and entry.get("mtime") == stat.st_mtime and entry.get("size") == stat.st_size:
            per_file = entry.get("days") or {}
        else:
            per_file = _scan_codex_file(path)
            section[key] = {"mtime": stat.st_mtime, "size": stat.st_size, "days": per_file}
        _merge_days(agg, per_file, cutoff_str)
    for key in [k for k in section if k not in seen]:
        section.pop(key, None)
    _save_local_section("codex", section)
    return {"source": "codex", "files": files, "days": agg}


# ── 用量汇总 ────────────────────────────────────────────────────────────
def summarize_local(local, days):
    """把 claude / codex 的按日记录汇总成 今日 / 近 N 天 / 本月 三档。

    合计 = 输入 + 缓存读 + 输出，口径写在渲染层里，别让人猜。
    """
    blocks = [b for b in local if b and b.get("days")]
    if not blocks:
        return None
    today = datetime.now().date()
    month_prefix = f"{today.year:04d}-{today.month:02d}"

    def agg(predicate):
        totals = {"in_total": 0, "cache_read": 0, "out": 0, "msgs": 0}
        per_source = {}
        for block in blocks:
            sub = {"in_total": 0, "cache_read": 0, "out": 0, "msgs": 0}
            for day, data in block["days"].items():
                if not predicate(day):
                    continue
                for key in sub:
                    sub[key] += int(data.get(key) or 0)
            if any(sub.values()):
                sub["burned"] = sub["in_total"] + sub["out"]
                per_source[block["source"]] = sub
            for key in totals:
                totals[key] += sub[key]
        totals["burned"] = totals["in_total"] + totals["out"]
        return totals, per_source

    today_agg, _ = agg(lambda d: d == str(today))
    range_agg, _ = agg(lambda d: True)
    month_agg, month_per = agg(lambda d: d.startswith(month_prefix))

    rows = [
        ("今日", today_agg),
        (f"近 {days} 天", range_agg),
        (f"本月 {today.month:02d}-01 起", month_agg),
    ]

    parts = []
    for source, sub in sorted(month_per.items(), key=lambda kv: -kv[1]["burned"]):
        unit = "条" if source == "claude" else "会话"
        parts.append(f"{source} {human(sub['burned'])}（{sub['msgs']} {unit}）")
    return {"rows": [{"label": label, **data} for label, data in rows],
            "by_source": month_per,
            "by_source_text": " · ".join(parts)}


# ── 终端渲染 ────────────────────────────────────────────────────────────
def render_text(items, local, days):
    lines = []
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    header = color("用量监视器", BOLD) + color(f"  {stamp}", DIM)
    if _cache_hits:
        header += color(f"   部分数据取自缓存（最长 {max(_cache_hits)}s 前）", DIM)
    lines.append(header)
    lines.append("")
    for item in items:
        head = color(item["name"], BOLD)
        if item.get("plan"):
            head += color(f"  {item['plan']}", CYAN)
        if item.get("account"):
            head += color(f"  {item['account']}", DIM)
        if item["status"] not in ("ok", "local_only"):
            head += color(f"  [{item['status']}]", RED)
        lines.append(head)
        metric = item.get("metric")
        if metric:
            line = f"  {pad(metric['label'], 17)}{color(metric['value'], BOLD)}"
            if metric.get("sub"):
                line += color('   ' + metric['sub'], DIM)
            lines.append(line)
        for window in item["windows"]:
            label = pad(window["label"], 17)
            pct = window.get("used_pct")
            if pct is None:
                lines.append(f"  {label}{color(window.get('detail') or '—', DIM)}")
                continue
            bar = render_bar(pct)
            body = f"  {label}{color(bar, pct_color(pct))}  {pct:5.1f}%"
            reset = fmt_remaining(window.get("reset")) if window.get("reset") else "—"
            tail = f"   重置 {reset}" if reset != "已重置" else "   已重置"
            body += color(tail, DIM)
            if window.get("detail"):
                body += color(f"   {window['detail']}", DIM)
            if pct >= 100:
                body += color("   ⚠ 已耗尽", RED)
            lines.append(body)
        for note in item["notes"]:
            lines.append(color(f"  · {note}", DIM))
        lines.append("")

    summary = summarize_local(local, days)
    if summary:
        lines.append(color("Token 消耗", BOLD) + color("  claude + codex 本机会话记录", DIM))
        head = (f"  {pad('区间', 16)}{pad('烧掉 token', 13)}{pad('输入(含缓存)', 14)}"
                f"{pad('其中缓存读', 13)}输出")
        lines.append(color(head, DIM))
        for row in summary["rows"]:
            lines.append(
                f"  {pad(row['label'], 16)}{color(pad(human(row['burned']), 13), BOLD)}"
                f"{pad(human(row['in_total']), 14)}"
                f"{pad(human(row['cache_read']), 13)}{human(row['out'])}")
        if summary["by_source_text"]:
            lines.append(color(f"  本月分布  {summary['by_source_text']}", DIM))
        lines.append(color("  烧掉 = 输入 + 输出；缓存读已含在输入内，不重复计", DIM))
        lines.append("")

    if local:
        rows = []
        for block in local:
            if not block:
                continue
            for day, data in block["days"].items():
                rows.append((day, block["source"], data))
        if rows:
            lines.append(color(f"本机会话（近 {days} 天）", BOLD))
            lines.append(color(f"  {pad('日期', 12)}{pad('来源', 9)}{pad('单位', 9)}"
                               f"{pad('烧掉', 12)}{pad('缓存读', 12)}输出", DIM))
            for day, source, data in sorted(rows, key=lambda r: (r[0], r[1]), reverse=True):
                unit = f"{data['msgs']} " + ("条" if source == "claude" else "会话")
                burned = int(data.get("in_total") or 0) + int(data.get("out") or 0)
                lines.append(
                    f"  {pad(day, 12)}{pad(source, 9)}{pad(unit, 9)}"
                    f"{pad(human(burned), 12)}{pad(human(data.get('cache_read')), 12)}"
                    f"{human(data.get('out'))}"
                )
            lines.append("")
    return "\n".join(lines)


# ── HTML 渲染 ───────────────────────────────────────────────────────────
CSS = """
:root{--bg:#0f1115;--card:#171a21;--line:#242938;--fg:#e6e9ef;--dim:#8b93a7;
--ok:#3fb950;--warn:#d29922;--bad:#f85149;--accent:#58a6ff}
*{box-sizing:border-box}
body{margin:0;padding:32px;background:var(--bg);color:var(--fg);
font:14px/1.55 -apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang SC",sans-serif}
h1{font-size:18px;font-weight:600;margin:0 0 4px}
.sub{color:var(--dim);font-size:12px;margin-bottom:24px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px}
.head{display:flex;align-items:baseline;gap:8px;margin-bottom:14px}
.name{font-size:15px;font-weight:600}
.tag{font-size:11px;color:var(--accent);border:1px solid var(--line);
border-radius:4px;padding:1px 6px}
.acct{font-size:11px;color:var(--dim);margin-left:auto}
.row{display:grid;grid-template-columns:86px 1fr 58px 92px;gap:10px;align-items:center;
margin-bottom:10px;font-size:12px}
.lbl{color:var(--dim)}
.track{height:8px;background:#22262f;border-radius:4px;overflow:hidden}
.fill{height:100%;border-radius:4px}
.pct{text-align:right;font-variant-numeric:tabular-nums}
.reset{color:var(--dim);font-size:11px}
.detail{grid-column:1/-1;color:var(--dim);font-size:11px;margin-top:-6px}
.notes{margin-top:12px;padding-top:10px;border-top:1px solid var(--line);
color:var(--dim);font-size:11px}
.notes div{margin-bottom:3px}
table{width:100%;border-collapse:collapse;margin-top:10px;font-size:12px}
th,td{text-align:right;padding:6px 8px;border-bottom:1px solid var(--line);
font-variant-numeric:tabular-nums}
th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){text-align:left}
th{color:var(--dim);font-weight:500}
.section{margin-top:28px}
.wrap{max-width:1180px;margin:0 auto}
.err{color:var(--bad)}
body.wallpaper{padding:44px 52px}
body.wallpaper .wrap{max-width:none}
body.wallpaper h1{font-size:22px}
body.wallpaper .sub{font-size:14px;margin-bottom:30px}
body.wallpaper .grid{grid-template-columns:repeat(auto-fill,minmax(430px,1fr));gap:20px}
body.wallpaper .card{padding:22px 24px;border-radius:12px}
body.wallpaper .name{font-size:17px}
body.wallpaper .row{grid-template-columns:96px 1fr 62px 104px;font-size:13px}
body.wallpaper .track{height:9px}
body.wallpaper .metric .mval{font-size:34px}
body.wallpaper .section{margin-top:34px}
body.wallpaper table{font-size:14px}
body.wallpaper th,body.wallpaper td{padding:9px 10px}
.metric{display:flex;align-items:baseline;gap:10px;margin:0 0 12px}
.mval{font-size:26px;font-weight:600;font-variant-numeric:tabular-nums;letter-spacing:-.5px}
.msub{font-size:11px;color:var(--dim)}
"""


def render_html(items, local, days):
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    cards = []
    for item in items:
        rows = []
        for window in item["windows"]:
            pct = window.get("used_pct")
            if pct is None:
                rows.append(
                    f'<div class="row"><span class="lbl">{window["label"]}</span>'
                    f'<span class="detail">{window.get("detail") or "—"}</span></div>')
                continue
            if pct >= 85:
                hue = "var(--bad)"
            elif pct >= 60:
                hue = "var(--warn)"
            else:
                hue = "var(--ok)"
            reset = fmt_remaining(window.get("reset")) if window.get("reset") else "—"
            detail = f'<div class="detail">{window["detail"]}</div>' if window.get("detail") else ""
            rows.append(
                f'<div class="row"><span class="lbl">{window["label"]}</span>'
                f'<span class="track"><span class="fill" style="width:{min(pct, 100):.1f}%;background:{hue}"></span></span>'
                f'<span class="pct">{pct:.1f}%</span>'
                f'<span class="reset">{reset}</span></div>{detail}')
        metric = item.get("metric")
        metric_html = ""
        if metric:
            sub = metric.get("sub") or ""
            sub_html = f'<span class="msub">{sub}</span>' if sub else ""
            metric_html = (f'<div class="metric"><span class="mval">{metric["value"]}</span>'
                           f'{sub_html}</div>')
        notes = "".join(f"<div>· {n}</div>" for n in item["notes"])
        tag = f'<span class="tag">{item["plan"]}</span>' if item.get("plan") else ""
        acct = f'<span class="acct">{item["account"]}</span>' if item.get("account") else ""
        bad = ' class="err"' if item["status"] not in ("ok", "local_only") else ""
        cards.append(
            f'<div class="card"><div class="head"><span class="name"{bad}>{item["name"]}</span>'
            f'{tag}{acct}</div>{metric_html}{"".join(rows)}'
            f'{f'<div class="notes">{notes}</div>' if notes else ""}</div>')

    summary_html = ""
    summary = summarize_local(local, days)
    if summary:
        body = "".join(
            f"<tr><td>{row['label']}</td>"
            f"<td><strong>{human(row['burned'])}</strong></td>"
            f"<td>{human(row['in_total'])}</td>"
            f"<td>{human(row['cache_read'])}</td><td>{human(row['out'])}</td></tr>"
            for row in summary["rows"])
        source_line = (f'<div class="sub">{summary["by_source_text"]}</div>'
                       if summary["by_source_text"] else "")
        summary_html = (
            f'<div class="section"><h1>Token 消耗</h1>'
            f'<div class="sub">claude + codex 本机会话记录 · 烧掉 = 输入 + 输出，'
            f'缓存读已含在输入内不重复计</div>'
            f"<table><thead><tr><th>区间</th><th>烧掉 token</th><th>输入(含缓存)</th>"
            f"<th>其中缓存读</th><th>输出</th></tr></thead><tbody>{body}</tbody></table>"
            f"{source_line}</div>")

    table = ""
    rows = []
    for block in local:
        if not block:
            continue
        for day, data in block["days"].items():
            unit = f"{data['msgs']} " + ("条" if block["source"] == "claude" else "会话")
            rows.append((day, block["source"], unit, data))
    if rows:
        body = "".join(
            f"<tr><td>{day}</td><td>{source}</td><td>{unit}</td>"
            f"<td><strong>{human(int(data.get('in_total') or 0) + int(data.get('out') or 0))}</strong></td>"
            f"<td>{human(data.get('cache_read'))}</td><td>{human(data.get('out'))}</td></tr>"
            for day, source, unit, data in sorted(rows, key=lambda r: (r[0], r[1]), reverse=True))
        table = (
            f'<div class="section"><h1>本机会话（近 {days} 天）</h1>'
            f"<div class=\"sub\">来自 Claude 与 Codex 的本地记录文件，按本地日期归集</div>"
            f"<table><thead><tr><th>日期</th><th>来源</th><th>量</th>"
            f"<th>烧掉</th><th>缓存读</th><th>输出</th></tr></thead><tbody>{body}</tbody></table></div>")

    cache_note = f" · 部分数据取自缓存（最长 {max(_cache_hits)}s 前）" if _cache_hits else ""
    if _WALLPAPER:
        table = ""
    body_class = ' class="wallpaper"' if _WALLPAPER else ""
    return f"""<!doctype html>
<html lang="zh-Hans"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>用量监视器</title><style>{CSS}</style></head>
<body{body_class}><div class="wrap">
<h1>用量监视器</h1>
<div class="sub">生成于 {stamp} · coding plan 额度 + 账户余额 + 本机会话统计{cache_note}</div>
<div class="grid">{''.join(cards)}</div>
{summary_html}
{table}
</div></body></html>"""


# ── 入口 ────────────────────────────────────────────────────────────────
def _safe_collect(collector, local_only):
    """单个 provider 崩了不影响其他。"""
    try:
        return collector(fetch=not local_only)
    except Exception as exc:
        item = _blank(collector.__name__.replace("collect_", ""), collector.__name__)
        item.update(status="error", notes=[f"采集异常：{exc}"])
        return item


def gather(local_only, days):
    _cache_hits.clear()
    # 六家互不依赖，并发采集：串行 6s+ → 并发约 1-2s
    with ThreadPoolExecutor(max_workers=len(PROVIDERS)) as pool:
        items = list(pool.map(lambda c: _safe_collect(c, local_only), PROVIDERS))
    rank = {pid: index for index, pid in enumerate(DISPLAY_ORDER)}
    items.sort(key=lambda item: rank.get(item["id"], len(DISPLAY_ORDER)))
    if local_only:
        for item in items:
            if item["status"] == "ok" and not item["windows"]:
                item["notes"].append("已跳过网络请求（--local-only）")
    local = []
    if days > 0:
        today = datetime.now().date()
        month_start = today.replace(day=1)
        scan_days = max(days, (today - month_start).days + 1)
        def _safe_local(func):
            try:
                return func(scan_days)
            except Exception:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            local = list(pool.map(_safe_local, (local_claude, local_codex)))
    return items, local


def fingerprint(items, local, days):
    """数据指纹：忽略 generated_at，其余内容变了才算变。用于跳过重复渲染。"""
    payload = json.loads(to_json(items, local, days))
    payload.pop("generated_at", None)
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def to_json(items, local, days):
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "providers": [],
        "local": {},
    }
    for item in items:
        payload["providers"].append({
            "id": item["id"],
            "name": item["name"],
            "status": item["status"],
            "plan": item.get("plan"),
            "account": item.get("account"),
            "source": item.get("source"),
            "windows": [{
                "label": w["label"],
                "used_pct": w.get("used_pct"),
                "detail": w.get("detail"),
                "reset_at": w["reset"].isoformat() if w.get("reset") else None,
            } for w in item["windows"]],
            "metric": item.get("metric"),
            "notes": item["notes"],
        })
    for block in local:
        if block:
            payload["local"][block["source"]] = block["days"]
    summary = summarize_local(local, days)
    if summary:
        payload["summary"] = {
            "window_days": days,
            "rows": summary["rows"],
            "by_source": summary["by_source"],
        }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def main():
    parser = argparse.ArgumentParser(
        prog="usage-monitor",
        description="Codex / Claude / 智谱 GLM / MiniMax / Grok Bot 额度监视器")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--html", nargs="?", const="", metavar="PATH",
                        help="生成自包含 HTML（默认 ~/.local/share/usage-monitor/report.html）")
    parser.add_argument("--open", action="store_true", help="生成后打开 HTML")
    parser.add_argument("--watch", type=int, metavar="SEC", help="每 SEC 秒刷新")
    parser.add_argument("--local-only", action="store_true", help="不打网络，只看本机会话统计")
    parser.add_argument("--days", type=int, default=7, help="本地统计回看天数，0 表示不统计")
    parser.add_argument("--wallpaper", action="store_true",
                        help="壁纸排版：铺满视口、放大字号、省略逐日明细（配合 --html 使用）")
    parser.add_argument("--units", choices=("cn", "si"), default="cn",
                        help="token 单位：cn 用万/亿（默认），si 用 K/M/G")
    parser.add_argument("--ttl", type=int, default=90,
                        help="接口结果缓存秒数，0 表示每次都重新请求（默认 90）")
    args = parser.parse_args()

    global _cache_ttl, _UNITS, _WALLPAPER
    _cache_ttl = max(0, args.ttl)
    _UNITS = args.units
    _WALLPAPER = args.wallpaper

    while True:
        items, local = gather(args.local_only, args.days)

        if args.json:
            print(to_json(items, local, args.days))
        elif args.html is not None:
            path = Path(args.html) if args.html else HOME / ".local" / "share" / "usage-monitor" / "report.html"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(render_html(items, local, args.days), encoding="utf-8")
            fp = fingerprint(items, local, args.days)
            path.with_name(path.name + ".fingerprint").write_text(fp, encoding="utf-8")
            print(f"已生成 {path}  fp={fp}")
            if args.open:
                subprocess.run(["open", str(path)], check=False)
        else:
            if args.watch:
                sys.stdout.write("\033[H\033[2J")
            print(render_text(items, local, args.days))

        if not args.watch:
            break
        try:
            time.sleep(max(5, args.watch))
        except KeyboardInterrupt:
            break


if __name__ == "__main__":
    main()
