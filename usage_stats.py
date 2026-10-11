"""用量统计：给进度卡片右下角那行小字供数（只读本地记录，不联网）。

设置里可切的几种模式：

* ``today``   —— **今日新增**：今天（按本地日期）各 agent 新增的 token 消耗，多 agent 求和；
* ``session`` —— **本会话累积**：当前这个会话从头到现在的 token 消耗；
* ``off``     —— 不显示。

数据来源（Codex）：``~/.codex/sessions/**/*.jsonl`` 里每条 ``token_count`` 事件带的
``total_token_usage``。注意它是**会话级累计**（每个会话各自从 0 开始涨），所以
"今天新增"不能直接把各会话的数值相加 —— 正确的是：对每个会话取
"今天最后一条的值 − 今天第一条的值"（跨天会话也就算对了），再对所有会话求和。

多 agent：这里按 agent 分开统计（``today_tokens("codex")``），今天新增的合计是各 agent 之和。
现在只有 Codex 有 token 读者；别的新接入 agent（Claude Code / Gemini / OpenClaw / Hermes）
暂时只出状态 —— 没有读者的 agent，卡片右下角那行小字直接不显示，宁可不写也不写个假的 0。
将来给某个 agent 补上读者函数（``_AGENT_TODAY_READERS`` / ``_AGENT_SESSION_READERS``），
"今日新增"和"本会话累积"就会自动把它算进来。
"""

from __future__ import annotations

import datetime
import glob
import json
import os
import re
import sqlite3
import threading
import time

CODEX_HOME = os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
SESSIONS_DIR = os.path.join(CODEX_HOME, "sessions")

# 只认 total_token_usage 里的 total_tokens（同一行里 last_token_usage / turn_token_usage 也有
# 同名字段，所以正则要卡住前缀，不能直接搜 "total_tokens"）
_CUMULATIVE = re.compile(rb'"total_token_usage":\{[^}]*?"total_tokens":(\d+)')
_STAMP = re.compile(rb'"timestamp":"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})')

# 每个文件算出来的"今天增量"缓存：path -> (mtime, 本地日期, 增量)
_delta_cache: dict[str, tuple[float, str, int]] = {}
_lock = threading.Lock()

# 同一份统计最多这么多久重算一次：state_loop 每秒会来问一次，没必要每次都扫会话文件
REFRESH_SECONDS = 20.0
_today_cache: dict[str, tuple[float, int]] = {}
_session_cache: dict[str, tuple[float, int]] = {}


# ------------------------------------------------------------------ helpers


def _local_day(parts) -> str:
    """把会话记录里的 UTC 时间戳折算成本地日期（YYYY-MM-DD）。"""
    year, month, day, hour, minute, second = (int(p) for p in parts)
    utc = datetime.datetime(year, month, day, hour, minute, second, tzinfo=datetime.timezone.utc)
    return utc.astimezone().strftime("%Y-%m-%d")


def today_key() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d")


def format_tokens(value: float) -> str:
    """1234567 -> 1.2M；45678 -> 45.7k；把小数位压到刚好够看。"""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return "0"
    for unit, scale in (("B", 1_000_000_000.0), ("M", 1_000_000.0), ("k", 1_000.0)):
        if abs(n) >= scale:
            short = n / scale
            text = ("%.1f" % short) if short < 100 else ("%.0f" % short)
            return text + unit
    return "%d" % int(n)


def _session_files() -> list[str]:
    try:
        files = glob.glob(os.path.join(SESSIONS_DIR, "**", "*.jsonl"), recursive=True)
    except OSError:
        return []
    out = []
    for path in files:
        try:
            if os.path.isfile(path):
                out.append(path)
        except OSError:
            continue
    return out


# ------------------------------------------------------------------ per-agent readers


def _codex_file_delta_today(path: str, day: str, mtime: float) -> int:
    """一个会话文件在"今天"这一段里的增量 = 今天最后一条 − 今天第一条。"""
    lowest = highest = None
    try:
        with open(path, "rb") as fh:
            for line in fh:
                if b"total_token_usage" not in line:
                    continue
                stamp = _STAMP.search(line)
                if not stamp:
                    continue
                if _local_day(stamp.groups()) != day:
                    continue
                match = _CUMULATIVE.search(line)
                if not match:
                    continue
                total = int(match.group(1))
                if lowest is None or total < lowest:
                    lowest = total
                if highest is None or total > highest:
                    highest = total
    except OSError:
        return 0
    if lowest is None or highest is None:
        return 0
    return max(0, highest - lowest)


