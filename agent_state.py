"""统一的多 agent 状态源：设置里选了哪个 agent，桌宠就读谁的本地记录。

只读本地文件、不联网、不碰密钥类字段。所有适配器最后输出同一套 schema
（和 ``codex_state.snapshot()`` 完全一致）：

    state / label / title / activity / say / elapsed / tools / tokens / source / threadId

第一阶段接入的 agent（都是 CLI；桌面版和 CLI 共用一份记录的会标 shared，名字后不写 CLI）：

* ``codex``    —— 原有 codex_state.py，全量解析（桌面版 ChatGPT 和 codex CLI 共用 ~/.codex）
* ``claude``   —— Claude Code CLI：~/.claude/projects/<项目>/*.jsonl
* ``gemini``   —— Gemini CLI：~/.gemini/tmp/<项目>/chats/session-*.jsonl
* ``openclaw`` —— OpenClaw CLI：~/.openclaw/agents/<id>/sessions/*.jsonl
                  （会话记录还没生成时退化成"数据新旧 + 进程在不在"的忙闲判断）
* ``hermes``   —— Hermes CLI：<hermes home>/state.db 的 sessions / messages 表

第二阶段再做 ZCode、Kimi Code 等：在 ``_AGENTS`` 里加一条、补一个适配器函数就行。

另外留了一个"状态桥"的口子：任何 agent（包括自建 harness）用 status-bridge.py 把状态写进
``~/.codexpet/status/<agent>.json``，桌宠取最新且 3 分钟内更新过的那份，覆盖在上面 ——
读不到本地记录的 agent、或想补任务名/工具数的 agent 都可以用它。

命令行自检::

    python agent_state.py --list          # 有哪些 agent
    python agent_state.py claude          # 打印当前看到的状态
"""

from __future__ import annotations

import glob
import json
import os
import re
import time

HOME = os.path.expanduser("~")
BRIDGE_DIR = os.path.join(HOME, ".codexpet", "status")
BRIDGE_FRESH_SECONDS = 180.0
# 工具调用超过这么久还没看到结果，就不再当成"正在跑"（多半是记录不全或早就结束了）
PENDING_TOOL_MAX_AGE = 30 * 60.0
# 一次最多读会话文件尾巴多少字节（CLI 的长会话只关心最近这一段）
TAIL_BYTES = 1_200_000
# 最近这段时间内的错误 / 打断才演给用户看，太久以前的不算
RECENT_FOR_FLASH = 30.0
# 助手刚说完 → "完成了"保留这么久，之后回空闲
DONE_KEEP_SECONDS = 90.0
# 用户发完一直没动静：先当思考中，超过这个时间当"等待你"
WAITING_AFTER_SECONDS = 45.0
# 再久就回空闲（半小时没人碰就当收工）
IDLE_AFTER_SECONDS = 1800.0

VALID_STATES = {
    "idle", "thinking", "working", "waiting", "review",
    "syncing", "blocked", "error", "done", "interrupted",
}

STATE_LABELS = {
    "idle": "空闲",
    "thinking": "思考中",
    "working": "执行中",
    "waiting": "等待你",
    "review": "找工具",
    "syncing": "同步中",
    "blocked": "被挡住",
    "error": "出错了",
    "done": "完成了",
    "interrupted": "被打断",
}

STATE_HINT_ALIASES = {
    "running": "working",
    "working": "working",
    "in_progress": "working",
    "in-progress": "working",
    "active": "working",
    "pending": "working",
    "busy": "working",
    "thinking": "thinking",
    "reasoning": "thinking",
    "queued": "thinking",
    "waiting": "waiting",
    "waiting_for_user": "waiting",
    "waiting-for-user": "waiting",
    "needs_input": "waiting",
    "needs-input": "waiting",
    "paused": "waiting",
    "completed": "done",
    "complete": "done",
    "done": "done",
    "success": "done",
    "cancelled": "interrupted",
    "canceled": "interrupted",
    "interrupted": "interrupted",
    "aborted": "interrupted",
    "failed": "error",
    "failure": "error",
    "error": "error",
}


def _normalize_state_hint(value: str) -> str:
    key = str(value or "").strip().lower().replace(" ", "_")
    return STATE_HINT_ALIASES.get(key, key if key in STATE_LABELS else "")


# ------------------------------------------------------------------ agent 注册表


def _hermes_home() -> str:
    """Hermes 的数据目录：HERMES_HOME > %LOCALAPPDATA%\\hermes > ~/.hermes。"""
    env = os.environ.get("HERMES_HOME")
    if env:
        return env
    local = os.environ.get("LOCALAPPDATA") or os.path.join(HOME, "AppData", "Local")
    candidate = os.path.join(local, "hermes")
    if os.path.isdir(candidate):
        return candidate
    return os.path.join(HOME, ".hermes")


HERMES_HOME = _hermes_home()


def _opencode_data_dir() -> str:
    r"""OpenCode 桌面版/CLI 的数据目录：XDG_DATA_HOME > %LOCALAPPDATA%\opencode > ~/.local/share/opencode。"""
    env = (os.environ.get("XDG_DATA_HOME") or "").strip()
    if env:
        candidate = os.path.join(env, "opencode")
        if os.path.isdir(candidate):
            return candidate
    local = (os.environ.get("LOCALAPPDATA") or "").strip()
    if local:
        candidate = os.path.join(local, "opencode")
        if os.path.isdir(candidate):
            return candidate
    return os.path.join(HOME, ".local", "share", "opencode")


OPENCODE_DATA_DIR = _opencode_data_dir()


def _mimo_data_dir() -> str:
    r"""小米 MiMo 桌面版的引擎（mimocode）数据目录：XDG_DATA_HOME > ~/.local/share/mimocode。"""
    env = (os.environ.get("XDG_DATA_HOME") or "").strip()
    if env:
        candidate = os.path.join(env, "mimocode")
        if os.path.isdir(candidate):
            return candidate
    return os.path.join(HOME, ".local", "share", "mimocode")


MIMO_DATA_DIR = _mimo_data_dir()

_AGENTS: dict[str, dict] = {
    "codex": {
        "name": "Codex",
        "shared": True,          # 桌面版和 CLI 共用 ~/.codex 记录：名字不带 CLI、设置里不给提示
        "kind": "codex",
        "procs": ["ChatGPT", "codex"],
        "pkg": [r"node_modules[\\/]@openai[\\/]codex"],
        "roots": [os.path.join(HOME, ".codex", "sessions", "**", "*.jsonl")],
    },
    "claude": {
        "name": "Claude Code",
        "shared": False,         # 只接了 CLI 的会话记录（Claude 桌面版是另一套数据，读不到）
        "kind": "claude",
        "procs": ["claude"],
        "pkg": [r"node_modules[\\/]@anthropic-ai[\\/]claude-code"],
        "roots": [
            os.path.join(HOME, ".claude", "projects", "**", "*.jsonl"),
            os.path.join(HOME, ".claude", "sessions", "**", "*.jsonl"),
        ],
    },
    "gemini": {
        "name": "Gemini",
        "shared": False,
        "kind": "gemini",
        "procs": ["gemini"],
        "pkg": [r"node_modules[\\/]@google[\\/]gemini-cli"],
        "roots": [
            os.path.join(HOME, ".gemini", "tmp", "**", "chats", "*.jsonl"),
            os.path.join(HOME, ".gemini", "tmp", "**", "chats", "*.json"),
        ],
    },
    "openclaw": {
        "name": "OpenClaw",
        "shared": False,
        "kind": "openclaw",
        "procs": ["openclaw"],
        "pkg": [r"node_modules[\\/]openclaw"],
        "roots": [
            os.path.join(HOME, ".openclaw", "agents", "**", "sessions", "*.jsonl"),
            os.path.join(HOME, ".openclaw", "sessions", "**", "*.jsonl"),
        ],
        # 记录还没解析到时的忙闲兜底：这些数据文件在动 = 它在干活
        "recency": [
            os.path.join(HOME, ".openclaw", "agents", "**", "agent", "*.sqlite*"),
            os.path.join(HOME, ".openclaw", "agents", "**", "sessions", "*"),
            os.path.join(HOME, ".openclaw", "state", "openclaw.sqlite*"),
        ],
    },
    "hermes": {
        "name": "Hermes",
        "shared": False,
        "kind": "hermes",
        "procs": ["hermes", "hermes-acp"],
        "pkg": [r"hermes[\\/]bin[\\/]hermes"],
        "roots": [
            os.path.join(HERMES_HOME, "sessions", "**", "*.jsonl"),
            os.path.join(HERMES_HOME, "sessions", "*.json"),
        ],
        "db": os.path.join(HERMES_HOME, "state.db"),
        "recency": [
            os.path.join(HERMES_HOME, "state.db*"),
            os.path.join(HERMES_HOME, "sessions", "*"),
        ],
    },
    "dsh": {
        "name": "DeepSeek Harness",
        "shared": True,          # 桌面版和 CLI 共用 ~/.dsh 的会话记录：名字不标 CLI
        "kind": "dsh",
        "procs": ["DeepSeek Harness", "dsh-desktop", "dsh"],
        "roots": [
            os.path.join(HOME, ".dsh", "storages", "session_projcache", "sessions", "*.json"),
        ],
        "recency": [
            os.path.join(HOME, ".dsh", "storages", "session_projcache", "sessions", "*"),
            os.path.join(HOME, ".dsh", "sessions", "**", "session.v4.jsonl.zstd"),
        ],
    },
    "zcode": {
        "name": "ZCode",
        "shared": True,          # ZCode 桌面版落在同一个 CLI 会话库里
        "kind": "zcode",
        "procs": ["ZCode"],
        "db": os.path.join(HOME, ".zcode", "cli", "db", "db.sqlite"),
        "roots": [os.path.join(HOME, ".zcode", "cli", "db", "db.sqlite")],
        "recency": [
            os.path.join(HOME, ".zcode", "cli", "db", "db.sqlite*"),
            os.path.join(HOME, ".zcode", "v2", "tasks-index.sqlite*"),
        ],
    },
    "workbuddy": {
        "name": "WorkBuddy",
        "shared": True,
        "kind": "workbuddy",
        "procs": ["WorkBuddy"],
        "db": os.path.join(HOME, ".workbuddy", "workbuddy.db"),
        "roots": [os.path.join(HOME, ".workbuddy", "projects", "**", "*.jsonl")],
        "recency": [
            os.path.join(HOME, ".workbuddy", "workbuddy.db*"),
            os.path.join(HOME, ".workbuddy", "projects", "**", "*.jsonl"),
        ],
    },
    "opencode": {
        "name": "OpenCode",
        "shared": True,          # 桌面版和 CLI 共用 %LOCALAPPDATA%/opencode 的会话库
        "kind": "opencode",
        "procs": ["opencode-cli", "opencode"],
        "db": os.path.join(OPENCODE_DATA_DIR, "opencode.db"),
        "roots": [os.path.join(OPENCODE_DATA_DIR, "opencode.db")],
        "recency": [os.path.join(OPENCODE_DATA_DIR, "opencode.db*")],
    },
    "kimi": {
        "name": "Kimi Code",
        "shared": True,          # 桌面版和 CLI 共用 ~/.kimi-code
        "kind": "kimi",
        "procs": ["Kimi Code", "kimi-code", "kimi"],
        "pkg": [r"node_modules[\\/]@moonshot-ai[\\/]kimi-code"],
        # 一个会话一个目录：<工作区>/<session_xxx>/state.json（标题 / 最后一句 / 结束原因）
        # + agents/main/wire.jsonl（事件流：turn.prompt / llm.request / usage.record / turn.ended）
        "roots": [os.path.join(HOME, ".kimi-code", "sessions", "**", "state.json")],
        "wire": [os.path.join(HOME, ".kimi-code", "sessions", "**", "agents", "main", "wire.jsonl")],
        "recency": [
            os.path.join(HOME, ".kimi-code", "sessions", "**", "state.json"),
            os.path.join(HOME, ".kimi-code", "sessions", "**", "wire.jsonl"),
        ],
    },
    "mimo": {
        "name": "Xiaomi MiMo",
        "shared": True,          # 桌面版 = mimocode 引擎，和 mimo CLI 共用一份会话库
        "kind": "mimo",
        "procs": ["Xiaomi MiMo", "mimo"],
        "pkg": [r"node_modules[\\/]@mimo-ai[\\/]"],
        "db": os.path.join(MIMO_DATA_DIR, "mimocode.db"),
        "roots": [os.path.join(MIMO_DATA_DIR, "mimocode.db")],
        "recency": [os.path.join(MIMO_DATA_DIR, "mimocode.db*")],
    },
    "qwen": {
        "name": "Qwen Code",
        "shared": False,         # 只有 CLI（Gemini CLI 同族）
        "kind": "qwen",
        "procs": ["qwen"],
        "pkg": [r"node_modules[\\/]@qwen-code[\\/]qwen-code"],
        "transcripts": [
            os.path.join(HOME, ".qwen", "projects", "**", "chats", "*.jsonl"),
            os.path.join(HOME, ".qwen", "tmp", "**", "chats", "*.jsonl"),
        ],
        "runtimes": [os.path.join(HOME, ".qwen", "projects", "**", "chats", "*.runtime.json")],
        "roots": [
            os.path.join(HOME, ".qwen", "projects", "**", "chats", "*.jsonl"),
            os.path.join(HOME, ".qwen", "tmp", "**", "chats", "*.jsonl"),
        ],
        "recency": [
            os.path.join(HOME, ".qwen", "projects", "**", "chats", "*"),
            os.path.join(HOME, ".qwen", "tmp", "**", "*"),
        ],
    },
}