def _codex_today_tokens(day: str) -> int:
    total = 0
    with _lock:
        seen = set()
        for path in _session_files():
            seen.add(path)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            cached = _delta_cache.get(path)
            if cached and cached[0] == mtime and cached[1] == day:
                total += cached[2]
                continue
            delta = _codex_file_delta_today(path, day, mtime)
            _delta_cache[path] = (mtime, day, delta)
            total += delta
        for gone in [p for p in _delta_cache if p not in seen]:
            _delta_cache.pop(gone, None)
    return total


def _codex_session_tokens() -> int:
    """当前（最近更新）会话的累计消耗。"""
    files = _session_files()
    if not files:
        return 0
    try:
        newest = max(files, key=os.path.getmtime)
    except (OSError, ValueError):
        return 0
    size = os.path.getsize(newest) if os.path.exists(newest) else 0
    latest = 0
    try:
        with open(newest, "rb") as fh:
            if size > 2_000_000:          # 只读尾巴：当前值就在最后
                fh.seek(size - 2_000_000)
                fh.readline()
            for line in fh:
                if b"total_token_usage" not in line:
                    continue
                match = _CUMULATIVE.search(line)
                if match:
                    latest = int(match.group(1))
    except OSError:
        return latest
    return latest


# --------------------------------------------------- 其它 agent 的 token 读者
# 通用的"从会话 JSONL 里数 token"：各家字段名不同，都在 _entry_token_usage 里翻译。
#   本会话累积 = 最新那个会话文件里、所有助手回复的用量合计
#   今日新增   = 其中时间戳落在"本地今天"的那些回复的合计
# 每个文件按 mtime 缓存，state_loop 每秒来问也不会重新扫盘。

_JSONL_TOKEN_CACHE: dict[str, tuple[float, str, str, int, int]] = {}
_JSONL_TOKEN_LOCK = threading.Lock()

# agent id -> 用哪套字段解析（和 agent_state 的 kind 一致）
_JSONL_TOKEN_KINDS = {"claude": "claude", "gemini": "gemini", "openclaw": "openclaw"}


def _int(value) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _stamp_seconds(value) -> float:
    """ISO 字符串 / 秒 / 毫秒时间戳 → Unix 秒；认不出来返回 0。"""
    if isinstance(value, (int, float)):
        number = float(value)
        return number / 1000.0 if number > 1e11 else number
    text = str(value or "").strip().replace("Z", "+00:00")
    if not text:
        return 0.0
    try:
        return datetime.datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0


def _local_day_from_ts(stamp: float) -> str:
    try:
        return datetime.datetime.fromtimestamp(float(stamp)).strftime("%Y-%m-%d")
    except (OSError, OverflowError, ValueError):
        return ""


def _entry_token_usage(entry: dict, kind: str) -> tuple[int, float]:
    """一条会话记录里的 (token 数, 时间戳)；不是"助手回复"就返回 0。"""
    if kind == "claude":
        if str(entry.get("type") or "").lower() != "assistant":
            return 0, 0.0
        message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
        usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
        value = sum(_int(usage.get(key)) for key in (
            "input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        return value, _stamp_seconds(entry.get("timestamp"))
    if kind == "gemini":
        if str(entry.get("type") or "").lower() != "gemini":
            return 0, 0.0
        tokens = entry.get("tokens") if isinstance(entry.get("tokens"), dict) else {}
        value = _int(tokens.get("total")) or sum(
            _int(tokens.get(key)) for key in ("input", "output", "cached", "thoughts"))
        return value, _stamp_seconds(entry.get("timestamp"))
    if kind == "openclaw":
        message = entry.get("message") if isinstance(entry.get("message"), dict) else entry
        if str(message.get("role") or "").lower() not in ("assistant", "ai", "model"):
            return 0, 0.0
        usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
        value = _int(usage.get("totalTokens")) or _int(usage.get("total")) or sum(
            _int(usage.get(key)) for key in ("input", "output", "input_tokens", "output_tokens", "cacheRead", "cacheWrite"))
        return value, _stamp_seconds(entry.get("timestamp") or message.get("timestamp"))
    return 0, 0.0


def _scan_jsonl_tokens(path: str, kind: str, day: str) -> tuple[int, int]:
    """扫一个会话文件，返回 (今日新增, 本会话合计)。"""
    today = total = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                value, stamp = _entry_token_usage(entry, kind)
                if value <= 0:
                    continue
                total += value
                if day and stamp and _local_day_from_ts(stamp) == day:
                    today += value
    except OSError:
        return 0, 0
    return today, total


def _file_tokens(path: str, kind: str, day: str) -> tuple[int, int]:
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return 0, 0
    hit = _JSONL_TOKEN_CACHE.get(path)
    if hit and hit[0] == mtime and hit[1] == day and hit[2] == kind:
        return hit[3], hit[4]
    today, total = _scan_jsonl_tokens(path, kind, day)
    if len(_JSONL_TOKEN_CACHE) > 400:
        _JSONL_TOKEN_CACHE.clear()
    _JSONL_TOKEN_CACHE[path] = (mtime, day, kind, today, total)
    return today, total


def _agent_session_files(agent: str) -> list[str]:
    """复用 agent_state 的注册表拿会话文件（保持"读哪些文件"只有一处定义）。"""
    try:
        import agent_state

        patterns = agent_state._spec(agent).get("roots") or []
    except Exception:  # noqa: BLE001
        return []
    files: list[str] = []
    for pattern in patterns:
        try:
            files.extend(glob.glob(pattern, recursive=True))
        except OSError:
            continue
    return [item for item in files if os.path.isfile(item)]


def _jsonl_today_tokens(day: str, agent: str) -> int:
    kind = _JSONL_TOKEN_KINDS.get(agent)
    if not kind:
        return 0
    total = 0
    with _JSONL_TOKEN_LOCK:
        for path in _agent_session_files(agent):
            today, _ = _file_tokens(path, kind, day)
            total += today
    return total


def _jsonl_session_tokens(agent: str) -> int:
    kind = _JSONL_TOKEN_KINDS.get(agent)
    files = _agent_session_files(agent)
    if not kind or not files:
        return 0
    try:
        newest = max(files, key=os.path.getmtime)
    except (OSError, ValueError):
        return 0
    with _JSONL_TOKEN_LOCK:
        _, total = _file_tokens(newest, kind, today_key())
    return total



def _sqlite_ro(path: str):
    """只读打开 sqlite（缺 shm 就退 immutable）：不写盘、不起子进程。"""
    target = str(path or "").replace("\\", "/")
    try:
        con = sqlite3.connect("file:%s?mode=ro" % target, uri=True, timeout=1.5)
        con.row_factory = sqlite3.Row
        con.execute("select 1").fetchone()
        return con
    except sqlite3.Error:
        con = sqlite3.connect("file:%s?mode=ro&immutable=1" % target, uri=True, timeout=1.5)
        con.row_factory = sqlite3.Row
        return con


def _close_quiet(con) -> None:
    try:
        if con is not None:
            con.close()
    except sqlite3.Error:
        pass


def _day_start_ms() -> int:
    start = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp() * 1000)


def _agent_db(agent: str) -> str:
    try:
        import agent_state

        return str(agent_state._spec(agent).get("db") or "")
    except Exception:  # noqa: BLE001
        return ""


def _zcode_tokens(mode: str) -> int:
    """ZCode：turn_usage 表逐轮记 token（input / output / cache 读+写）。"""
    db_path = _agent_db("zcode")
    if not db_path or not os.path.exists(db_path):
        return 0
    expr = ("coalesce(input_tokens, 0) + coalesce(output_tokens, 0) + "
            "coalesce(cache_read_input_tokens, 0) + coalesce(cache_creation_input_tokens, 0)")
    con = None
    try:
        con = _sqlite_ro(db_path)
        if mode == "session":
            row = con.execute("select id from session order by time_updated desc limit 1").fetchone()
            if row is None:
                return 0
            row = con.execute(
                "select coalesce(sum(%s), 0) as total from turn_usage where session_id = ?" % expr,
                (str(row[0]),),
            ).fetchone()
        else:
            row = con.execute(
                "select coalesce(sum(%s), 0) as total from turn_usage where started_at >= ?" % expr,
                (_day_start_ms(),),
            ).fetchone()
        return int(row["total"] or 0)
    except sqlite3.Error:
        return 0
    finally:
        _close_quiet(con)


def _workbuddy_tokens(mode: str) -> int:
    """WorkBuddy：session_usage.used 是会话上下文已用 token（没有逐日流水，今日按"今天动过的会话"近似）。"""
    db_path = _agent_db("workbuddy")
    if not db_path or not os.path.exists(db_path):
        return 0
    con = None
    try:
        con = _sqlite_ro(db_path)
        if mode == "session":
            row = con.execute("select used from session_usage order by updated_at desc limit 1").fetchone()
        else:
            row = con.execute(
                "select coalesce(sum(used), 0) as total from session_usage where updated_at >= ?",
                (_day_start_ms(),),
            ).fetchone()
        return int(row["used" if mode == "session" else "total"] or 0) if row is not None else 0
    except sqlite3.Error:
        return 0
    finally:
        _close_quiet(con)


def _dsh_tokens(mode: str) -> int:
    """DeepSeek Harness：projcache 的 tokenUsage.totals 是会话累计；今日按"今天更新过的会话"近似。"""
    try:
        import agent_state

        patterns = agent_state._spec("dsh").get("roots") or []
    except Exception:  # noqa: BLE001
        return 0
    files: list[str] = []
    for pattern in patterns:
        try:
            files.extend(glob.glob(pattern, recursive=True))
        except OSError:
            continue
    files = [item for item in files if os.path.isfile(item)]
    if not files:
        return 0
    if mode == "session":
        try:
            files = [max(files, key=os.path.getmtime)]
        except (OSError, ValueError):
            return 0
    else:
        start = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        picked = []
        for item in files:
            try:
                if os.path.getmtime(item) >= start:
                    picked.append(item)
            except OSError:
                continue
        files = picked
    total = 0
    for path in files:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        record = data.get("record") if isinstance(data, dict) else None
        rows = record.get("rows") if isinstance(record, dict) else None
        usage = rows.get("tokenUsage") if isinstance(rows, dict) else None
        value = usage.get("val") if isinstance(usage, dict) else None
        totals = value.get("totals") if isinstance(value, dict) else None
        if isinstance(totals, dict):
            total += sum(_int(totals.get(key)) for key in (
                "uncachedInputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens"))
    return int(total)


def _opencode_tokens(mode: str) -> int:
    """OpenCode：session_v2 里直接有 input/output/reasoning/cache 读写五列。"""
    db_path = _agent_db("opencode")
    if not db_path or not os.path.exists(db_path):
        return 0
    expr = ("coalesce(tokens_input, 0) + coalesce(tokens_output, 0) + coalesce(tokens_reasoning, 0) + "
            "coalesce(tokens_cache_read, 0) + coalesce(tokens_cache_write, 0)")
    con = None
    try:
        con = _sqlite_ro(db_path)
        if mode == "session":
            row = con.execute(
                "select %s as total from session_v2 order by time_updated desc limit 1" % expr
            ).fetchone()
        else:
            row = con.execute(
                "select coalesce(sum(%s), 0) as total from session_v2 where time_updated >= ?" % expr,
                (_day_start_ms(),),
            ).fetchone()
        return int(row["total"] or 0) if row is not None else 0
    except sqlite3.Error:
        return 0
    finally:
        _close_quiet(con)



def _mimo_tokens(mode: str) -> int:
    """小米 MiMo：mimocode.db 的 part 里 type=step-finish 的那条带 tokens（每轮一份）。"""
    db_path = _agent_db("mimo")
    if not db_path or not os.path.exists(db_path):
        return 0
    con = None
    try:
        con = _sqlite_ro(db_path)
        if mode == "session":
            row = con.execute("select id from session order by time_updated desc limit 1").fetchone()
            session_ids = [str(row[0])] if row is not None else []
        else:
            rows = con.execute(
                "select id from session where time_updated >= ?", (_day_start_ms(),)
            ).fetchall()
            session_ids = [str(item[0]) for item in rows]
        total = 0
        for session_id in session_ids:
            for (raw,) in con.execute("select data from part where session_id = ?", (session_id,)):
                try:
                    data = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if not isinstance(data, dict) or data.get("type") != "step-finish":
                    continue
                tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
                cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
                value = _int(tokens.get("total"))
                if not value:
                    value = (_int(tokens.get("input")) + _int(tokens.get("output"))
                             + _int(tokens.get("reasoning"))
                             + _int(cache.get("read")) + _int(cache.get("write")))
                total += value
        return int(total)
    except sqlite3.Error:
        return 0
    finally:
        _close_quiet(con)



def _kimi_wire_files() -> list[str]:
    try:
        import agent_state

        patterns = agent_state._spec("kimi").get("wire") or []
    except Exception:  # noqa: BLE001
        return []
    files: list[str] = []
    for pattern in patterns:
        try:
            files.extend(glob.glob(pattern, recursive=True))
        except OSError:
            continue
    return [item for item in files if os.path.isfile(item)]


def _kimi_tokens(mode: str) -> int:
    """Kimi Code：wire.jsonl 里 token_counting.turn_recorded 一轮一条（tokens=该轮总数）。"""
    files = _kimi_wire_files()
    if not files:
        return 0
    if mode == "session":
        try:
            files = [max(files, key=os.path.getmtime)]
        except (OSError, ValueError):
            return 0
    day_start = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    total = 0
    for path in files:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(entry, dict):
                        continue
                    if str(entry.get("type") or "") not in ("token_counting.turn_recorded", "token_counting.measured"):
                        continue
                    if mode != "session":
                        stamp = _stamp_seconds(entry.get("time"))
                        if not stamp or stamp < day_start:
                            continue
                    total += _int(entry.get("tokens"))
        except OSError:
            continue
    return total


def _qwen_transcript_files() -> list[str]:
    try:
        import agent_state

        patterns = agent_state._spec("qwen").get("transcripts") or []
    except Exception:  # noqa: BLE001
        return []
    files: list[str] = []
    for pattern in patterns:
        try:
            files.extend(glob.glob(pattern, recursive=True))
        except OSError:
            continue
    return [item for item in files if os.path.isfile(item)]


def _qwen_tokens(mode: str) -> int:
    """Qwen Code：assistant 条目里的 usageMetadata.totalTokenCount（一条一次调用）。"""
    files = _qwen_transcript_files()
    if not files:
        return 0
    if mode == "session":
        try:
            files = [max(files, key=os.path.getmtime)]
        except (OSError, ValueError):
            return 0
    day_start = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    total = 0
    for path in files:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(entry, dict) or str(entry.get("type") or "") != "assistant":
                        continue
                    if mode != "session":
                        stamp = _stamp_seconds(entry.get("timestamp"))
                        if not stamp or stamp < day_start:
                            continue
                    usage = entry.get("usageMetadata") if isinstance(entry.get("usageMetadata"), dict) else {}
                    value = _int(usage.get("totalTokenCount")) or (
                        _int(usage.get("promptTokenCount")) + _int(usage.get("candidatesTokenCount"))
                        + _int(usage.get("thoughtsTokenCount"))
                    )
                    total += value
        except OSError:
            continue
    return total


def _hermes_tokens(mode: str) -> int:
    """Hermes：state.db 的 sessions 表有会话级累计用量（input/output/cache 读写）。"""
    try:
        import agent_state

        db_path = str(agent_state._spec("hermes").get("db") or "")
    except Exception:  # noqa: BLE001
        return 0
    if not db_path or not os.path.exists(db_path):
        return 0
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db_path.replace("\\", "/"), uri=True, timeout=1.5)
        con.row_factory = sqlite3.Row
    except sqlite3.Error:
        return 0
    try:
        columns = {str(row["name"]) for row in con.execute("pragma table_info(sessions)").fetchall()}
        keys = [key for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")
                if key in columns]
        if not keys or "id" not in columns:
            return 0
        expr = " + ".join("coalesce(%s, 0)" % key for key in keys)
        if mode == "session":
            order = "coalesce(last_activity_at, started_at, 0)" if "last_activity_at" in columns else "started_at"
            row = con.execute("select %s as total from sessions order by %s desc limit 1" % (expr, order)).fetchone()
            return int(row["total"] or 0)
        # 今日：Hermes 只有会话级累计、没有逐日流水，就用"今天开始的会话"的累计值近似
        if "started_at" not in columns:
            return 0
        day_start = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        row = con.execute(
            "select coalesce(sum(%s), 0) as total from sessions where started_at >= ?" % expr, (day_start,)
        ).fetchone()
        return int(row["total"] or 0)
    except sqlite3.Error:
        return 0
    finally:
        try:
            con.close()
        except sqlite3.Error:
            pass