# 还没有本地记录的（Kimi Code 桌面版、小米 MiMo 桌面版）等用户聊过一次再按真实格式校准。


def _display_name(spec: dict) -> str:
    """下拉框和卡片上的名字：只接了 CLI 的 agent 后面标 CLI，桌面版共用的不标。"""
    name = str(spec.get("name") or "Agent")
    if not spec.get("shared") and not name.upper().endswith("CLI"):
        name += " CLI"
    return name


# 设置页两列网格的显示顺序：左列 = 最早那批（含 OpenCode），右列 = 桌面端那批。
# 注册表本身仍按"接入批次"分组，这里只影响展示，不动任何读取逻辑。
_AGENT_DISPLAY_ORDER = (
    "codex", "claude", "gemini", "openclaw", "hermes", "opencode",
    "dsh", "workbuddy", "mimo", "zcode", "kimi", "qwen",
)


def agent_list() -> list[dict]:
    """给设置页的 Agent 网格用（顺序 = 两列网格从左到右、从上到下的排布）。"""
    ordered = [item for item in _AGENT_DISPLAY_ORDER if item in _AGENTS]
    ordered += [item for item in _AGENTS if item not in _AGENT_DISPLAY_ORDER]
    return [
        {
            "id": agent_id,
            "name": _display_name(_AGENTS[agent_id]),
            "shared": bool(_AGENTS[agent_id].get("shared")),
        }
        for agent_id in ordered
    ]


def _spec(agent_id: str) -> dict:
    return _AGENTS.get(agent_id or "codex") or _AGENTS["codex"]


# ------------------------------------------------------------------ 小工具


def _now() -> float:
    return time.time()


def _shorten(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _compact(value) -> str:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return ""
    for unit, scale in (("M", 1_000_000.0), ("k", 1_000.0)):
        if abs(n) >= scale:
            short = n / scale
            text = ("%.1f" % short) if short < 100 else ("%.0f" % short)
            if text.endswith(".0"):
                text = text[:-2]
            return text + unit
    return "%d" % int(n)


def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _iso_seconds(stamp) -> float:
    """把 ISO 时间戳（带不带 Z / 毫秒都行）折成 Unix 秒；认不出来返回 0。"""
    if isinstance(stamp, (int, float)):
        value = float(stamp)
        if value > 1e11:      # 毫秒
            value /= 1000.0
        return value
    text = str(stamp or "").strip()
    if not text:
        return 0.0
    text = text.replace("Z", "+00:00")
    try:
        import datetime

        return datetime.datetime.fromisoformat(text).timestamp()
    except ValueError:
        pass
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})", text)
    if match:
        import calendar

        return calendar.timegm(tuple(int(part) for part in match.groups()) + (0, 0, 0)) - time.timezone
    return 0.0


_NEWEST_CACHE: dict[tuple, tuple[float, str | None, float]] = {}
NEWEST_CACHE_TTL = 3.0


def _newest_file(patterns) -> tuple[str | None, float]:
    key = tuple(patterns or [])
    now = _now()
    hit = _NEWEST_CACHE.get(key)
    if hit and now - hit[0] < NEWEST_CACHE_TTL:
        return hit[1], hit[2]
    path, newest = _newest_file_uncached(patterns)
    _NEWEST_CACHE[key] = (now, path, newest)
    return path, newest


def _newest_file_uncached(patterns) -> tuple[str | None, float]:
    path, newest = None, 0.0
    for pattern in patterns or []:
        try:
            matches = glob.glob(pattern, recursive=True)
        except (OSError, re.error):
            continue
        for item in matches:
            try:
                if not os.path.isfile(item):
                    continue
                mtime = os.path.getmtime(item)
            except OSError:
                continue
            if mtime > newest:
                path, newest = item, mtime
    return path, newest


def _read_tail_text(path: str, max_bytes: int = TAIL_BYTES) -> str:
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()          # 丢掉可能被截断的半行
            raw = fh.read()
    except OSError:
        return ""
    return raw.decode("utf-8", "replace")


def _load_jsonl(path: str, max_bytes: int = TAIL_BYTES) -> list[dict]:
    """按 JSONL 读会话文件尾巴；顺带兼容"一个 JSON 数组/对象"的老格式。"""
    text = _read_tail_text(path, max_bytes).strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    entries: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            entries.append(item)
    return entries


def _blocks(content) -> list[dict]:
    """把各家消息里的 content 统一成 block 列表。"""
    if isinstance(content, list):
        return [block for block in content if isinstance(block, dict)]
    if isinstance(content, str) and content:
        return [{"type": "text", "text": content}]
    return []


def _block_text(blocks) -> str:
    parts = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind in ("text", None, "input_text", "output_text") and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(part for part in parts if part).strip()


def _json_text(value) -> str:
    """有些记录把内容整段存成 JSON 字符串：能取出 text 就取，取不到原样返回。"""
    if isinstance(value, str):
        text = value.strip()
        if text[:1] in "[{":
            try:
                data = json.loads(text)
            except ValueError:
                return value
            if isinstance(data, list):
                return _block_text(_blocks(data)) or value
            if isinstance(data, dict):
                return _block_text(_blocks(data.get("content"))) or str(data.get("text") or value)
        return value
    if isinstance(value, list):
        return _block_text(_blocks(value))
    return ""


# ------------------------------------------------------------------ 进程兜底（Windows）


_PROC_CACHE: dict[str, tuple[float, bool]] = {}
PROC_CACHE_TTL = 8.0
_PROC_NAMES_CACHE: tuple[float, set[str]] | None = None
_PROC_SNAPSHOT_CACHE: tuple[float, list] | None = None
# node.exe 的命令行只能靠 PowerShell/WMI 拿 —— 起进程要几百毫秒到几秒，
# 而且个别机器上 WMI 会卡（实测会把整台机器拖住 2~7 秒）。
# 所以：**只有后台线程去扫**、调用方只读缓存，而且扫一次慢就成倍退避。
NODE_SCAN_MIN_TTL = 300.0     # 正常情况下最少 5 分钟扫一次
NODE_SCAN_MAX_TTL = 1800.0    # 慢机器最多退到半小时一次
_NODE_SCAN_TTL = NODE_SCAN_MIN_TTL
PROC_NAMES_TTL = 2.0          # 进程名快照缓存（原生 API 很便宜，但也别每毫秒查）
_NODE_CMDLINES: list[str] = []
_NODE_CMDLINES_AT = 0.0
_NODE_CMDLINES_BUSY = False


def _process_snapshot() -> list | None:
    """[ (pid, 父 pid, 进程名小写不带 .exe), ... ]，Toolhelp32 原生读，2 秒缓存。

    读不到返回 None（个别受限环境）。桌宠用它做两件事：进程兜底检测、
    以及"这个窗口是不是某个 agent 的终端"。
    """
    global _PROC_SNAPSHOT_CACHE
    now = _now()
    hit = _PROC_SNAPSHOT_CACHE
    if hit and now - hit[0] < PROC_NAMES_TTL:
        return hit[1]
    rows = None
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class PROCESSENTRY32W(ctypes.Structure):
                _fields_ = [
                    ("dwSize", wintypes.DWORD),
                    ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_void_p),
                    ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260),
                ]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
            kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
            kernel32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
            kernel32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

            TH32CS_SNAPPROCESS = 0x00000002
            snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
            if snapshot and snapshot != ctypes.c_void_p(-1).value:
                rows = []
                try:
                    entry = PROCESSENTRY32W()
                    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
                    ok = kernel32.Process32FirstW(ctypes.c_void_p(snapshot), ctypes.byref(entry))
                    while ok:
                        name = str(entry.szExeFile or "").strip().lower()
                        if name.endswith(".exe"):
                            name = name[:-4]
                        rows.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID), name))
                        ok = kernel32.Process32NextW(ctypes.c_void_p(snapshot), ctypes.byref(entry))
                finally:
                    kernel32.CloseHandle(ctypes.c_void_p(snapshot))
        except Exception:  # noqa: BLE001
            rows = None
    if rows is not None:
        _PROC_SNAPSHOT_CACHE = (now, rows)
    return rows


def find_process_pids(names) -> list[int]:
    """按可执行文件名（不带 .exe、忽略大小写）找 PID。"""
    wanted = {str(name).lower().removesuffix(".exe") for name in (names or []) if name}
    if not wanted:
        return []
    return [pid for pid, _parent, name in (_process_snapshot() or []) if name in wanted]


def ancestor_pids(pid: int, limit: int = 8) -> list[int]:
    """自己 + 往上数的父链（最多 limit 个 PID）。找不到就返回空。"""
    rows = _process_snapshot()
    if not rows:
        return []
    parents = {item[0]: item[1] for item in rows}
    chain: list[int] = []
    current = int(pid or 0)
    while current and current not in chain and len(chain) < limit:
        chain.append(current)
        current = parents.get(current, 0)
    return chain


def _process_names_native() -> set[str] | None:
    """用 Toolhelp32 读当前进程名（纯内存、不起子进程）。读不到返回 None。"""
    rows = _process_snapshot()
    if rows is None:
        return None
    return {name for _pid, _parent, name in rows}


def _process_names() -> set[str]:
    """当前进程名集合（小写、不带 .exe）：原生 API 优先，只有它失败才退回 tasklist。"""
    global _PROC_NAMES_CACHE
    now = _now()
    hit = _PROC_NAMES_CACHE
    if hit and now - hit[0] < PROC_NAMES_TTL:
        return hit[1]
    names = _process_names_native()
    if names is None:
        names = set()
        listing = _run_hidden(["tasklist", "/FO", "CSV", "/NH"], timeout=2.5)
        for line in listing.splitlines():
            head = line.split('","')[0].strip().strip('"').strip().lower()
            if head.endswith(".exe"):
                head = head[:-4]
            if head:
                names.add(head)
    _PROC_NAMES_CACHE = (now, names)
    return names


def _tasklist_matches(names, listing: str) -> bool:
    """从 tasklist 的 CSV 输出里找可执行文件名（纯函数，方便测；现在只当兜底用）。"""
    low = (listing or "").lower()
    for name in names or []:
        token = str(name).lower()
        if ('"%s.exe"' % token) in low or ('"%s"' % token) in low:
            return True
    return False


def _run_hidden(args: list[str], timeout: float = 4.0) -> str:
    import subprocess

    creation = 0x08000000 if os.name == "nt" else 0     # CREATE_NO_WINDOW
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
            creationflags=creation,
        )
    except Exception:
        return ""
    return proc.stdout or ""


def _scan_node_command_lines() -> list[str] | None:
    """起一次 PowerShell 拿 node.exe 的命令行（只在后台线程里调）。"""
    if os.name != "nt":
        return []
    # 只有 CIM **报错**（比如 WMI 被禁用）才退回老接口；
    # CIM 正常返回"0 个 node 进程"是有效答案，不能再起一路 WMI —— 个别机器上 WMI 会卡很久。
    ps = (
        "$p = @(); $ok = $true; "
        "try { $p = @(Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" -ErrorAction Stop) } "
        "catch { $ok = $false }; "
        "if (-not $ok) { try { $p = @(Get-WmiObject -Class Win32_Process -Filter \"Name='node.exe'\" -ErrorAction Stop) } catch { $p = @() } }; "
        "$p | Select-Object -ExpandProperty CommandLine"
    )
    text = _run_hidden(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps], timeout=6.0)
    return [line.strip() for line in text.splitlines() if line.strip()]


def _refresh_node_command_lines_async() -> None:
    """后台刷新 node 命令行缓存；调用方立刻返回（本轮先用旧缓存）。"""
    global _NODE_CMDLINES, _NODE_CMDLINES_AT, _NODE_CMDLINES_BUSY
    if os.name != "nt" or _NODE_CMDLINES_BUSY:
        return
    if _now() - _NODE_CMDLINES_AT < _NODE_SCAN_TTL:
        return
    _NODE_CMDLINES_BUSY = True

    def work() -> None:
        global _NODE_CMDLINES, _NODE_CMDLINES_AT, _NODE_CMDLINES_BUSY, _NODE_SCAN_TTL
        started = _now()
        try:
            lines = _scan_node_command_lines()
            if lines is not None:
                _NODE_CMDLINES = lines
        except Exception:  # noqa: BLE001
            pass
        finally:
            elapsed = _now() - started
            if elapsed > 2.0:
                # 这台机器上扫一次就能拖住系统：成倍退避，最多半小时一次
                _NODE_SCAN_TTL = min(_NODE_SCAN_TTL * 2.0, NODE_SCAN_MAX_TTL)
            else:
                _NODE_SCAN_TTL = NODE_SCAN_MIN_TTL
            _NODE_CMDLINES_AT = _now()      # 失败也等一个 TTL，别反复起进程
            _NODE_CMDLINES_BUSY = False

    import threading

    threading.Thread(target=work, name="agent-node-scan", daemon=True).start()


def _node_command_lines() -> list[str]:
    """node.exe 进程的命令行（只读后台缓存；缓存不新鲜时顺手叫后台刷新一下）。"""
    return list(_NODE_CMDLINES)


def _process_running(spec: dict, deep: bool = False) -> bool:
    """进程兜底：桌宠不知道它在聊什么，但至少知道"它开着"。

    先按可执行文件名找（Toolhelp32 原生 API，纯内存、不起子进程；只有它失败才退回
    tasklist）；``deep=True`` 时再看一眼
    Node 系 CLI（宿主是 node.exe，只认限定在 node_modules 的包路径正则 —— 不拿整条
    命令行乱匹配，避免"项目目录碰巧叫 openclaw"这种误判）。
    ``deep`` 用的命令行来自**后台线程**维护的缓存，本函数在任何线程里都不会被卡住。
    """
    if os.name != "nt":
        return False
    # 浅探测（只看可执行文件名）和深探测（还看 node 命令行）分开缓存：
    # 否则浅探测刚写下的"没找到"会把深探测也挡掉，Node 系 CLI 就永远轮不到看命令行。
    key = "%s|%s" % (spec.get("name") or "", "deep" if deep else "shallow")
    now = time.time()
    hit = _PROC_CACHE.get(key)
    if hit and now - hit[0] < PROC_CACHE_TTL:
        return hit[1]

    found = False
    wanted = {str(name).lower().removesuffix(".exe") for name in (spec.get("procs") or [])}
    if wanted:
        found = bool(wanted & _process_names())      # 原生查进程名：不起子进程
    if not found and deep and spec.get("pkg") and not os.environ.get("CODEXPET_NO_PROC_SCAN"):
        _refresh_node_command_lines_async()          # 叫后台去扫；这一轮先用手上的缓存
        for line in _node_command_lines():
            for pattern in spec["pkg"]:
                try:
                    if re.search(pattern, line, re.IGNORECASE):
                        found = True
                        break
                except re.error:
                    continue
            if found:
                break
    _PROC_CACHE[key] = (now, found)
    return found


# ------------------------------------------------------------------ 状态桥


def _bridge_overlay(agent_id: str) -> dict | None:
    """状态桥：~/.codexpet/status/<agent>.json，新鲜才认。"""
    path = os.path.join(BRIDGE_DIR, "%s.json" % agent_id)
    try:
        if _now() - os.path.getmtime(path) > BRIDGE_FRESH_SECONDS:
            return None
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    allowed = ("state", "label", "title", "activity", "say", "elapsed", "tools", "tokens", "threadId")
    overlay = {key: data[key] for key in allowed if data.get(key) not in (None, "")}
    if overlay.get("state") not in VALID_STATES:
        overlay.pop("state", None)
    if "state" in overlay and "label" not in overlay:
        overlay["label"] = STATE_LABELS.get(overlay["state"], "")
    return overlay or None


# ------------------------------------------------------------------ 通用收尾（各适配器共用）


def _idle_payload(spec: dict) -> dict:
    return {
        "_no_record": True,       # 内部标记：这是"完全没读到记录"的空闲，才允许进程兜底
        "state": "idle",
        "label": STATE_LABELS["idle"],
        "title": "",
        "activity": "",
        "say": "",
        "elapsed": 0,
        "tools": 0,
        "tokens": "",
        "source": _display_name(spec),
        "threadId": "",
    }


def _finish(
    spec: dict,
    *,
    title: str = "",
    say: str = "",
    activity: str = "",
    tools: int = 0,
    tokens: str = "",
    thread: str = "",
    cwd: str = "",
    last_user_ts: float = 0.0,
    last_ts: float = 0.0,
    last_role: str = "",
    pending_tool: str = "",
    last_error: bool = False,
    last_interrupt: bool = False,
    waiting_for_user: bool = False,
    needs_user_input: bool = False,   # agent 明确提问/等待确认：必须优先显示“等待你”
    state_hint: str = "",      # 记录里写死的状态（桌面端 App 会说 running / error）
    fallback_title: str = "",
) -> dict:
    """把"会话里看到了什么"翻译成桌宠的一个状态（各适配器最后都走这里）。

    "已用时"只算**当前这一轮**：用户刚发出请求、助手还没接上（或工具还在跑）时才开始走表；
    助手回过话（或会话已结束）就归零 —— 和 Codex 卡片的行为一致。
    它不是"这个会话开了多久"，更不是"我用它用了多久"。
    """
    now = _now()
    age = (now - last_ts) if last_ts else None

    if last_user_ts and (pending_tool or last_role in ("user", "tool")):
        elapsed = max(0.0, now - last_user_ts)
    else:
        elapsed = 0.0

    if last_error and (age is None or age < RECENT_FOR_FLASH):
        state = "error"
    elif last_interrupt and (age is None or age < RECENT_FOR_FLASH):
        state = "interrupted"
    elif needs_user_input:
        # 明确提问 / 等待确认比“还有工具行没关”更权威，否则会一直显示执行中。
        state = "waiting"
        activity = activity or "等你确认或回复"
    elif pending_tool:
        state = "working"
        activity = activity or ("调用工具 · %s" % pending_tool)
    elif last_role in ("user", "tool"):
        if age is None or age < WAITING_AFTER_SECONDS:
            state = "thinking"
            activity = activity or "正在思考下一步"
        elif age < IDLE_AFTER_SECONDS:
            state = "waiting"
            activity = activity or "等你确认或回复"
        else:
            state = "idle"
            activity = ""
    elif last_role == "assistant":
        if age is None or age < DONE_KEEP_SECONDS:
            state = "done"
            activity = activity or "任务完成"
        else:
            state = "idle"
            activity = ""
    else:
        state = "thinking" if (age is None or age < WAITING_AFTER_SECONDS) else "idle"
        activity = activity if state == "thinking" else ""

    # 桌面端 App / DB 直接写状态时以它为准，但先统一 running/completed/needs_input 等别名。
    hint = _normalize_state_hint(state_hint)
    if hint:
        if hint == "done":
            if age is None or age < DONE_KEEP_SECONDS:
                state = "done"
                activity = activity or "任务完成"
            elif age < IDLE_AFTER_SECONDS:
                state = "waiting"
                activity = "等你确认或回复"
            else:
                state = "idle"
                activity = ""
        elif hint == "waiting":
            state = "waiting"
            activity = "等你确认或回复"
        else:
            state = hint
            if state == "idle":
                activity = ""
            elif state == "thinking":
                activity = activity or "正在思考下一步"
            elif state == "working":
                activity = activity or ("调用工具 · %s" % pending_tool if pending_tool else "正在执行这一轮")

    # “完成了”先保留一小段时间；之后会话还活着就转为“等待你”，而不是闪成空闲。
    # 只有普通“任务结束等下一句”走这条，明确 needs_user_input 已经在上面立即等待。
    if waiting_for_user and state in ("done", "idle") and not needs_user_input:
        if state == "done" and (age is None or age < DONE_KEEP_SECONDS):
            pass
        elif age is None or age < IDLE_AFTER_SECONDS:
            state = "waiting"
            activity = "等你确认或回复"

    payload = {
        "state": state,
        "label": STATE_LABELS.get(state, ""),
        "title": _shorten(title or fallback_title, 60),
        "activity": "" if state == "idle" else activity,
        "say": _shorten(say, 110),
        "elapsed": int(max(0.0, min(elapsed, 12 * 3600.0))),
        "tools": int(tools),
        "tokens": tokens,
        "source": _display_name(spec),
        "threadId": thread,
    }
    if cwd:
        payload["cwd"] = cwd
    return payload