# 接入新 agent 时往这里加一行即可（返回该 agent 今天的 token 增量 / 会话累计）
_AGENT_TODAY_READERS = {
    "codex": _codex_today_tokens,
    "claude": lambda day: _jsonl_today_tokens(day, "claude"),
    "gemini": lambda day: _jsonl_today_tokens(day, "gemini"),
    "openclaw": lambda day: _jsonl_today_tokens(day, "openclaw"),
    "hermes": lambda day: _hermes_tokens("today"),
    "dsh": lambda day: _dsh_tokens("today"),
    "zcode": lambda day: _zcode_tokens("today"),
    "workbuddy": lambda day: _workbuddy_tokens("today"),
    "opencode": lambda day: _opencode_tokens("today"),
    "mimo": lambda day: _mimo_tokens("today"),
    "kimi": lambda day: _kimi_tokens("today"),
    "qwen": lambda day: _qwen_tokens("today"),
}
_AGENT_SESSION_READERS = {
    "codex": _codex_session_tokens,
    "claude": lambda: _jsonl_session_tokens("claude"),
    "gemini": lambda: _jsonl_session_tokens("gemini"),
    "openclaw": lambda: _jsonl_session_tokens("openclaw"),
    "hermes": lambda: _hermes_tokens("session"),
    "dsh": lambda: _dsh_tokens("session"),
    "zcode": lambda: _zcode_tokens("session"),
    "workbuddy": lambda: _workbuddy_tokens("session"),
    "opencode": lambda: _opencode_tokens("session"),
    "mimo": lambda: _mimo_tokens("session"),
    "kimi": lambda: _kimi_tokens("session"),
    "qwen": lambda: _qwen_tokens("session"),
}