# ------------------------------------------------------------------ codex 适配器


def _codex_snapshot(spec: dict) -> dict:
    import codex_state

    payload = codex_state.snapshot()
    payload["source"] = _display_name(spec)
    return payload


# ------------------------------------------------------------------ Claude Code 适配器


def _claude_snapshot(spec: dict) -> dict:
    path, mtime = _newest_file(spec["roots"])
    if not path:
        return _idle_payload(spec)

    entries = _load_jsonl(path)
    title = ""
    summary = ""
    say = ""
    tools = 0
    tokens = ""
    thread = ""
    cwd = ""
    first_user_ts = 0.0
    last_user_ts = 0.0
    last_ts = mtime
    last_role = ""
    last_error = False
    last_interrupt = False
    pending: dict[str, str] = {}
    pending_ts: dict[str, float] = {}
    resolved: set[str] = set()

    for entry in entries:
        message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
        kind = str(entry.get("type") or "").lower()
        stamp = _iso_seconds(entry.get("timestamp")) or _iso_seconds(message.get("timestamp"))
        if stamp:
            last_ts = stamp
        thread = thread or str(entry.get("sessionId") or "")
        cwd = cwd or str(entry.get("cwd") or "")

        if kind == "summary":
            summary = str(entry.get("summary") or summary)
            continue
        if kind in ("system", "file-history-snapshot", "progress"):
            continue

        blocks = _blocks(message.get("content"))
        text = _block_text(blocks)
        tool_uses = [b for b in blocks if str(b.get("type") or "") == "tool_use"]
        tool_results = [b for b in blocks if str(b.get("type") or "") == "tool_result"]
        is_meta = bool(entry.get("isMeta"))

        if kind == "user":
            if tool_results:
                for block in tool_results:
                    tool_id = str(block.get("tool_use_id") or "")
                    if tool_id:
                        resolved.add(tool_id)
                last_role = "tool"
                last_error = any(bool(block.get("is_error")) for block in tool_results)
                last_interrupt = False
            elif text and not is_meta:
                if not first_user_ts:
                    first_user_ts = stamp
                last_user_ts = stamp
                if not title:
                    title = text
                last_role = "user"
                last_error = False
                last_interrupt = "interrupted by user" in text.lower() or "[request interrupted" in text.lower()
        elif kind == "assistant":
            for block in tool_uses:
                tool_id = str(block.get("id") or "")
                if tool_id:
                    pending[tool_id] = str(block.get("name") or "工具")[:24]
                    pending_ts[tool_id] = stamp or last_ts
            tools += len(tool_uses)
            if text:
                say = text
                last_role = "assistant"
                last_error = False
                last_interrupt = False
            usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
            if usage:
                total = sum(
                    _num(usage.get(key))
                    for key in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
                )
                if total:
                    tokens = _compact(total)

    open_ids = [
        tool_id for tool_id in pending
        if tool_id not in resolved
        and (last_ts - pending_ts.get(tool_id, last_ts)) < PENDING_TOOL_MAX_AGE
    ]
    pending_tool = pending[open_ids[-1]] if open_ids else ""
    return _finish(
        spec,
        title=title or summary,
        say=say,
        tools=tools,
        tokens=tokens,
        thread=thread or os.path.splitext(os.path.basename(path))[0],
        cwd=cwd,
        last_user_ts=last_user_ts,
        last_ts=last_ts,
        last_role=last_role,
        pending_tool=pending_tool,
        last_error=last_error,
        last_interrupt=last_interrupt,
        fallback_title="Claude Code 会话",
    )

# ------------------------------------------------------------------ Gemini CLI 适配器


def _gemini_human_user(message: dict) -> bool:
    """Gemini 记录里 "user" 也可能是工具返回（functionResponse）的合成消息，不是人说的。"""
    blocks = _blocks(message.get("content"))
    if not blocks:
        return False
    if any("functionResponse" in block or "function_response" in block for block in blocks):
        return False
    return bool(_block_text(blocks))


def _gemini_snapshot(spec: dict) -> dict:
    path, mtime = _newest_file(spec["roots"])
    if not path:
        return _idle_payload(spec)

    meta: dict = {}
    messages: list[dict] = []
    index: dict[str, int] = {}
    for record in _load_jsonl(path):
        if "$rewindTo" in record:
            rewind_id = str(record.get("$rewindTo") or "")
            if rewind_id in index:
                messages = messages[: index[rewind_id]]
            else:
                messages = []
            index = {str(item.get("id")): pos for pos, item in enumerate(messages) if item.get("id")}
            continue
        update = record.get("$set")
        if isinstance(update, dict):
            meta.update(update)
            continue
        if isinstance(record.get("messages"), list):        # 老格式：整份快照一行
            meta.update({key: value for key, value in record.items() if key != "messages"})
            messages = [item for item in record["messages"] if isinstance(item, dict)]
            index = {str(item.get("id")): pos for pos, item in enumerate(messages) if item.get("id")}
            continue
        if record.get("type") in (None, "") and "sessionId" in record and "id" not in record:
            meta.update(record)
            continue
        message_id = str(record.get("id") or "")
        if message_id and message_id in index:
            messages[index[message_id]] = record
        else:
            if message_id:
                index[message_id] = len(messages)
            messages.append(record)

    if not messages:
        return _idle_payload(spec)

    title = ""
    say = ""
    tools = 0
    tokens = ""
    first_user_ts = 0.0
    last_user_ts = 0.0
    last_ts = _iso_seconds(meta.get("lastUpdated")) or mtime
    last_role = ""
    last_error = False
    pending_tool = ""
    pending_ts = 0.0

    for message in messages:
        kind = str(message.get("type") or "").lower()
        stamp = _iso_seconds(message.get("timestamp"))
        if stamp:
            last_ts = stamp
        blocks = _blocks(message.get("content"))
        text = _block_text(blocks)

        if kind == "user":
            if _gemini_human_user(message):
                if not first_user_ts:
                    first_user_ts = stamp
                last_user_ts = stamp
                if not title:
                    title = text
                last_role = "user"
                last_error = False
            else:
                last_role = "tool"
            continue

        if kind in ("error",):
            last_error = True
            last_role = "assistant"
            continue
        if kind in ("warning", "info"):
            last_role = "tool"
            continue

        if kind == "gemini":
            calls = [call for call in (message.get("toolCalls") or []) if isinstance(call, dict)]
            calls += [block for block in blocks if "functionCall" in block or "function_call" in block]
            tools += len(calls)
            for call in calls:
                payload = call.get("functionCall") or call.get("function_call") or call
                if isinstance(payload, dict) and "result" in call:
                    continue
                if isinstance(payload, dict) and (call.get("result") is not None):
                    continue
                if isinstance(payload, dict):
                    pending_tool = str(payload.get("name") or "工具")[:24]
                    pending_ts = stamp or last_ts
            if text:
                say = text
                last_role = "assistant"
                last_error = False
            usage = message.get("tokens") if isinstance(message.get("tokens"), dict) else {}
            if usage:
                total = _num(usage.get("total")) or (
                    _num(usage.get("input")) + _num(usage.get("output"))
                    + _num(usage.get("cached")) + _num(usage.get("thoughts"))
                )
                if total:
                    tokens = _compact(total)

    if pending_tool and (last_ts - pending_ts) > PENDING_TOOL_MAX_AGE:
        pending_tool = ""
    return _finish(
        spec,
        title=title,
        say=say,
        tools=tools,
        tokens=tokens,
        thread=str(meta.get("sessionId") or os.path.splitext(os.path.basename(path))[0]),
        last_user_ts=last_user_ts,
        last_ts=last_ts,
        last_role=last_role,
        pending_tool=pending_tool,
        last_error=last_error,
        fallback_title="Gemini 会话",
    )


# ------------------------------------------------------------------ OpenClaw 适配器


def _openclaw_snapshot(spec: dict) -> dict:
    """优先读会话 JSONL；没有可读记录时退化成"数据新旧 + 进程"的忙闲判断。"""
    path, mtime = _newest_file(spec.get("roots"))
    if not path:
        payload = _recency_snapshot(spec, label="OpenClaw")
        return payload

    entries = _load_jsonl(path)
    title = ""
    say = ""
    tools = 0
    tokens = ""
    thread = ""
    cwd = ""
    first_user_ts = 0.0
    last_user_ts = 0.0
    last_ts = mtime
    last_role = ""
    last_error = False
    interrupted = False
    pending: dict[str, str] = {}
    pending_ts: dict[str, float] = {}
    resolved: set[str] = set()

    for entry in entries:
        message = entry.get("message") if isinstance(entry.get("message"), dict) else entry
        kind = str(entry.get("type") or message.get("type") or "").lower()
        if kind == "session":
            thread = thread or str(entry.get("id") or "")
            cwd = cwd or str(entry.get("cwd") or "")
            continue
        stamp = _iso_seconds(entry.get("timestamp")) or _iso_seconds(message.get("timestamp"))
        if stamp:
            last_ts = stamp
        cwd = cwd or str(entry.get("cwd") or message.get("cwd") or "")
        role = str(message.get("role") or "").lower()
        blocks = _blocks(message.get("content"))
        text = _block_text(blocks)
        tool_calls = [b for b in blocks if str(b.get("type") or "").lower() in ("toolcall", "tool_call", "function_call")]
        tool_results = [b for b in blocks if str(b.get("type") or "").lower() in ("toolresult", "tool_result", "function_response")]

        if role in ("user", "human"):
            if not first_user_ts:
                first_user_ts = stamp
            last_user_ts = stamp
            if not title:
                title = text
            last_role = "user"
            last_error = False
        elif role in ("assistant", "ai", "model"):
            for call in tool_calls:
                tool_id = str(call.get("id") or call.get("toolCallId") or "")
                if tool_id:
                    pending[tool_id] = str(call.get("name") or "工具")[:24]
                    pending_ts[tool_id] = stamp or last_ts
            tools += len(tool_calls)
            if text:
                say = text
                last_role = "assistant"
                last_error = False
            usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
            if usage:
                total = _num(usage.get("totalTokens") or usage.get("total"))
                if not total:
                    total = sum(
                        _num(usage.get(key))
                        for key in ("input", "output", "input_tokens", "output_tokens", "cacheRead", "cacheWrite")
                    )
                if total:
                    tokens = _compact(total)
        elif role in ("tool", "toolresult", "tool_result"):
            tool_id = str(message.get("toolCallId") or message.get("tool_call_id") or "")
            if tool_id:
                resolved.add(tool_id)
            for block in tool_results:
                block_id = str(block.get("toolCallId") or block.get("tool_use_id") or block.get("id") or "")
                if block_id:
                    resolved.add(block_id)
            last_role = "tool"
            last_error = bool(message.get("isError") or message.get("is_error"))

    if not title and not say and not last_role:
        # 文件是"session"等元信息行，没有任何消息 —— 当作没有记录
        return _recency_snapshot(spec, label="OpenClaw")

    open_ids = [
        tool_id for tool_id in pending
        if tool_id not in resolved
        and (last_ts - pending_ts.get(tool_id, last_ts)) < PENDING_TOOL_MAX_AGE
    ]
    pending_tool = pending[open_ids[-1]] if open_ids else ""
    payload = _finish(
        spec,
        title=title,
        say=say,
        tools=tools,
        tokens=tokens,
        thread=thread or os.path.splitext(os.path.basename(path))[0],
        cwd=cwd,
        last_user_ts=last_user_ts,
        last_ts=last_ts,
        last_role=last_role,
        pending_tool=pending_tool,
        last_error=last_error,
        last_interrupt=interrupted,
        fallback_title="OpenClaw 会话",
    )
    return payload


# ------------------------------------------------------------------ Hermes 适配器


def _hermes_snapshot(spec: dict) -> dict:
    db_path = str(spec.get("db") or "")
    payload = _hermes_db_snapshot(spec, db_path) if db_path and os.path.exists(db_path) else None
    if payload is not None:
        return payload
    return _recency_snapshot(spec, label="Hermes")


def _hermes_db_snapshot(spec: dict, db_path: str) -> dict | None:
    try:
        import sqlite3

        con = sqlite3.connect("file:%s?mode=ro" % db_path.replace("\\", "/"), uri=True, timeout=1.5)
        con.row_factory = sqlite3.Row
    except Exception:
        return None
    try:
        columns = {
            str(row["name"])
            for row in con.execute("pragma table_info(sessions)").fetchall()
        }
        if "id" not in columns:
            return None
        wanted = [
            col for col in (
                "id", "title", "display_name", "source", "model", "started_at", "ended_at", "end_reason",
                "message_count", "tool_call_count", "input_tokens", "output_tokens", "cache_read_tokens",
                "last_activity_at", "last_activity_description", "cwd",
            )
            if col in columns
        ]
        order = "coalesce(last_activity_at, started_at, 0)" if "last_activity_at" in columns else (
            "started_at" if "started_at" in columns else "rowid"
        )
        row = con.execute(
            "select %s from sessions order by %s desc limit 1" % (", ".join(wanted), order)
        ).fetchone()
        if row is None:
            return None
        data = {key: row[key] for key in wanted}

        session_id = str(data.get("id") or "")
        now = _now()
        last_ts = _num(data.get("last_activity_at")) or _num(data.get("started_at")) or os.path.getmtime(db_path)
        ended = data.get("ended_at") is not None

        say = ""
        try:
            text_row = con.execute(
                "select content from messages where session_id = ? and lower(role) in ('assistant','ai','model') "
                "order by timestamp desc limit 1",
                (session_id,),
            ).fetchone()
            if text_row is not None:
                say = _json_text(text_row["content"])
        except Exception:
            say = ""
        last_role = ""
        last_user_ts = 0.0
        newest_ts = 0.0
        try:
            row = con.execute(
                "select role, timestamp from messages where session_id = ? order by timestamp desc limit 1",
                (session_id,),
            ).fetchone()
            if row is not None:
                newest_ts = _num(row["timestamp"])
                role = str(row["role"] or "").strip().lower()
                if role in ("assistant", "ai", "model"):
                    last_role = "assistant"
                elif role in ("user", "human"):
                    last_role = "user"
                else:
                    last_role = "tool"      # 工具结果等：按"还在干活"算
        except Exception:
            last_role = ""
        try:
            row = con.execute(
                "select timestamp from messages where session_id = ? and lower(role) in ('user','human') "
                "order by timestamp desc limit 1",
                (session_id,),
            ).fetchone()
            if row is not None:
                last_user_ts = _num(row["timestamp"])
        except Exception:
            last_user_ts = 0.0

        title = str(data.get("title") or data.get("display_name") or "")
        if not title:
            try:
                row = con.execute(
                    "select content from messages where session_id = ? and lower(role) in ('user','human') "
                    "and (active is null or active = 1) order by timestamp asc limit 1",
                    (session_id,),
                ).fetchone()
                if row is not None:
                    title = _json_text(row["content"])
            except Exception:
                title = ""

        tokens = ""
        total_tokens = _num(data.get("input_tokens")) + _num(data.get("output_tokens")) + _num(data.get("cache_read_tokens"))
        if total_tokens:
            tokens = _compact(total_tokens)
        end_reason = str(data.get("end_reason") or "").lower()
        activity = str(data.get("last_activity_description") or "")
        last_ts = max(last_ts, newest_ts)

        if ended:
            if end_reason in ("error", "failed", "crash"):
                last_error = (now - last_ts) < 3600
            else:
                last_error = False
            return _finish(
                spec,
                title=title,
                say=say,
                activity=activity,
                tools=int(_num(data.get("tool_call_count"))),
                tokens=tokens,
                thread=session_id,
                cwd=str(data.get("cwd") or ""),
                last_ts=last_ts,
                last_role="assistant",     # 已结束的会话：最近结束过就演"完成了"
                last_error=last_error,
                fallback_title="Hermes 会话",
            )
        return _finish(
            spec,
            title=title,
            say=say,
            activity=activity,
            tools=int(_num(data.get("tool_call_count"))),
            tokens=tokens,
            thread=session_id,
            cwd=str(data.get("cwd") or ""),
            last_user_ts=last_user_ts,
            last_ts=last_ts,
            last_role=last_role or "tool",  # 会话还开着：按"最新一条是谁说的"推断在干活 / 等你
            waiting_for_user=(last_role == "assistant"),
            fallback_title="Hermes 会话",
        )
    except Exception:
        return None
    finally:
        try:
            con.close()
        except Exception:
            pass


# ------------------------------------------------------------------ 桌面端适配器（第二阶段）


def _connect_ro(path: str):
    """只读打开 sqlite：先普通只读；缺 shm / 库被占用就退到 immutable（绝不写盘、不起子进程）。"""
    import sqlite3

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


def _table_columns(con, table: str) -> set:
    try:
        return {str(row["name"]) for row in con.execute("pragma table_info(%s)" % table).fetchall()}
    except Exception:      # noqa: BLE001
        return set()


def _ms_seconds(value) -> float:
    """毫秒时间戳 → 秒。"""
    return _num(value) / 1000.0


def _token_text(total) -> str:
    value = int(_num(total))
    return _compact(value) if value > 0 else ""


def _dict_of(row) -> dict:
    try:
        return {key: row[key] for key in row.keys()}
    except Exception:      # noqa: BLE001
        return {}


def _json_dict(text):
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# -------------------------------------------------------- DeepSeek Harness（dsh 桌面版）

def _dsh_val(rows: dict, key: str):
    item = rows.get(key)
    if isinstance(item, dict) and "val" in item:
        return item.get("val")
    return None


def _dsh_block(rows: dict, key: str) -> dict:
    value = _dsh_val(rows, key)
    return value if isinstance(value, dict) else {}


def _dsh_snapshot(spec: dict) -> dict:
    """dsh 桌面版：~/.dsh/storages/session_projcache/sessions/*.json 是实时的"投影缓存"。

    里面直接写了：这一轮开着没有（openTurnStartSeq / openStep / pendingCalls）、
    有没有在等用户回答（userQuestions.active）、累计 token、最后一次提问时间。
    """
    path, mtime = _newest_file(spec.get("roots") or [])
    if not path:
        return _idle_payload(spec)
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return _idle_payload(spec)
    record = data.get("record") if isinstance(data, dict) else None
    rows = record.get("rows") if isinstance(record, dict) else None
    if not isinstance(rows, dict):
        return _idle_payload(spec)
    identity = record.get("identity") if isinstance(record.get("identity"), dict) else {}

    stats = _dsh_block(rows, "sessionStats")
    boundary = _dsh_block(rows, "turnBoundary")
    usage = _dsh_block(rows, "tokenUsage")
    questions = _dsh_block(rows, "userQuestions")
    outline = _dsh_block(rows, "turnOutline")
    meta = _dsh_block(rows, "sessionListMetadata")

    title = str(_dsh_val(rows, "title") or "")
    say = ""
    turns = outline.get("turns") if isinstance(outline.get("turns"), list) else []
    if turns and isinstance(turns[-1], dict):
        say = str(turns[-1].get("response") or "")
        if not title:
            title = str(turns[-1].get("prompt") or "")

    pending = stats.get("pendingCalls") if isinstance(stats.get("pendingCalls"), dict) else {}
    pending_tool = ""
    for item in pending.values():
        if isinstance(item, dict):
            pending_tool = str(item.get("name") or item.get("tool") or item.get("title") or "")
        elif isinstance(item, str):
            pending_tool = item
        if pending_tool:
            break

    last_boundary = boundary.get("lastStepBoundary")
    last_boundary = last_boundary if isinstance(last_boundary, dict) else {}
    open_turn = boundary.get("openTurnStartSeq")
    open_step = stats.get("openStep")
    busy = bool(pending) or open_step is not None or open_turn is not None or str(last_boundary.get("kind") or "") == "start"

    q_block = questions.get("questions") if isinstance(questions.get("questions"), dict) else {}
    active_questions = [item for item in (q_block.get("active") or []) if item]

    totals = usage.get("totals") if isinstance(usage.get("totals"), dict) else {}
    tokens = _token_text(sum(_num(totals.get(key)) for key in (
        "uncachedInputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens")))

    last_prompt = _ms_seconds(meta.get("lastPromptAt"))
    last_ts = mtime or _now()

    state_hint = ""
    activity = ""
    last_role = "assistant"
    waiting = bool(active_questions)
    if busy:
        last_role = "tool"
        state_hint = "working"
        if pending_tool:
            activity = "调用工具 · %s" % pending_tool
        else:
            turn_no = stats.get("lastTurn")
            activity = "正在执行第 %s 轮" % turn_no if turn_no else "正在执行这一轮"

    return _finish(
        spec,
        title=title,
        say=say,
        activity=activity,
        tokens=tokens,
        thread=os.path.splitext(os.path.basename(path))[0],
        cwd=str(identity.get("cwd") or ""),
        last_user_ts=last_prompt if busy else 0.0,
        last_ts=last_ts,
        last_role=last_role,
        pending_tool=pending_tool if busy else "",
        state_hint=state_hint,
        needs_user_input=waiting,
        fallback_title="DeepSeek Harness 会话",
    )


# -------------------------------------------------------- ZCode（桌面版走 ~/.zcode/cli 的同一个库）

_ZCODE_TOKEN_KEYS = (
    "input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
)