# ------------------------------------------------------------------ public API


def today_tokens(agent: str = "") -> int:
    """今天的新增消耗；不传 agent 就是所有已接入 agent 的合计（多 agent 统计）。"""
    key = agent or "*"
    now = time.monotonic()
    hit = _today_cache.get(key)
    if hit and now - hit[0] < REFRESH_SECONDS:
        return hit[1]
    if agent:
        reader = _AGENT_TODAY_READERS.get(agent)
        value = int(reader(today_key())) if reader else 0
    else:
        value = sum(int(reader(today_key())) for reader in _AGENT_TODAY_READERS.values())
    _today_cache[key] = (now, value)
    return value


def session_tokens(agent: str = "codex") -> int:
    now = time.monotonic()
    hit = _session_cache.get(agent)
    if hit and now - hit[0] < REFRESH_SECONDS:
        return hit[1]
    reader = _AGENT_SESSION_READERS.get(agent)
    value = int(reader()) if reader else 0
    _session_cache[agent] = (now, value)
    return value


def usage_text(mode: str = "today", agent: str = "") -> str:
    """卡片上那一行小字。mode: today / session / off。"""
    mode = (mode or "today").lower()
    if mode in ("off", "none", "hide", ""):
        return ""
    if mode == "session":
        # 没接 token 统计的 agent、或者它还没有任何记录 —— 都别写个假的"本次 0 token"
        if agent and agent not in _AGENT_SESSION_READERS:
            return ""
        value = session_tokens(agent or "codex")
        if value <= 0:
            return ""
        return "本次 %s token" % format_tokens(value)
    value = today_tokens(agent)
    if value <= 0:
        return ""
    return "今日 +%s token" % format_tokens(value)


if __name__ == "__main__":
    import sys
    import time as _time

    mode = sys.argv[1] if len(sys.argv) > 1 else "today"
    started = _time.perf_counter()
    first = usage_text(mode)
    first_ms = (_time.perf_counter() - started) * 1000
    started = _time.perf_counter()
    second = usage_text(mode)
    second_ms = (_time.perf_counter() - started) * 1000
    print("mode=%s" % mode)
    print("  第一次（冷）: %-22s %.0f ms" % (first, first_ms))
    print("  第二次（缓存）: %-20s %.0f ms" % (second, second_ms))
    print("  今日原始值: %d" % today_tokens())
    print("  本会话原始值: %d" % session_tokens())