def _zcode_snapshot(spec: dict) -> dict:
    db_path = str(spec.get("db") or "")
    if not db_path or not os.path.exists(db_path):
        return _recency_snapshot(spec, label="ZCode")
    con = None
    try:
        con = _connect_ro(db_path)
        row = con.execute(
            "select id, title, directory, time_updated from session order by time_updated desc limit 1"
        ).fetchone()
        if row is None:
            return _idle_payload(spec)
        sid = str(row["id"] or "")

        turn = None
        if _table_columns(con, "turn_usage"):
            turn = con.execute(
                "select * from turn_usage where session_id = ? order by started_at desc limit 1", (sid,)
            ).fetchone()
        turn = _dict_of(turn) if turn is not None else {}

        last_role = ""
        last_user_ts = 0.0
        newest_id = ""
        for item in con.execute(
            "select id, data, time_created from message where session_id = ? order by time_created desc limit 40",
            (sid,),
        ):
            data = _json_dict(item["data"])
            if not data:
                continue
            meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
            if str(meta.get("visibility") or "") == "model-only":
                continue      # 系统注入的提醒 / todo reminder 不算真的用户发言
            role = str(data.get("role") or "").lower()
            if role in ("user", "assistant") and not last_role:
                last_role = role
                newest_id = str(item["id"] or "")
            if role == "user" and not last_user_ts:
                last_user_ts = _ms_seconds(item["time_created"])
            if last_role and last_user_ts:
                break

        say = ""
        for item in con.execute(
            "select data from part where session_id = ? order by time_created desc limit 80", (sid,)
        ):
            data = _json_dict(item["data"])
            if data and str(data.get("type") or "") == "text":
                say = str(data.get("text") or "")
                if say:
                    break

        # 最新一条消息（通常是最新一步 assistant 回复）里没收尾的证据：
        # ZCode 每一步都新起一条 assistant 消息，正在跑的那步**没有 step-finish**；
        # 工具只认这条消息里的 running/pending —— 免得翻到上一轮的僵尸工具。
        pending_tool = ""
        step_finished = False
        if newest_id and last_role == "assistant":
            for item in con.execute(
                "select data from part where message_id = ? order by time_created desc limit 60",
                (newest_id,),
            ):
                data = _json_dict(item["data"])
                if not data:
                    continue
                kind = str(data.get("type") or "")
                if kind == "step-finish":
                    step_finished = True
                elif kind == "tool" and not pending_tool:
                    state = data.get("state") if isinstance(data.get("state"), dict) else {}
                    if str(state.get("status") or "") in ("running", "pending"):
                        pending_tool = str(data.get("tool") or "工具")

        # 桌面版一整轮跑完前一般**不写 turn_usage**，但 tool_usage / model_usage
        # 会实时写 running —— 这才是"现在在不在干活"的第一手来源。
        live_tool = ""
        live_model = False
        tools_live = None
        tokens_live = None
        since_ms = int(last_user_ts * 1000) if last_user_ts else 0
        try:
            if _table_columns(con, "tool_usage"):
                sql = "select tool_name from tool_usage where session_id = ? and status = 'running'"
                params = [sid]
                if since_ms:
                    sql += " and started_at >= ?"      # 只认"这一轮"里的实时行
                    params.append(since_ms)
                got = con.execute(sql + " order by started_at desc limit 1", params).fetchone()
                if got is not None:
                    live_tool = str(got["tool_name"] or "工具")
                if since_ms:
                    got = con.execute(
                        "select count(*) from tool_usage where session_id = ? and started_at >= ?",
                        (sid, since_ms),
                    ).fetchone()
                    if got is not None:
                        tools_live = int(_num(got[0]))
        except Exception:      # noqa: BLE001
            live_tool = ""
        try:
            if _table_columns(con, "model_usage"):
                sql = "select 1 from model_usage where session_id = ? and status = 'running'"
                params = [sid]
                if since_ms:
                    sql += " and started_at >= ?"
                    params.append(since_ms)
                if con.execute(sql + " limit 1", params).fetchone() is not None:
                    live_model = True
                if since_ms:
                    got = con.execute(
                        "select sum(input_tokens) + sum(output_tokens) + sum(cache_creation_input_tokens) "
                        "+ sum(cache_read_input_tokens) from model_usage where session_id = ? and started_at >= ?",
                        (sid, since_ms),
                    ).fetchone()
                    if got is not None and _num(got[0]) > 0:
                        tokens_live = _token_text(got[0])
        except Exception:      # noqa: BLE001
            live_model = False

        status = str(turn.get("status") or "")
        started = _ms_seconds(turn.get("started_at"))
        completed = _ms_seconds(turn.get("completed_at"))
        # turn_usage 只有"当前这一轮"的行才算数：桌面版当前轮没跑完时没有行，
        # 查出来的往往是上一轮的历史记录，不能拿它判断本轮状态或用量。
        turn_is_current = bool(turn) and (not started or not last_user_ts or started >= last_user_ts - 1.0)
        if turn_is_current:
            last_ts = completed or _ms_seconds(row["time_updated"]) or _now()
            tools = int(_num(turn.get("tool_call_count")))
            tokens = _token_text(sum(_num(turn.get(key)) for key in _ZCODE_TOKEN_KEYS))
        else:
            last_ts = _ms_seconds(row["time_updated"]) or _now()
            status = ""
            started = 0.0
            completed = 0.0
            tools = tools_live if tools_live is not None else 0
            tokens = tokens_live if tokens_live is not None else ""

        step_open = bool(last_role == "assistant" and not step_finished)
        step_live = bool(not turn_is_current and step_open and (_now() - last_ts) < IDLE_AFTER_SECONDS)

        state_hint = ""
        activity = ""
        last_error = False
        last_interrupt = False
        needs_input = False
        if status == "error":
            last_error = True
            last_ts = completed or last_ts
        elif status in ("cancelled", "canceled", "interrupted", "aborted"):
            last_interrupt = True
            last_ts = completed or last_ts
        elif status in ("waiting", "waiting_for_user", "waiting_for_input", "needs_input", "paused"):
            needs_input = True
            last_role = "assistant"
            last_ts = completed or last_ts
        elif status == "running" or live_tool or live_model or step_live:
            last_role = "tool"          # 还在这一轮里：让"已用时"继续走
            last_user_ts = started or last_user_ts
            tool_now = live_tool or pending_tool
            if tool_now:
                state_hint = "working"
                activity = "调用工具 · %s" % tool_now
            elif live_model:
                state_hint = "thinking"
                activity = "正在思考下一步"
            elif status == "running":
                state_hint = "working"
                activity = "正在执行这一步"
            else:
                state_hint = "thinking"
                activity = "正在思考下一步"

        return _finish(
            spec,
            title=str(row["title"] or ""),
            say=say,
            activity=activity,
            tools=tools,
            tokens=tokens,
            thread=sid,
            cwd=str(row["directory"] or ""),
            last_user_ts=last_user_ts,
            last_ts=last_ts,
            last_role=last_role or "assistant",
            pending_tool=pending_tool if state_hint == "working" else "",
            last_error=last_error,
            last_interrupt=last_interrupt,
            state_hint=state_hint,
            needs_user_input=needs_input,
            fallback_title="ZCode 会话",
        )
    except Exception:      # noqa: BLE001
        return _recency_snapshot(spec, label="ZCode")
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:      # noqa: BLE001
                pass

# -------------------------------------------------------- WorkBuddy

def _workbuddy_db_row(db_path: str, session_id: str) -> dict:
    if not db_path or not session_id or not os.path.exists(db_path):
        return {}
    con = None
    try:
        con = _connect_ro(db_path)
        if not _table_columns(con, "sessions"):
            return {}
        row = con.execute(
            "select title, custom_title, status, last_activity_at, updated_at from sessions where id = ?",
            (session_id,),
        ).fetchone()
        return _dict_of(row) if row is not None else {}
    except Exception:      # noqa: BLE001
        return {}
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:      # noqa: BLE001
                pass


def _workbuddy_snapshot(spec: dict) -> dict:
    """WorkBuddy：会话正文在 ~/.workbuddy/projects/<项目>/<会话>.jsonl（和 Claude 类似），
    状态 / 最后活动时间在 ~/.workbuddy/workbuddy.db 的 sessions 表。"""
    path, mtime = _newest_file(spec.get("roots") or [])
    if not path:
        return _recency_snapshot(spec, label="WorkBuddy")
    session_id = os.path.splitext(os.path.basename(path))[0]
    meta = _workbuddy_db_row(str(spec.get("db") or ""), session_id)
    entries = _load_jsonl(path, 400_000)

    title = str(meta.get("custom_title") or meta.get("title") or "")
    last_ts = _ms_seconds(meta.get("last_activity_at")) or mtime or _now()

    last_msg = None
    last_msg_index = -1
    for index in range(len(entries) - 1, -1, -1):
        entry = entries[index]
        if str(entry.get("type") or "") == "message" and str(entry.get("role") or "").lower() in ("user", "assistant"):
            last_msg, last_msg_index = entry, index
            break

    last_role = ""
    last_user_ts = 0.0
    say = ""
    tokens = ""
    for entry in reversed(entries):
        if isinstance(entry, dict) and str(entry.get("role") or "").lower() == "user":
            last_user_ts = _ms_seconds(entry.get("timestamp"))
            break
    if last_msg is not None:
        role = str(last_msg.get("role") or "").lower()
        last_role = "user" if role == "user" else "assistant"
        stamp = _ms_seconds(last_msg.get("timestamp"))
        if stamp:
            last_ts = stamp
        if role == "user":
            last_user_ts = stamp
        else:
            provider = last_msg.get("providerData") if isinstance(last_msg.get("providerData"), dict) else {}
            raw = provider.get("rawUsage") if isinstance(provider.get("rawUsage"), dict) else {}
            usage = provider.get("usage") if isinstance(provider.get("usage"), dict) else {}
            total = _num(raw.get("total_tokens")) or _num(usage.get("total_tokens")) or _num(usage.get("total"))
            tokens = _token_text(total)
            say = _block_text(last_msg.get("content"))

    for entry in entries[last_msg_index + 1:]:
        if not isinstance(entry, dict):
            continue
        if not title and str(entry.get("type") or "") == "ai-title":
            title = str(entry.get("aiTitle") or entry.get("title") or "")

    tools = 0
    pending_tool = ""
    for entry in entries[last_msg_index + 1:]:
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("type") or "").lower()
        if "tool" in kind:
            tools += 1
            pending_tool = str(entry.get("toolName") or entry.get("name") or entry.get("tool") or "工具")

    status = str(meta.get("status") or "").strip().lower()
    state_hint = ""
    last_error = False
    last_interrupt = False
    needs_input = status in ("waiting", "waiting_for_user", "waiting_for_input", "needs_input", "paused")
    if status in ("running", "working", "in_progress", "active"):
        state_hint = "working"
    elif status in ("error", "failed"):
        last_error = True
    elif status in ("cancelled", "canceled", "interrupted", "aborted"):
        last_interrupt = True
    if pending_tool and not state_hint and not needs_input:
        state_hint = "working"
    if state_hint == "working":
        last_role = "tool"      # 让"已用时"按当前这轮走表，而不是从助手回话算

    waiting = (last_role == "assistant" and not state_hint and not last_error and not last_interrupt
               and (_now() - last_ts) < IDLE_AFTER_SECONDS)

    return _finish(
        spec,
        title=title,
        say=say,
        activity=("调用工具 · %s" % pending_tool) if pending_tool else "",
        tools=tools,
        tokens=tokens,
        thread=session_id,
        cwd=str(last_msg.get("cwd") or "") if isinstance(last_msg, dict) else "",
        last_user_ts=last_user_ts,
        last_ts=last_ts,
        last_role=last_role or "assistant",
        pending_tool=pending_tool,
        last_error=last_error,
        last_interrupt=last_interrupt,
        state_hint=state_hint,
        waiting_for_user=waiting,
        needs_user_input=needs_input,
        fallback_title="WorkBuddy 会话",
    )


# -------------------------------------------------------- OpenCode

def _opencode_role(parsed, raw: str) -> str:
    text = ""
    if isinstance(parsed, dict):
        for key in ("role", "type", "kind", "name"):
            value = parsed.get(key)
            if isinstance(value, str):
                text += " " + value.lower()
        message = parsed.get("message")
        if isinstance(message, dict) and isinstance(message.get("role"), str):
            text += " " + message["role"].lower()
    haystack = text + " " + str(raw or "").lower()
    for needle, role in (("assistant", "assistant"), ("user", "user"), ("tool", "tool"), ("system", "tool")):
        if needle in haystack:
            return role
    return ""


def _opencode_say(parsed) -> str:
    if not isinstance(parsed, dict):
        return ""
    for key in ("text", "content", "message"):
        value = parsed.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, list):
            text = _block_text(value)
            if text:
                return text
    parts = parsed.get("parts")
    if isinstance(parts, list):
        texts = []
        for part in parts:
            if isinstance(part, dict):
                item = _block_text([part])
                if item:
                    texts.append(item)
        if texts:
            return "\n".join(texts)
    return ""


def _opencode_snapshot(spec: dict) -> dict:
    """OpenCode 桌面版 / CLI 共用 openext.db：session_v2 一行一个会话，session_message 是消息流水。"""
    db_path = str(spec.get("db") or "")
    if not db_path or not os.path.exists(db_path):
        return _recency_snapshot(spec, label="OpenCode")
    con = None
    try:
        con = _connect_ro(db_path)
        columns = _table_columns(con, "session_v2")
        if not columns:
            return _recency_snapshot(spec, label="OpenCode")
        wanted = [
            name for name in (
                "id", "title", "directory", "time_created", "time_updated", "time_idle",
                "tokens_input", "tokens_output", "tokens_reasoning", "tokens_cache_read", "tokens_cache_write",
            )
            if name in columns
        ]
        row = con.execute(
            "select %s from session_v2 order by time_updated desc limit 1" % ", ".join(wanted)
        ).fetchone()
        if row is None:
            return _idle_payload(spec)
        data = _dict_of(row)
        sid = str(data.get("id") or "")
        last_ts = _ms_seconds(data.get("time_updated")) or _now()

        last_role = ""
        say = ""
        pending_tool = ""
        last_user_ts = 0.0
        if _table_columns(con, "session_message"):
            for item in con.execute(
                "select type, data, time_created from session_message where session_id = ? order by seq desc limit 40",
                (sid,),
            ):
                raw = str(item["data"] or "")
                parsed = _json_dict(raw)
                role = _opencode_role(parsed, raw)
                if not last_role and role:
                    last_role = role
                    last_ts = _ms_seconds(item["time_created"]) or last_ts
                if role == "tool" and not pending_tool and ('"running"' in raw or '"pending"' in raw):
                    pending_tool = "工具"
                if role == "user" and not last_user_ts:
                    last_user_ts = _ms_seconds(item["time_created"])
                if not say and parsed:
                    say = _opencode_say(parsed)
                if last_role and last_role != "tool" and say:
                    break

        tokens = _token_text(sum(_num(data.get(key)) for key in (
            "tokens_input", "tokens_output", "tokens_reasoning", "tokens_cache_read", "tokens_cache_write")))

        state_hint = "working" if pending_tool else ""
        if state_hint == "working":
            last_role = "tool"
        waiting = (last_role == "assistant" and not state_hint and (_now() - last_ts) < IDLE_AFTER_SECONDS)

        return _finish(
            spec,
            title=str(data.get("title") or ""),
            say=say,
            activity=("调用工具 · %s" % pending_tool) if pending_tool else "",
            tokens=tokens,
            thread=sid,
            cwd=str(data.get("directory") or ""),
            last_user_ts=last_user_ts,
            last_ts=last_ts,
            last_role=last_role or "assistant",
            pending_tool=pending_tool,
            state_hint=state_hint,
            waiting_for_user=waiting,
            fallback_title="OpenCode 会话",
        )
    except Exception:      # noqa: BLE001
        return _recency_snapshot(spec, label="OpenCode")
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:      # noqa: BLE001
                pass


# -------------------------------------------------------- Kimi Code

def _kimi_repr(value):
    """Kimi 的 wire.jsonl 里有些字段是 Python repr 字符串（单引号），安全地还原成对象。"""
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text[0] not in "[{":
        return None
    try:
        import ast

        return ast.literal_eval(text)
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return None


def _kimi_message_text(blob):
    """agent.message.appended 的 message 字段 → (role, text)。"""
    data = _kimi_repr(blob)
    if not isinstance(data, dict):
        return "", ""
    message = data.get("message") if isinstance(data.get("message"), dict) else data
    role = str(message.get("role") or "").lower()
    content = message.get("content")
    texts = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                texts.append(block["text"])
    elif isinstance(content, str):
        texts.append(content)
    return role, "\n".join(item for item in texts if item).strip()


def _kimi_wire_summary(path):
    """wire.jsonl（一行一个事件）→ 最后说话的是谁 / 还在不在跑 / token。"""
    info = {"role": "", "say": "", "prompt": "", "tokens": "", "busy": False,
            "error": False, "interrupted": False, "last_ts": 0.0, "last_user_ts": 0.0}
    for entry in _load_jsonl(path, 400_000):
        etype = str(entry.get("type") or "")
        stamp = _ms_seconds(entry.get("time"))
        if stamp:
            info["last_ts"] = stamp
        if etype == "turn.prompt":
            blocks = _kimi_repr(entry.get("input")) or []
            text = "\n".join(
                block.get("text", "") for block in blocks
                if isinstance(block, dict) and isinstance(block.get("text"), str)
            ).strip()
            info["prompt"] = text or info["prompt"]
            info["role"] = "user"
            info["last_user_ts"] = stamp
            info["busy"] = True
        elif etype in ("agent.turn.started", "llm.request", "plugin.session_start"):
            info["busy"] = True
        elif etype == "agent.message.appended":
            role, text = _kimi_message_text(entry.get("message"))
            if role in ("user", "assistant"):
                info["role"] = role
                info["busy"] = role != "assistant"
                if role == "assistant" and text:
                    info["say"] = text
        elif etype == "usage.record":
            usage = _kimi_repr(entry.get("usage"))
            if isinstance(usage, dict):
                total = sum(_num(usage.get(key)) for key in (
                    "inputOther", "output", "inputCacheRead", "inputCacheCreation"))
                if total:
                    info["tokens"] = _token_text(total)
        elif etype in ("token_counting.turn_recorded", "token_counting.measured"):
            value = _num(entry.get("tokens"))
            if value:
                info["tokens"] = _token_text(value)
        elif etype == "agent.turn.ended":
            outcome = str(entry.get("outcome") or "done").lower()
            info["busy"] = False
            info["role"] = "assistant"
            if outcome in ("cancelled", "canceled", "interrupted", "aborted"):
                info["interrupted"] = True
            elif outcome not in ("done", "completed", "ok", "success", "stop"):
                info["error"] = True
        elif etype == "turn.ended":
            reason = str(entry.get("reason") or "").lower()
            info["busy"] = False
            if reason in ("failed", "error"):
                info["error"] = True
            elif reason in ("aborted", "cancelled", "canceled", "interrupted"):
                info["interrupted"] = True
            if reason in ("completed", "done", "stop"):
                info["role"] = "assistant"
        elif etype == "prompt.completed":
            info["busy"] = False
            info["role"] = "assistant"
    return info


def _kimi_snapshot(spec):
    """Kimi Code：桌面版和 CLI 共用 ~/.kimi-code。

    结构：sessions/<工作区>/<会话>/state.json（标题、最后一句、结束原因）
    + agents/main/wire.jsonl（事件流）。没有会话记录时交给"进程在跑"的兜底。
    """
    path, mtime = _newest_file(spec.get("roots") or [])
    if not path:
        return _idle_payload(spec)
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        return _idle_payload(spec)
    if not isinstance(state, dict):
        return _idle_payload(spec)
    session_dir = os.path.dirname(path)
    title = str(state.get("title") or state.get("lastPrompt") or "").strip()
    cwd = str(state.get("cwd") or "")
    thread = str(state.get("id") or os.path.basename(session_dir))
    updated = _ms_seconds(state.get("updatedAt")) or mtime
    reason = str(state.get("lastTurnReason") or "").lower()

    wire = os.path.join(session_dir, "agents", "main", "wire.jsonl")
    info = _kimi_wire_summary(wire) if os.path.exists(wire) else {}

    last_ts = info.get("last_ts") or updated
    last_user_ts = info.get("last_user_ts") or 0.0
    role = str(info.get("role") or "")
    error = bool(info.get("error")) or reason in ("failed", "error")
    interrupted = bool(info.get("interrupted")) or reason in ("aborted", "cancelled", "canceled", "interrupted")
    busy = bool(info.get("busy"))
    if not role and not busy:
        role = "assistant" if reason in ("completed", "done", "stop") else ""

    state_hint = ""
    if busy:
        state_hint = "working"
        role = "tool"
    return _finish(
        spec,
        title=title,
        say=str(info.get("say") or ""),
        activity=("正在执行这一轮" if busy else ""),
        tokens=str(info.get("tokens") or ""),
        thread=thread,
        cwd=cwd,
        last_user_ts=last_user_ts,
        last_ts=last_ts,
        last_role=role,
        last_error=error,
        last_interrupt=interrupted,
        state_hint=state_hint,
        waiting_for_user=(role == "assistant" and not state_hint and not error and not interrupted),
        fallback_title="Kimi Code 会话",
    )


# -------------------------------------------------------- 小米 MiMo（mimocode 引擎）

def _mimo_token_text(value) -> str:
    """step-finish 里的 tokens 结构：{total, input, output, reasoning, cache{read,write}}。"""
    if not isinstance(value, dict):
        return ""
    total = _num(value.get("total"))
    if not total:
        cache = value.get("cache") if isinstance(value.get("cache"), dict) else {}
        total = (_num(value.get("input")) + _num(value.get("output"))
                 + _num(value.get("reasoning")) + _num(cache.get("read")) + _num(cache.get("write")))
    return _token_text(total)


def _mimo_snapshot(spec: dict) -> dict:
    """小米 MiMo 桌面版：引擎是 mimocode，会话库 ~/.local/share/mimocode/mimocode.db，
    和 opencode 同族的 session / message / part 三张表（part.type=step-finish 带 token）。"""
    db_path = str(spec.get("db") or "")
    if not db_path or not os.path.exists(db_path):
        return _recency_snapshot(spec, label="Xiaomi MiMo")
    con = None
    try:
        con = _connect_ro(db_path)
        if not _table_columns(con, "session"):
            return _recency_snapshot(spec, label="Xiaomi MiMo")
        row = con.execute(
            "select id, title, directory, time_updated from session order by time_updated desc limit 1"
        ).fetchone()
        if row is None:
            return _recency_snapshot(spec, label="Xiaomi MiMo")
        sid = str(row["id"] or "")
        title = str(row["title"] or "")
        cwd = str(row["directory"] or "")
        last_ts = _ms_seconds(row["time_updated"]) or _now()

        last_role = ""
        last_user_ts = 0.0
        last_user_ms = 0.0
        last_error = False
        for item in con.execute(
            "select data, time_created from message where session_id = ? order by time_created desc limit 30",
            (sid,),
        ):
            data = _json_dict(item["data"])
            if not data:
                continue
            role = str(data.get("role") or "").lower()
            if role not in ("user", "assistant"):
                continue
            stamp = _ms_seconds(item["time_created"])
            if not last_role:
                last_role = role
                last_ts = stamp or last_ts
            if role == "user" and not last_user_ts:
                last_user_ts = stamp
                last_user_ms = _num(item["time_created"])
            if role == "assistant" and isinstance(data.get("error"), dict):
                last_error = True
            if last_role and last_user_ts:
                break

        pending_tool = ""
        say = ""
        tokens = ""
        tools = 0
        for item in con.execute(
            "select data, time_created from part where session_id = ? order by time_created desc limit 40",
            (sid,),
        ):
            data = _json_dict(item["data"])
            if not data:
                continue
            kind = str(data.get("type") or "")
            stamp = _num(item["time_created"])
            if kind == "tool":
                if last_user_ms and stamp >= last_user_ms:
                    tools += 1
                state = data.get("state") if isinstance(data.get("state"), dict) else {}
                if not pending_tool and str(state.get("status") or "") in ("running", "pending"):
                    pending_tool = str(data.get("tool") or "工具")
            elif kind == "text" and not say and not data.get("synthetic"):
                say = str(data.get("text") or "")
            elif kind == "step-finish" and not tokens:
                tokens = _mimo_token_text(data.get("tokens"))
            if say and tokens and pending_tool:
                break

        state_hint = "working" if pending_tool else ""
        if pending_tool:
            last_role = "tool"
        waiting = (last_role == "assistant" and not state_hint and not last_error
                   and (_now() - last_ts) < IDLE_AFTER_SECONDS)

        return _finish(
            spec,
            title=title,
            say=say,
            activity=("调用工具 · %s" % pending_tool) if pending_tool else "",
            tools=tools,
            tokens=tokens,
            thread=sid,
            cwd=cwd,
            last_user_ts=last_user_ts,
            last_ts=last_ts,
            last_role=last_role or "assistant",
            pending_tool=pending_tool,
            last_error=last_error,
            state_hint=state_hint,
            waiting_for_user=waiting,
            fallback_title="Xiaomi MiMo 会话",
        )
    except Exception:      # noqa: BLE001
        return _recency_snapshot(spec, label="Xiaomi MiMo")
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:      # noqa: BLE001
                pass


# -------------------------------------------------------- Qwen Code CLI

def _qwen_parts_text(parts, want_thought=False):
    texts = []
    for part in parts or []:
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        if bool(part.get("thought")) is bool(want_thought):
            texts.append(text)
    return "\n".join(texts).strip()


def _qwen_snapshot(spec):
    """Qwen Code CLI：~/.qwen/projects/<项目>/chats/<会话>.jsonl。

    记录格式（0.25+）像 Claude Code 那类：user / assistant / tool_result / system(ui_telemetry)，
    助手内容在 message.parts[]（thought: true 是思考），用量在 usageMetadata。
    还没有正文时，退回 runtime.json（这次 CLI 进程的 pid）+ 数据新旧。
    """
    path, mtime = _newest_file(spec.get("transcripts") or [])
    if path:
        title = ""
        say = ""
        tokens = ""
        tools = 0
        last_role = ""
        last_user_ts = 0.0
        last_ts = mtime
        pending_tool = ""
        pending_ts = 0.0
        session_id = ""
        for entry in _load_jsonl(path):
            kind = str(entry.get("type") or "").lower()
            stamp = _iso_seconds(entry.get("timestamp"))
            if stamp:
                last_ts = stamp
            if not session_id:
                session_id = str(entry.get("sessionId") or "")
            message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
            parts = message.get("parts") if isinstance(message.get("parts"), list) else []
            if kind == "user" and str(entry.get("provenance") or "") == "real_user":
                text = _qwen_parts_text(parts)
                if text:
                    if not title:
                        title = text
                    last_user_ts = stamp
                    last_role = "user"
                continue
            if kind == "assistant":
                answer = _qwen_parts_text(parts)
                if answer:
                    say = answer
                    last_role = "assistant"
                calls = [
                    part for part in parts
                    if isinstance(part, dict) and isinstance(part.get("functionCall"), dict)
                ]
                tools += len(calls)
                for part in calls:
                    payload = part.get("functionCall") or {}
                    if part.get("functionResponse") is None:
                        pending_tool = str(payload.get("name") or "工具")[:24]
                        pending_ts = stamp or last_ts
                usage = entry.get("usageMetadata") if isinstance(entry.get("usageMetadata"), dict) else {}
                total = _num(usage.get("totalTokenCount")) or (
                    _num(usage.get("promptTokenCount")) + _num(usage.get("candidatesTokenCount"))
                    + _num(usage.get("thoughtsTokenCount"))
                )
                if total:
                    tokens = _compact(total)
                continue
            if kind == "tool_result":
                last_role = "tool"
                continue
            if kind == "system":
                payload = entry.get("systemPayload") if isinstance(entry.get("systemPayload"), dict) else {}
                event = payload.get("uiEvent") if isinstance(payload.get("uiEvent"), dict) else {}
                name = str(event.get("event.name") or "")
                if "tool_call" in name and event.get("function_name"):
                    pending_tool = str(event["function_name"])[:24]
                    pending_ts = stamp or last_ts
                if name.endswith("api_response"):
                    total = _num(event.get("total_token_count"))
                    if total:
                        tokens = _compact(total)
                continue
        if pending_tool and (last_ts - pending_ts) > PENDING_TOOL_MAX_AGE:
            pending_tool = ""
        if title or say:
            return _finish(
                spec,
                title=title,
                say=say,
                tools=tools,
                tokens=tokens,
                thread=session_id or os.path.splitext(os.path.basename(path))[0],
                last_user_ts=last_user_ts,
                last_ts=last_ts,
                last_role=last_role,
                pending_tool=pending_tool,
                waiting_for_user=(last_role == "assistant" and not pending_tool),
                fallback_title="Qwen Code 会话",
            )

    runtime, _runtime_mtime = _newest_file(spec.get("runtimes") or [])
    alive = False
    if runtime:
        try:
            with open(runtime, encoding="utf-8", errors="replace") as fh:
                info = json.load(fh)
            pid = int(info.get("pid") or 0)
            if pid:
                alive = any(pid == item[0] for item in (_process_snapshot() or []))
        except (OSError, ValueError, TypeError):
            alive = False
    if alive:
        payload = _idle_payload(spec)
        payload.update(
            state="working",
            label=STATE_LABELS["working"],
            title="Qwen Code 会话",
            activity="正在使用 Qwen Code CLI（还没聊起来时只显示忙闲）",
        )
        return payload
    return _recency_snapshot(spec, label="Qwen Code")


# ------------------------------------------------------------------ 忙闲兜底（数据有多新）


def _recency_snapshot(spec: dict, label: str = "") -> dict:
    path, mtime = _newest_file(spec.get("recency") or spec.get("roots") or [])
    if not mtime:
        return _idle_payload(spec)
    age = _now() - mtime
    if age < 20:
        state, activity = "working", "正在处理…"
    elif age < 90:
        state, activity = "thinking", "正在思考下一步"
    elif age < 300:
        state, waiting_activity = "waiting", "等你确认或回复"
        activity = waiting_activity
    else:
        state, activity = "idle", ""
    payload = _idle_payload(spec)
    payload.update(
        state=state,
        label=STATE_LABELS.get(state, ""),
        title=("%s 会话" % (label or str(spec.get("name") or "Agent"))),
        activity=activity,
    )
    return payload


# ------------------------------------------------------------------ 探测（设置页提示用）


def detect(agent_id: str) -> dict:
    """这个 agent 现在能不能读到东西 —— 设置页用它提示"已检测 / 还没记录"。"""
    spec = _spec(agent_id)
    kind = str(spec.get("kind") or "")
    if kind == "codex":
        found = os.path.isdir(os.path.join(HOME, ".codex"))
        return {"found": found, "detail": "~/.codex" if found else ""}

    path, mtime = _newest_file(spec.get("roots") or [])
    if not path and kind in ("openclaw", "hermes", "dsh", "zcode", "workbuddy", "opencode",
                             "kimi", "mimo", "qwen"):
        path, mtime = _newest_file(spec.get("recency") or [])
    if path and mtime:
        age_min = max(0.0, (_now() - mtime) / 60.0)
        detail = "最近更新 %.0f 分钟前" % age_min if age_min >= 1 else "刚刚更新"
        return {"found": True, "detail": detail}
    if _process_running(spec):
        return {"found": True, "detail": "进程在跑（暂时只能显示忙闲）"}
    return {"found": False, "detail": ""}


def detect_all() -> dict:
    return {agent_id: detect(agent_id) for agent_id in _AGENTS}


# ------------------------------------------------------------------ 对外入口


_ADAPTERS = {
    "codex": _codex_snapshot,
    "claude": _claude_snapshot,
    "gemini": _gemini_snapshot,
    "openclaw": _openclaw_snapshot,
    "hermes": _hermes_snapshot,
    "dsh": _dsh_snapshot,
    "zcode": _zcode_snapshot,
    "workbuddy": _workbuddy_snapshot,
    "opencode": _opencode_snapshot,
    "kimi": _kimi_snapshot,
    "mimo": _mimo_snapshot,
    "qwen": _qwen_snapshot,
}


def snapshot(agent_id: str = "codex") -> dict:
    """当前选中 agent 的状态。永远返回能用的结果，读不到就当它空闲。"""
    agent_id = agent_id if agent_id in _AGENTS else "codex"
    spec = _spec(agent_id)
    try:
        payload = _ADAPTERS.get(str(spec.get("kind") or ""), _idle_payload)(spec)
    except Exception as exc:      # noqa: BLE001
        payload = _idle_payload(spec)
        payload["activity"] = "读取失败：%s" % _shorten(str(exc), 40)
    if not isinstance(payload, dict):
        payload = _idle_payload(spec)

    overlay = _bridge_overlay(agent_id)
    if overlay:
        payload.update(overlay)
        payload.pop("_no_record", None)   # 状态桥是明确状态，不再走进程兜底
        if payload.get("state") in STATE_LABELS and not overlay.get("label"):
            payload["label"] = STATE_LABELS[payload["state"]]

    # 进程兜底：**只有完全没读到记录**、进程又开着时才演"正在用"。
    # 适配器已经读出记录并判定空闲（比如 Codex 任务已结束但 App 还开着）时必须尊重记录，
    # 否则会一直显示"执行中"。
    if payload.get("state") == "idle" and payload.get("_no_record") and _process_running(spec, deep=True):
        base = str(spec.get("name") or "Agent")
        payload.update(
            state="working",
            label=STATE_LABELS["working"],
            title=payload.get("title") or ("%s 会话" % base),
            activity="正在使用 %s（只显示忙闲；写状态桥可显示任务详情）" % base,
        )
    payload.pop("_no_record", None)       # 内部标记不外泄
    payload.setdefault("source", _display_name(spec))
    payload.setdefault("label", STATE_LABELS.get(str(payload.get("state") or ""), ""))
    return payload


if __name__ == "__main__":
    import sys

    argv = sys.argv[1:]
    if argv and argv[0] in ("--list", "-l"):
        for item in agent_list():
            print("%-10s %-14s shared=%s" % (item["id"], item["name"], item["shared"]))
        print("")
        for agent_id, info in detect_all().items():
            print("%-10s found=%-5s %s" % (agent_id, info.get("found"), info.get("detail")))
    else:
        print(json.dumps(snapshot(argv[0] if argv else "codex"), ensure_ascii=False, indent=2))
