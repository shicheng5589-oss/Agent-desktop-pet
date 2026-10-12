"""Agent 桌宠 —— 宿主程序。

一个贴在桌面上的透明小窗，里面跑 Live2D 形象：

* 状态来自 Codex 自己的本地记录（只读），见 codex_state.py
* 窗口无边框、置顶、不占任务栏；鼠标只有在形象身上才"抓住"窗口，其他位置点击穿透
* 跟着你在虚拟桌面之间移动，所以切到别的桌面也看得到进度
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import ctypes.wintypes as wt
import functools
import http.server
import html
import json
import math
import os
import queue
import socketserver
import sys
import threading
import time

import settings_store

HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(HERE, "vendor")
WEB = os.path.join(HERE, "web")
POSITION_FILE = os.path.join(HERE, "pet_position.json")
SETTINGS_WINDOW_FILE = os.path.join(HERE, "settings_window.json")   # 设置窗口的大小/位置
PID_FILE = os.path.join(HERE, "pet.pid")
STOP_FILE = os.path.join(HERE, "stop.request")

# 「切到 Agent 窗口」用的匹配规则：先认进程名（原生 exe 能直接找到），
# 再认标题关键词（node 系的 CLI 跑在终端里，窗口属于终端进程，只能靠标题/进程树认）。
AGENT_WINDOW_RULES = {
    "claude": {"procs": ("claude",), "titles": ("claude",)},
    "gemini": {"procs": (), "titles": ("gemini",)},
    "openclaw": {"procs": (), "titles": ("openclaw",)},
    "hermes": {"procs": ("hermes",), "titles": ("hermes",)},
    # 桌面版：进程名能直接对上（Kimi Code / Xiaomi MiMo 这类名字带空格也认，匹配时统一小写去 .exe）
    "dsh": {"procs": ("DeepSeek Harness", "dsh-desktop", "dsh"), "titles": ("deepseek harness", "deepseek")},
    "zcode": {"procs": ("ZCode",), "titles": ("zcode",)},
    "workbuddy": {"procs": ("WorkBuddy",), "titles": ("workbuddy",)},
    "opencode": {"procs": ("opencode-cli", "opencode", "opencodedesktop"), "titles": ("opencode",)},
    "kimi": {"procs": ("Kimi Code", "kimi-code", "kimi"), "titles": ("kimi",)},
    "mimo": {"procs": ("Xiaomi MiMo", "mimo"), "titles": ("mimo", "小米")},
    "qwen": {"procs": ("qwen",), "titles": ("qwen", "千问")},
}

sys.path.insert(0, VENDOR)
sys.path.insert(0, HERE)

WINDOW_W = 400     # 桌宠窗口：固定尺寸，下方放角色、上方放进度卡片（不再改尺寸）
WINDOW_H = 620
PANEL_W = 420
PANEL_H = 620
ICON_SIZE = 48  # 桌面图标大约这么大：只当缩放下限的兜底参照（现在下限固定 30%）
CHAR_BASE_H = 380  # 角色本身的基准高度（不含上方留给进度卡片的空间）
CARD_W = 380   # 进度卡片（CSS 像素）
CARD_H = 250
CARD_COMPACT_W = 300   # 桌宠缩得很小时的紧凑卡片
CARD_COMPACT_H = 108
CARD_RADIUS = 20       # 圆角半径（CSS 像素）
CARD_GAP = 4           # 卡片底部与"发梢/头顶"之间的空隙（CSS 像素，故意留得很小）
CARD_HEAD_TOP_RATIO = 0.18   # 卡片底边往下贴到模型高度的这个比例（压住头顶发丝，但不挡脸）
CARD_COMPACT_BELOW = 0.6   # 桌宠缩放小于这个值就用紧凑卡片
SCALE_MAX_WISH = 1.6       # 放大上限至少给到这里（以前按"不超过半屏"算，屏幕不高时只能到 120%~140%）
SCALE_MIN = 0.30           # 缩小下限（2026-10-11 抬上来的：以前按"桌面图标大小"约 12%，太小没法用）

TOOL_BUTTONS = ("undo", "pencil", "eraser")
TOOL_EVENTS = ("single", "right")   # 三个图标：单击触发功能、右键出对应菜单
# 三个图标在"角色包围盒"里的横向中心位置（实测：把渲染出来的图按像素量出来的比例）。
# 注意：它们挤在角色中间偏左，一共只占 35%~56% 的宽度 —— 绝对不能按"三等分"去分，
# 以前就是这么分的，结果点最左边的撤回会落进中间那一格，提示"铅笔：未配置功能"。
TOOL_MARK_X = (("undo", 0.352), ("pencil", 0.457), ("eraser", 0.564))
TOOL_MARK_Y = 0.775      # 图标中心的纵向位置（相对角色包围盒高度）
# 横向半宽：图标本体大约 ±0.037，这里给到 ±0.046（图标本体 + 一点点容错）。
# 相邻图标中心间距约 0.105，所以两个按钮的命中区中间会留出 ~0.013 的空隙 ——
# 点在图标之间的空白不再算按钮，这就是 "横向防误触" 的关键（旧值 0.085 时
# 两块命中区大面积重叠，整条带子都算"最近的那个按钮"）。
# 注意：这里全是"相对角色包围盒"的比例，所以放大 / 缩小桌宠时命中区会跟着等比缩放，
# 不会因为桌宠变小或变大而失准。
TOOL_MARK_HALF_X = 0.046
# 纵向半高（相对角色包围盒高度），上下分开：误触几乎都发生在图标**上方**那一截空白，
# 所以上方收得紧、下方留一点容错。旧值是上下各 0.075（命中带总高 15%），比图标本身
# 高出快一倍 —— 点在图标上方一截空白也会被算成按到按钮，就是这么来的。
TOOL_MARK_HALF_Y_UP = 0.032
TOOL_MARK_HALF_Y_DOWN = 0.045
TOOL_LABELS = {
    "undo": "↩️ 撤回",
    "pencil": "📝 铅笔",
    "eraser": "🧽 橡皮擦",
}
TOOL_ACTION_LABELS = {
    "none": "无功能",
    "open-settings": "打开设置面板",
    "open-menu": "打开右键菜单",
    "toggle-edit-mode": "切换编辑模式",
    "toggle-position-lock": "锁定 / 解锁位置",
    "reset-default": "恢复默认状态",
    "undo-expression": "撤销上一次表情",
    "clear-card": "清空进度卡片",
    "hide-pet": "隐藏到系统托盘",
    "mute-animations": "暂时静音动画",
    "codex-new-session": "新建 Codex 会话",
    "codex-focus": "切到 Agent 窗口",
    "codex-rename-task": "重命名当前任务（复制新名称）",
    "codex-add-note": "添加备注",
    "codex-interrupt": "打断当前任务",
    "codex-undo-question": "撤回上一步输入",
    "codex-clear-context": "清空当前上下文（新建空白会话）",
    "codex-end-session": "结束当前会话（关闭 Codex 窗口）",
}

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

GWL_STYLE = -16
GWL_WNDPROC = -4
GWL_EXSTYLE = -20
WS_SYSMENU = 0x00080000      # 有它，DefWindowProc 才会在右键时弹出窗口系统菜单
WM_CONTEXTMENU = 0x007B      # 右键消息：网页没消费掉就会冒泡到窗口默认处理
WM_NCRBUTTONUP = 0x00A5      # 非客户区右键：系统菜单的另一条来路
WM_PET_CLOSE_MENU = 0x8001   # 自定义：让桌宠在消息循环里关掉右键菜单（鼠标钩子里不能直接关）
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000

GA_ROOT = 2                      # GetAncestor：取顶层窗口
GW_HWNDFIRST = 0                  # 同一层级最上方窗口
GW_HWNDNEXT = 2                   # 同层级下一个窗口

# Windows 任务栏/开始菜单等 Shell 覆盖层。它们永远高于桌宠；输入必须原样交给它们，
# 不能因为光标坐标仍落在桌宠矩形里就触发摸头/拖动/缩放。
SHELL_OVERLAY_PROCESS_NAMES = {
    "shellexperiencehost.exe",
    "startmenuexperiencehost.exe",
    "shellhost.exe",
    "sihost.exe",
    "searchhost.exe",
    "widgetservice.exe",
    "textinputhost.exe",
    "systemsettings.exe",
    "controlcenter.exe",
    "quicksettings.exe",
}
SHELL_OVERLAY_PROCESS_TOKENS = (
    "shellexperiencehost", "startmenuexperiencehost", "shellhost",
    "controlcenter", "quicksettings",
)

SHELL_OVERLAY_CLASSES = {
    "Shell_TrayWnd",
    "Shell_SecondaryTrayWnd",
    "MSTaskListWClass",
    "MSTaskSwWClass",
    "TrayNotifyWnd",
    "TaskListThumbnailWnd",
    "XamlExplorerHostIslandWindow",
    "Windows.UI.Core.CoreWindow",
    "PopupHost",
}

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010

# 缩放后的"重画补救"：改完窗口尺寸后按这个节奏连着重画，抹掉 Win11 留下的黑边。
# 前几次密一些（黑边基本都在换尺寸那一瞬间冒出来），后面越来越稀
# （有的机器要过一两百毫秒才把新表面准备好），最后一轮再把窗口"抖"1px，
# 逼 DWM 彻底重新合成一次。整套下来 ~1.15 秒，全是重绘，不改功能。
REPAIR_SCHEDULE_MS = (30, 30, 40, 60, 90, 130, 180, 250, 340)
# 上一版的做法：6 次固定 50ms、不抖窗口。放一个标记文件就退回它（见 repair_schedule）
REPAIR_SCHEDULE_LEGACY_MS = (50,) * 6
REPAIR_LEGACY_FLAG = os.path.join(HERE, "黑边修复-用旧版.txt")
REPAIR_KEEP_SECONDS = 3.0  # 这一段内的尺寸变化都算"同一次缩放"，并成一块一起刷


def repair_schedule() -> tuple:
    """当前用哪套补救节奏：默认新版；放了标记文件就退回上一版。"""
    try:
        if os.path.exists(REPAIR_LEGACY_FLAG):
            return REPAIR_SCHEDULE_LEGACY_MS, False
    except OSError:
        pass
    return REPAIR_SCHEDULE_MS, True

user32.GetDpiForWindow.argtypes = [ctypes.c_void_p]
user32.GetDpiForWindow.restype = ctypes.c_uint
user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
user32.GetForegroundWindow.restype = ctypes.c_void_p
user32.GetClassNameW.argtypes = [wt.HWND, ctypes.c_wchar_p, ctypes.c_int]
user32.WindowFromPoint.argtypes = [wt.POINT]
user32.WindowFromPoint.restype = wt.HWND
user32.GetAncestor.argtypes = [wt.HWND, ctypes.c_uint]
user32.GetWindow.argtypes = [wt.HWND, ctypes.c_uint]
user32.GetWindow.restype = wt.HWND
user32.GetTopWindow.argtypes = [wt.HWND]
user32.GetTopWindow.restype = wt.HWND
kernel32.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_uint]
kernel32.OpenProcess.restype = ctypes.c_void_p
kernel32.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint)]
kernel32.QueryFullProcessImageNameW.restype = ctypes.c_int
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(ctypes.c_ulong)]
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetAncestor.restype = wt.HWND
user32.SetCapture.argtypes = [wt.HWND]
user32.SetCapture.restype = wt.HWND
user32.ReleaseCapture.argtypes = []
user32.ReleaseCapture.restype = wt.BOOL
user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
user32.UnhookWindowsHookEx.restype = wt.BOOL

log_lock = threading.Lock()      # 保留给外部（现在写盘由专门的线程串行做）
log_path = os.path.join(HERE, "pet.log")
LOG_MAX_BYTES = 512 * 1024     # 超过 512KB 就只保留最近 300 行
_log_size = 0
_log_queue: "queue.Queue" = queue.Queue()
_log_writer_started = False
verbose_log = False            # 是否记录鼠标事件（排查用；默认关，设置里可开）


def _write_log_line(line: str) -> None:
    global _log_size
    try:
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        _log_size += len(line.encode("utf-8")) + 1
        if _log_size > LOG_MAX_BYTES:
            with open(log_path, encoding="utf-8") as fh:
                tail = fh.readlines()[-300:]
            with open(log_path, "w", encoding="utf-8") as fh:
                fh.writelines(tail)
            _log_size = sum(len(t.encode("utf-8")) for t in tail)
    except OSError:
        pass


def _log_writer() -> None:
    while True:
        line = _log_queue.get()
        if line is None:
            return
        _write_log_line(line)


def log(message: str, force: bool = False) -> None:
    """写日志：只把这一行丢进队列，落盘交给专门的写线程。

    界面线程（鼠标钩子就在它上面）**绝不**因为磁盘 / 杀毒软件卡住而停顿——
    上一轮"未响应"的现场里，看门狗自己就卡在 log() 的锁上排队。
    """
    if not force and not verbose_log:
        # 非强制日志里，只有鼠标事件会被跳过（它最频繁，也最没用）
        if message.startswith("鼠标事件"):
            return
    line = "%s  %s" % (time.strftime("%H:%M:%S"), message)
    global _log_writer_started
    if not _log_writer_started:
        _log_writer_started = True
        threading.Thread(target=_log_writer, name="pet-log", daemon=True).start()
    _log_queue.put(line)
    try:
        print(line, flush=True)
    except Exception:  # noqa: BLE001
        pass


def install_stack_dumps() -> None:
    """每 15 秒把所有线程的调用栈写进 pet-stacks.log（诊断"未响应"用）。

    纯 Python 实现（sys._current_frames + traceback）：只逐线程取 Python 层的栈，
    不碰解释器内部结构。上一版用的 faulthandler 全线程转储（C 线程）在特定时序下
    会踩空指针、把整个进程带崩（2026-10-11 排查：6 个崩溃转储全部死在
    _Py_DumpTracebackThreads 里），所以换成这个不会崩的实现。
    代价：解释器彻底卡到跑不了 Python 代码时写不出快照（那种场面交给鼠标看门狗兜底）。
    每次启动重写（只保留本次运行），想关掉就设环境变量 CODEXPET_NO_STACKDUMP=1。
    """
    if os.environ.get("CODEXPET_NO_STACKDUMP"):
        return
    try:
        import traceback

        handle = open(os.path.join(HERE, "pet-stacks.log"), "w", encoding="utf-8", buffering=1)

        def dump_once() -> None:
            names = {t.ident: t.name for t in threading.enumerate()}
            frames = sys._current_frames()
            handle.write(
                "===== %s  线程栈快照（%d 个线程）=====\n"
                % (time.strftime("%H:%M:%S"), len(frames))
            )
            for tid, frame in frames.items():
                handle.write(
                    "\nThread 0x%08x (%s) (most recent call first):\n"
                    % (tid, names.get(tid, "?"))
                )
                for line in traceback.format_stack(frame):
                    handle.write(line)
            handle.flush()

        def loop() -> None:
            while True:
                time.sleep(15.0)
                try:
                    dump_once()
                except Exception:  # noqa: BLE001
                    pass

        threading.Thread(target=loop, name="pet-stacks", daemon=True).start()
        log("线程栈快照已开启：每 15 秒写一份 pet-stacks.log（只在排查卡死时有用）")
    except Exception as exc:  # noqa: BLE001
        log("开启线程栈快照失败（不影响使用）：%r" % (exc,))


def install_exception_logging() -> None:
    """pythonw 没有控制台，未捕获异常会凭空消失 —— 全部写进 pet.log。"""
    import traceback

    def handler(exc_type, exc_value, exc_tb):
        log("未捕获异常：%s" % "".join(traceback.format_exception(exc_type, exc_value, exc_tb)))

    sys.excepthook = handler
    if hasattr(threading, "excepthook"):

        def thread_handler(args):
            handler(args.exc_type, args.exc_value, args.exc_traceback)

        threading.excepthook = thread_handler


def load_saved_position():
    try:
        with open(POSITION_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return int(data["x"]), int(data["y"])
    except Exception:
        return None


def save_position(x: int, y: int) -> None:
    try:
        with open(POSITION_FILE, "w", encoding="utf-8") as fh:
            json.dump({"x": int(x), "y": int(y)}, fh)
    except OSError:
        pass


def load_settings_window_rect():
    """设置窗口上次摆好的矩形 (x, y, 宽, 高)；没存过或者存坏了返回 None。"""
    try:
        with open(SETTINGS_WINDOW_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return int(data["x"]), int(data["y"]), int(data["width"]), int(data["height"])
    except Exception:      # noqa: BLE001
        return None


def save_settings_window_rect(x, y, width, height) -> None:
    try:
        with open(SETTINGS_WINDOW_FILE, "w", encoding="utf-8") as fh:
            json.dump(
                {"x": int(x), "y": int(y), "width": int(width), "height": int(height)},
                fh,
            )
    except OSError:
        pass


def fit_window_rect(area, x, y, width, height, min_w: int = 520, min_h: int = 520):
    """把窗口矩形夹进工作区：宽高不超工作区、不小于窗口最小尺寸，位置保证整块可见。"""
    width = max(min_w, min(int(width), max(min_w, area.Width - 8)))
    height = max(min_h, min(int(height), max(min_h, area.Height - 8)))
    x = max(area.Left + 4, min(int(x), area.Right - width - 4))
    y = max(area.Top + 4, min(int(y), area.Bottom - height - 4))
    return x, y, width, height


def startup_link_path() -> str:
    return os.path.join(
        os.environ.get("APPDATA", ""),
        "Microsoft",
        "Windows",
        "Start Menu",
        "Programs",
        "Startup",
        "CodexPet.lnk",
    )


def desktop_dir() -> str:
    """当前用户的桌面目录（处理 OneDrive/重定向；测试可用 CODEXPET_DESKTOP 覆盖）。"""
    override = os.environ.get("CODEXPET_DESKTOP")
    if override and os.path.isdir(override):
        return override
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        )
        try:
            value, _ = winreg.QueryValueEx(key, "Desktop")
        finally:
            key.Close()
        path = os.path.expandvars(str(value or ""))
        if path:
            return path
    except Exception:      # noqa: BLE001
        pass
    return os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")), "Desktop")


def desktop_link_path() -> str:
    return os.path.join(desktop_dir(), "Agent 桌宠.lnk")


def desktop_link_exists() -> bool:
    try:
        return os.path.exists(desktop_link_path())
    except Exception:      # noqa: BLE001
        return False


def compute_card_position(pet, card_size, area, gap: int = CARD_GAP, head_offset: int = 0):
    """卡片位置：始终在**模型头顶正上方居中**；上面放不下就翻到桌宠下方。

    参数都是 (left, top, width, height) / 屏幕 (left, top, right, bottom)，
    head_offset = 模型实际可见顶部相对窗口顶部的距离（窗口比模型大，直接贴窗口会显得悬空）。
    返回卡片左上角 (x, y)。抽成纯函数是为了能单独验证"贴头、不重叠、不跑侧面"。
    """
    pet_left, pet_top, pet_w, pet_h = pet
    card_w, card_h = card_size
    area_left, area_top, area_right, area_bottom = area

    center = pet_left + pet_w // 2
    x = int(center - card_w / 2)
    x = max(area_left + 8, min(x, area_right - card_w - 8))

    y = pet_top + head_offset - card_h - gap   # 贴着模型头顶
    if y < area_top + 8:
        y = pet_top + pet_h + gap              # 顶上没空间 -> 放到脚下方，也不去侧面
    y = max(area_top + 8, min(y, area_bottom - card_h - 8))
    return x, y


def card_head_offset(char_rect) -> int:
    """卡片底边相对桌宠窗口顶部应该落在哪（物理像素）。

    模型外框顶部 + 18% 模型高度 —— 也就是允许卡片压住头顶那圈发丝/特效，
    但停在脸部之上。想再高/再低，就调 CARD_HEAD_TOP_RATIO。
    """
    if not char_rect:
        return 0
    _, top, _, height = char_rect
    return int(top + height * CARD_HEAD_TOP_RATIO)


class MouseTracker:
    """把鼠标按键状态翻译成动作。纯逻辑、不碰窗口，方便单独测。

    规则（按需求）：
      * 左键单击      -> pat（点在头部区域）或 poke（点在身体其他位置）
      * 左键双击      -> cycle（换一个表情）
      * 左键按住拖动  -> drag（超过阈值才算，避免和单击打架）
      * 右键单击      -> menu
      * 右键按住拖动  -> drag（并且不弹菜单）
    """

    DRAG_THRESHOLD = 5
    CLICK_MAX_SECONDS = 0.7
    DOUBLE_CLICK_SECONDS = 0.35
    SINGLE_CLICK_DELAY = 0.35

    def __init__(self) -> None:
        self.left_down = False
        self.right_down = False
        self.press_button = ""
        self.press_at = 0.0
        self.press_pos = (0, 0)
        self.press_on_char = False
        self.press_on_head = False
        self.moved = 0.0
        self.dragging = False
        self.last_left_click = 0.0
        self.pending_single = 0.0
        self.pending_head = False

    def feed(self, x: float, y: float, left: bool, right: bool, on_char: bool, on_head: bool, now: float) -> list[str]:
        events: list[str] = []

        # 先算"按下之后移动了多远"：必须在松开判定之前算好，
        # 否则"按下→松开"中间没有别的 poll 时，松开那一下看到的是 0。
        if self.press_button:
            self.moved = max(self.moved, abs(x - self.press_pos[0]) + abs(y - self.press_pos[1]))
            # 必须在处理 release 之前把 drag-start 判出来。否则一次“按下→移动→松开”
            # 全发生在两次轮询之间时，release 看到 dragging=False，会把快速拖动误判成单击。
            if self.press_on_char and not self.dragging and self.moved > self.DRAG_THRESHOLD:
                self.dragging = True
                events.append("drag-start")

        if left and not self.left_down:
            self._press("left", x, y, on_char, on_head, now)
        elif not left and self.left_down:
            self._release(events, now, on_char)

        if right and not self.right_down:
            self._press("right", x, y, on_char, on_head, now)
        elif not right and self.right_down:
            self._release(events, now, on_char)

        events += self.tick(now)
        return events

    def tick(self, now: float) -> list[str]:
        """定时器每帧调用：把"等第二下"的单击判决定下来（否则单击永远不会触发）。"""
        events: list[str] = []
        if self.pending_single and now - self.pending_single > self.SINGLE_CLICK_DELAY:
            self.pending_single = 0.0
            events.append("pat" if self.pending_head else "poke")
        return events

    def _press(self, button: str, x: float, y: float, on_char: bool, on_head: bool, now: float) -> None:
        if button == "left":
            self.left_down = True
        else:
            self.right_down = True
        self.press_button = button
        self.press_at = now
        self.press_pos = (x, y)
        self.press_on_char = on_char
        self.press_on_head = on_head
        self.moved = 0.0
        self.dragging = False

    def _release(self, events: list[str], now: float, on_char_now: bool = True) -> None:
        """松开：一次点击要求"按下和松开都压在角色像素上"。

        只判按下的话，在角色边缘按下、手指挪到旁边的透明处再松手，就变成
        "桌宠弹菜单 + 穿透出去给桌面弹菜单"同时发生（截图里那种两个菜单）。
        """
        button = self.press_button
        dragged = self.dragging
        quick = (now - self.press_at) < self.CLICK_MAX_SECONDS
        # 判定规则（按下/松开两个时刻的像素结论一起看）：
        #  * 原地点击（几乎没移动）——任一时刻判在角色上都算：按下那一瞬间的结论可能比
        #    网页采样慢半拍，只认它的话就会出现"右键明明点在角色上却没反应"；
        #  * 有位移（拖动级别）——要求按下和松开都在角色上，免得从别处拖进来松手也算点击。
        if self.moved <= self.DRAG_THRESHOLD:
            on_char = on_char_now or self.press_on_char
        else:
            on_char = on_char_now and self.press_on_char
        on_head = self.press_on_head

        self.dragging = False
        self.press_button = ""
        if button == "left":
            self.left_down = False
        else:
            self.right_down = False

        if dragged:
            events.append("drag-end")
            return
        if not on_char or not quick:
            return

        if button == "right":
            events.append("menu")
        elif self.pending_single and (now - self.last_left_click) < self.DOUBLE_CLICK_SECONDS:
            self.pending_single = 0.0
            self.last_left_click = 0.0
            events.append("cycle")
        else:
            self.last_left_click = now
            self.pending_single = now
            self.pending_head = on_head


def set_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor v2
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def init_sta() -> bool:
    """WebView2 needs a single-threaded apartment; threads start out as MTA."""
    hr = ctypes.windll.ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
    if hr in (0, 1):  # S_OK / S_FALSE
        return True
    log("COM 初始化失败 0x%08X" % (hr & 0xFFFFFFFF))
    return False


_singleton_handle = None


def single_instance() -> bool:
    """二开一次就够：桌宠已经在跑时，再启动直接退出。"""
    global _singleton_handle
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    _singleton_handle = kernel32.CreateMutexW(None, False, "CodexPet.SingleInstance")
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


def desktop_name(handle) -> str:
    buf = ctypes.create_unicode_buffer(256)
    needed = ctypes.c_uint(0)
    if user32.GetUserObjectInformationW(ctypes.c_void_p(handle), 2, buf, 512, ctypes.byref(needed)):
        return buf.value
    return "<err %d>" % ctypes.get_last_error()


def hop_to_interactive_desktop() -> bool:
    """If we were launched inside a sandbox/other desktop, move this thread to the real one.

    A thread's desktop decides where its windows appear; threads created afterwards inherit
    the *process* desktop, so this has to run on every thread that creates windows.
    """
    user32.OpenInputDesktop.restype = ctypes.c_void_p
    user32.GetThreadDesktop.restype = ctypes.c_void_p
    current = user32.GetThreadDesktop(kernel32.GetCurrentThreadId())
    if desktop_name(current).lower() == "default":
        return True
    for access in (0x000F01FF, 0x00000100 | 0x00000002 | 0x00000001):
        handle = user32.OpenInputDesktop(0, False, access)
        if handle and user32.SetThreadDesktop(ctypes.c_void_p(handle)):
            log("窗口线程已切换到桌面 %s" % desktop_name(handle))
            return True
    log("无法切换到交互桌面，窗口可能不可见（err=%d）" % ctypes.get_last_error())
    return False


# ---------------------------------------------------------------- virtual desktops


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    def __eq__(self, other):
        return bytes(self) == bytes(other)

    def is_null(self) -> bool:
        return bytes(self) == b"\x00" * 16


def guid_from_string(text: str) -> GUID:
    guid = GUID()
    ctypes.windll.ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(guid))
    return guid


class VirtualDesktops:
    """Documented IVirtualDesktopManager, plus the foreground-window trick for "current"."""

    CLSID = "{AA509086-5CA9-4C25-8F95-589D3C07B48A}"
    IID = "{A5CD92FF-29BE-454C-8D04-D82879FB3F1B}"

    def __init__(self) -> None:
        self.ok = False
        self.ptr = ctypes.c_void_p()
        try:
            clsid = guid_from_string(self.CLSID)
            iid = guid_from_string(self.IID)
            hr = ctypes.windll.ole32.CoCreateInstance(
                ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(self.ptr)
            )
            if hr != 0 or not self.ptr:
                raise OSError("CoCreateInstance failed: 0x%08X" % (hr & 0xFFFFFFFF))
            vtable = ctypes.cast(self.ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
            self._is_on_current = ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p, wt.HWND, ctypes.POINTER(wt.BOOL)
            )(vtable[3])
            self._desktop_id = ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p, wt.HWND, ctypes.POINTER(GUID)
            )(vtable[4])
            self._move = ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p, wt.HWND, ctypes.POINTER(GUID)
            )(vtable[5])
            self.ok = True
        except Exception as exc:  # noqa: BLE001
            log("虚拟桌面接口不可用：%r" % (exc,))

    def desktop_of(self, hwnd) -> GUID | None:
        if not self.ok or not hwnd:
            return None
        guid = GUID()
        if self._desktop_id(self.ptr, wt.HWND(hwnd), ctypes.byref(guid)) == 0 and not guid.is_null():
            return guid
        return None

    def move(self, hwnd, guid: GUID) -> bool:
        if not self.ok or not hwnd:
            return False
        return self._move(self.ptr, wt.HWND(hwnd), ctypes.byref(guid)) == 0

    def guid_text(self, guid: GUID | None) -> str:
        if not guid:
            return "?"
        raw = bytes(guid)
        return (
            "%08x-%04x-%04x-%02x%02x-%02x%02x%02x%02x%02x%02x"
            % (
                int.from_bytes(raw[0:4], "little"),
                int.from_bytes(raw[4:6], "little"),
                int.from_bytes(raw[6:8], "little"),
                raw[8], raw[9], raw[10], raw[11], raw[12], raw[13], raw[14], raw[15],
            )
        )

    def follow(self, hwnd) -> None:
        """Keep the pet on whatever desktop the user is looking at right now."""
        if not self.ok or not hwnd:
            return
        foreground = user32.GetForegroundWindow()
        if not foreground or int(foreground) == int(hwnd):
            return
        here = self.desktop_of(hwnd)
        there = self.desktop_of(foreground)
        if here and there and here != there:
            if self.move(hwnd, there):
                log("跟随虚拟桌面，移动到当前桌面")
            elif not getattr(self, "_move_warned", False):
                self._move_warned = True
                log("跟随虚拟桌面失败（可能被系统策略挡住），桌宠会留在原桌面")

    def align_to(self, target_hwnd, source_hwnd) -> bool:
        """把 target 窗口搬到 source 窗口所在的虚拟桌面。

        用于"新开的设置窗口/卡片窗口"：它们默认落在进程启动时那个桌面，
        用户切过桌面后就看不见了。桌宠本身一直跟着用户，所以以它为准最可靠。
        """
        if not self.ok or not target_hwnd or not source_hwnd:
            return False
        there = self.desktop_of(source_hwnd)
        here = self.desktop_of(target_hwnd)
        if there and here and there != here:
            if self.move(target_hwnd, there):
                log("已把新窗口搬到桌宠所在的虚拟桌面")
                return True
        return False


# ------------------------------------------------------------------------ server


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):  # noqa: D102
        if os.environ.get("CODEXPET_HTTP_LOG"):
            log("HTTP %s" % (fmt % args))

    def log_error(self, fmt, *args):
        if os.environ.get("CODEXPET_HTTP_LOG"):
            log("HTTP-ERR %s" % (fmt % args))


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def start_server() -> int:
    handler = functools.partial(QuietHandler, directory=WEB)
    httpd = Server(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    log("模型与界面服务已启动：http://127.0.0.1:%d/" % port)
    return port


# ---------------------------------------------------------------------- host app


class PetHost:
    def __init__(self, port: int, selftest: float, shot: str = "") -> None:
        import clr

        clr.AddReference(os.path.join(VENDOR, "webview", "lib", "Microsoft.Web.WebView2.Core.dll"))
        clr.AddReference(os.path.join(VENDOR, "webview", "lib", "Microsoft.Web.WebView2.WinForms.dll"))
        clr.AddReference("System.Drawing")
        clr.AddReference("System.Windows.Forms")

        loader_dir = os.path.join(VENDOR, "webview", "lib", "runtimes", "win-x64", "native")
        if os.path.isdir(loader_dir):
            os.environ["Path"] = os.environ.get("Path", "") + ";" + loader_dir

        self.clr = clr
        self.port = port
        self.selftest = selftest
        self.shot = shot
        self.queue: list[dict] = []
        self.lock = threading.Lock()
        self.panel_open = False
        self.settings = settings_store.load()
        self.webview = None
        self.settings_window = None
        self.settings_webview = None
        self._aux_hwnds = set()          # 已注册的设置/卡片等辅助顶层窗口（钩子输入隔离）
        self._expressions = None
        self._motions = None
        self._props = None
        self.ready = threading.Event()
        self.status_ready = threading.Event()
        self.status = {}
        self.snapshot_ready = threading.Event()
        self.snapshot = ""
        self.desktops = None
        self.last_follow = 0.0
        self.reload_attempts = 0
        self.last_cursor = None
        self.last_cursor_send = 0.0
        self.last_stop_check = 0.0
        self.char_rect = None
        self.head_rect = None
        self.web_dpr = None           # 网页自己报的 devicePixelRatio（换算光标坐标要用它，别用猜的）
        self.over_char = None
        self.alpha_probe_ok = False
        self.zoom_lock_until = 0.0
        self.resize_anchor = "bottom"
        self.pending_wheel = 0
        self.wheel_at = 0.0
        self._wheel_hook = None
        self._hook_stalled = False      # 看门狗用：界面线程卡住时先把钩子摘掉
        self._hook_watchdog = False     # 看门狗只起一次
        self._ui_heartbeat = time.time()  # 界面线程心跳（每个 tick 刷新）
        self._ui_phase = "启动"            # 界面线程"最后在做什么"，看门狗报警时写进日志
        self._settings_signature = None  # 上次发出去的设置内容指纹（内容没变就不重发）
        self._settings_pushed_at = 0.0
        self._fps_protect_until = 0.0    # 卡顿保护：这段时间内把渲染帧率压到 60
        self._stall_started = 0.0
        self._settings_outbox = None    # 待发给设置窗口的最新一条消息（合并用）
        self._settings_posting = False
        self._settings_post_started = 0.0
        self._settings_post_warned = False
        self._flush_started = 0.0       # 正在给桌宠页面发的这条消息什么时候开始的
        self._flush_warned = False
        self.agent_status_cache = {}    # 各 agent 的本地记录检测结果（后台线程算，界面只读）
        self.agent_status_at = 0.0
        self.agent_status_busy = False
        self._cache_size_bytes = 0        # WebView2 缓存占用（后台线程量，设置页只读）
        self._cache_size_at = 0.0
        self._cache_size_busy = False
        self._cache_size_ready = False
        self.form_input_works = False
        self.card = None
        self.card_webview = None
        # 进度卡片开着没：按上次关掉桌宠时的状态来（settings_store 的 cardOpen，默认收起）
        self.inline_card_open = bool((self.settings or {}).get("cardOpen", False))
        self.card_want_visible = False
        self.last_payload = None
        self.last_card_pos = None
        self.manual_drag = None
        self.manual_drag_started = 0.0
        self.manual_drag_moves = 0
        self.drag_anchor = None
        self.drag_button = "left"
        self.drag_started_at = 0.0
        self.drag_moves = 0
        self.drag_timer = None
        self.cursor_timer = None
        self.repair_timer = None
        self.repair_left = []
        self.repair_total = 0
        self.repair_nudge = True
        self.repair_rect = None       # 最近这次缩放涉及的屏幕区域（旧+新），用来让桌面重画
        self.repair_rect_at = 0.0
        self.mouse = MouseTracker()
        self.drag_stop = threading.Event()
        self.drag_monitor_stop = threading.Event()
        self.drag_samples = 0
        self.drag_changes = 0
        self.drag_max_gap_ms = 0.0
        self.drag_stop.set()
        self.drag_thread = None
        self.drag_started_at = 0.0
        self.drag_distance = 0.0
        self.render_fps = 0.0
        self.fps_log_at = 0.0
        self.last_drag_log = 0.0
        self.menu = None
        self.agent_picker = None        # 右键菜单「切换卡片 Agent」弹的快速选择面板（见 open_agent_picker）
        self.tray_icon = None
        self.edit_mode = False
        self.position_locked = False
        self.last_bottom_action = None
        # 系统菜单屏蔽用：ctypes 的窗口过程回调必须留引用，被 GC 掉会直接崩
        self._wndproc = None
        self._old_wndproc = None
        self._press_captured = False    # 拖动期间是否抓着鼠标（见 start_drag_loop/finish_drag）
        self._hwnd_int = None           # 窗口句柄缓存：鼠标钩子里不能去碰 CLR 对象
        self._rect_cache = None         # 窗口矩形缓存：钩子里算"光标在不在窗口里"用
        self._right_swallowed = False   # 这一次右键是不是已经被我们吃掉了
        self._left_swallowed = False    # 左键同理：按下/抬起都在钩子里成对吃掉
        self._left_swallow_log = False  # 吃掉左键的诊断标记，回到 tick 里再写日志
        self._ls_down_at = None         # 被吃掉的这次左键：按下位置（诊断/兜底用）
        self._ls_down_time = 0.0        # 被吃掉的这次左键：按下时刻
        self._hook_mouse_steps = []     # 钩子记录的左键 DOWN/UP 脉冲，tick 里按原顺序入状态机
        self._swallow_log = False       # 吃掉之后留个标记，回到 tick 里再写日志
        self._rs_down_at = None         # 被吃掉的这次右键：按下时的光标位置
        self._rs_down_time = 0.0        # 同上：按下时刻（超时兜底用）
        self._rs_peak = 0               # 被吃掉的这次右键：按住期间移动了多少
        self._pending_menu_at = None    # 待弹出菜单的位置（钩子决定，tick 里执行）
        self._menu_shown_at = 0.0       # 上一次弹菜单的时间（防止两条路径各弹一次）
        self._menu_visible = False      # 菜单是不是开着（钩子里只读这个缓存，不碰 CLR）
        self._menu_rect = None          # 菜单的屏幕矩形：用来判断左键点在菜单里还是外面
        self._menu_pos = None           # 菜单左上角（屏幕坐标）：快速切换面板就弹在同一个位置
        self._menu_close_at = 0.0       # "点了菜单外面"的兜底关闭请求（带时间戳，1 秒后作废）
        self._menu_ghost_since = None   # 菜单"丢了鼠标捕获"从什么时候开始（幽灵菜单自检用）
        self._menu_close_reason = ""    # 菜单是为什么关的（诊断用）
        self._ignore_click_until = 0.0  # 这一下点击属于菜单/下层窗口，别让底下的图标按钮跟着响应

    # -- window construction -------------------------------------------------

    def build(self):
        from System.Drawing import Color, Size, Point
        from System.Windows.Forms import (
            Application,
            DockStyle,
            Form,
            FormBorderStyle,
            FormStartPosition,
            Screen,
            Timer,
        )
        from Microsoft.Web.WebView2.WinForms import CoreWebView2CreationProperties, WebView2

        self.Color = Color
        self.Size = Size
        self.Point = Point

        form = Form()
        form.Text = "Agent 桌宠"
        form.FormBorderStyle = getattr(FormBorderStyle, "None")
        form.StartPosition = FormStartPosition.Manual
        form.ShowInTaskbar = False
        form.TopMost = bool(self.settings.get("alwaysOnTop", True))
        form.BackColor = Color.Magenta
        form.TransparencyKey = Color.Magenta
        form.MinimizeBox = False
        form.MaximizeBox = False
        form.AllowTransparency = True

        area = Screen.PrimaryScreen.WorkingArea
        dpi = user32.GetDpiForWindow(ctypes.c_void_p(int(form.Handle.ToInt64())))
        self.dpi_scale = (dpi or 96) / 96.0
        self.pet_scale = max(SCALE_MIN, float(self.settings.get("scale", 1.0)))
        physical_w = int(round(WINDOW_W * self.dpi_scale * self.pet_scale))
        physical_h = int(round(WINDOW_H * self.dpi_scale * self.pet_scale))
        form.ClientSize = Size(physical_w, physical_h)
        form.Location = Point(area.Right - physical_w - 24, area.Bottom - physical_h - 8)
        form.Opacity = float(self.settings.get("opacity", 1.0))
        saved = load_saved_position()
        if saved:
            virtual = Screen.GetWorkingArea(form)
            if virtual.Left - 40 <= saved[0] <= virtual.Right - 40 and virtual.Top <= saved[1] <= virtual.Bottom - 40:
                form.Location = Point(saved[0], saved[1])
                log("窗口位置已恢复：%s" % (saved,))
        log("窗口 %dx%d（DPI %d，桌宠缩放 %.2f）" % (physical_w, physical_h, dpi, self.pet_scale))
        self.form = form

        webview = WebView2()
        props = self.create_props()
        webview.CreationProperties = props
        webview.Dock = DockStyle.Fill
        try:
            webview.DefaultBackgroundColor = Color.Transparent
        except Exception as exc:  # noqa: BLE001
            log("设置透明背景失败：%r" % (exc,))
        form.Controls.Add(webview)
        self.webview = webview

        webview.CoreWebView2InitializationCompleted += self.on_webview_ready
        webview.WebMessageReceived += self.on_web_message
        form.Shown += self.on_shown
        form.MouseWheel += self.on_mouse_wheel
        self.attach_mouse_events(form, webview)
        webview.EnsureCoreWebView2Async(None)

        self.timer = Timer()
        self.timer.Interval = 110
        self.timer.Tick += self.on_tick
        self.timer.Start()

        # 视线专用定时器：33ms（~30Hz）。
        # 眼睛的输入就是鼠标位置，110ms 才送一次的话，眼珠只能"一格一格"地追，
        # 再怎么平滑也还是会顿；这里单独跑快一点，送完立刻发出去。
        self.cursor_timer = Timer()
        self.cursor_timer.Interval = 33
        self.cursor_timer.Tick += self.on_cursor_tick
        self.cursor_timer.Start()

        # 拖动专用定时器：15ms 一次，全程在 UI 线程
        self.drag_timer = Timer()
        self.drag_timer.Interval = 15
        self.drag_timer.Tick += self.on_drag_timer

        # 缩放补救定时器：改完窗口尺寸后连着重画几次（见 force_repaint）
        self.repair_timer = Timer()
        self.repair_timer.Interval = REPAIR_SCHEDULE_MS[0]   # 真正的间隔每轮开刷前会按表重设
        self.repair_timer.Tick += self.on_repair_timer

        return form

    def attach_mouse_events(self, form, webview) -> None:
        """分层窗口里，不透明像素上的点击会送到子控件 —— 用系统自己的命中判断最准。

        同时挂到窗体和 WebView 控件上（不同像素可能落在不同 HWND），
        这样右键点在角色身上不会被桌面抢走，点在透明处才会穿透。
        """
        for control in (form, webview):
            control.MouseDown += self.on_ui_mouse_down
            control.MouseMove += self.on_ui_mouse_move
            control.MouseUp += self.on_ui_mouse_up
            control.MouseWheel += self.on_mouse_wheel

    def cursor_screen(self):
        cursor = wt.POINT()
        user32.GetCursorPos(ctypes.byref(cursor))
        return cursor.x, cursor.y

    def feed_tracker(self, x: float, y: float, left: bool, right: bool, on_char: bool) -> None:
        local_x = x - self.form.Left
        local_y = y - self.form.Top
        on_head = on_char and self.on_head(local_x, local_y)
        for event in self.mouse.feed(x, y, left, right, on_char, on_head, time.time()):
            self.on_mouse_event(event, wt.POINT(int(x), int(y)), time.time())

    def on_ui_mouse_down(self, sender, args) -> None:
        from System.Windows.Forms import MouseButtons

        self.form_input_works = True
        x, y = self.cursor_screen()
        left = args.Button == MouseButtons.Left
        right = args.Button == MouseButtons.Right
        try:
            sender.Capture = True     # 捕获鼠标：拖出窗口也还能收到移动，拖动更顺
        except Exception:
            pass
        self.feed_tracker(x, y, left or self.mouse.left_down, right or self.mouse.right_down, True)

    def on_ui_mouse_move(self, sender, args) -> None:
        if not (self.mouse.left_down or self.mouse.right_down):
            return
        x, y = self.cursor_screen()
        self.feed_tracker(x, y, self.mouse.left_down, self.mouse.right_down, True)

    def on_ui_mouse_up(self, sender, args) -> None:
        from System.Windows.Forms import MouseButtons

        x, y = self.cursor_screen()
        left = args.Button == MouseButtons.Left
        right = args.Button == MouseButtons.Right
        try:
            sender.Capture = False
        except Exception:
            pass
        self.feed_tracker(
            x,
            y,
            self.mouse.left_down and not left,
            self.mouse.right_down and not right,
            True,
        )

    def create_props(self):
        """两个 WebView2 必须用完全相同的用户数据文件夹和启动参数，否则第二个初始化会失败。"""
        from Microsoft.Web.WebView2.WinForms import CoreWebView2CreationProperties

        props = CoreWebView2CreationProperties()
        props.UserDataFolder = os.path.join(HERE, ".webview2")
        props.AdditionalBrowserArguments = (
            "--disable-features=ElasticOverscroll,CalculateNativeWinOcclusion"
            " --disable-background-timer-throttling"
            " --disable-backgrounding-occluded-windows"
            " --disable-renderer-backgrounding"
            " --autoplay-policy=no-user-gesture-required"
        )
        return props

    def on_shown(self, sender, args) -> None:
        self.install_wheel_hook()
        self.suppress_system_menu()
        self.desktops = VirtualDesktops()
        self.desktops.follow(self.form.Handle.ToInt64())

    # -- 屏蔽窗口系统菜单 ----------------------------------------------------

    def suppress_system_menu(self) -> None:
        """右键不再弹出 Windows 的窗口系统菜单（还原 / 移动 / 大小 / 关闭）。

        网页那边把右键默认行为拦掉之后，没被消费的 WM_CONTEXTMENU 会冒泡到宿主窗口的
        默认处理；无边框窗口只要还带着 WS_SYSMENU，DefWindowProc 就照着"在标题栏上
        点了右键"的老规矩把系统菜单弹出来。两道保险：去掉 WS_SYSMENU + 在窗口过程里
        直接吃掉右键相关消息。

        （窗口过程回调是 ctypes 直接交给系统的，必须存成成员变量 —— 被 Python 回收掉
        之后系统再回调就会崩；换 Handle 之后要重新装，所以放在 on_shown 里。）
        """
        try:
            hwnd = wt.HWND(self.form.Handle.ToInt64())
            style = user32.GetWindowLongW(hwnd, GWL_STYLE) & 0xFFFFFFFF
            if style & WS_SYSMENU:
                user32.SetWindowLongW(hwnd, GWL_STYLE, style & ~WS_SYSMENU)

            LRESULT = ctypes.c_ssize_t
            WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, ctypes.c_uint, wt.WPARAM, wt.LPARAM)
            user32.SetWindowLongPtrW.restype = ctypes.c_void_p
            user32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_void_p]
            user32.CallWindowProcW.restype = LRESULT
            user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wt.HWND, ctypes.c_uint, wt.WPARAM, wt.LPARAM]

            def wnd_proc(h, msg, wparam, lparam):
                if msg in (WM_CONTEXTMENU, WM_NCRBUTTONUP):
                    return 0
                if msg == WM_PET_CLOSE_MENU:
                    # 鼠标钩子发现"左键点在菜单外面"时发过来的：
                    # 在这里关菜单（消息循环里，随便调 .NET 都安全；钩子里不能这么干）。
                    # 只认 1 秒内的请求：万一这条消息晚到（下一次菜单已经弹出），也不能误关。
                    if self._menu_close_at and time.time() - self._menu_close_at < 1.0:
                        self._menu_close_at = 0.0
                        try:
                            if self.menu is not None and not self.menu.IsDisposed and self.menu.Visible:
                                self.menu.Close()
                        except Exception:  # noqa: BLE001
                            pass
                    return 0
                return user32.CallWindowProcW(self._old_wndproc, h, msg, wparam, lparam)

            self._wndproc = WNDPROC(wnd_proc)
            old = user32.SetWindowLongPtrW(
                hwnd, GWL_WNDPROC, ctypes.cast(self._wndproc, ctypes.c_void_p)
            )
            if not old:
                self._wndproc = None
                log("窗口过程替换失败，右键可能还会冒出系统菜单")
                return
            self._old_wndproc = old
            log("已屏蔽窗口系统菜单（原 WS_SYSMENU=%s）" % bool(style & WS_SYSMENU))
        except Exception as exc:  # noqa: BLE001
            log("屏蔽系统菜单失败：%r" % (exc,))

    # -- webview plumbing ----------------------------------------------------

    def on_webview_ready(self, sender, args) -> None:
        core = sender.CoreWebView2
        if core is None:
            # 重启时旧实例的 WebView2 子进程可能还没放开 .webview2 用户数据目录，
            # 新实例就会拿到 0x800700AA（资源在使用中）/ 拒绝访问。这类是暂时性的：
            # 先退避重试几次，重试用完还不行才弹提示框。
            if self.retry_webview_init(args.InitializationException):
                return
            log("WebView2 初始化失败：%s" % args.InitializationException)
            self.report_fatal("WebView2 初始化失败", args.InitializationException)
            return
        self._webview_init_tries = 0
        core.Settings.AreDefaultContextMenusEnabled = False
        core.Settings.IsZoomControlEnabled = False
        core.Settings.AreDevToolsEnabled = bool(os.environ.get("CODEXPET_DEVTOOLS"))
        if os.environ.get("CODEXPET_PINGTEST"):
            core.NavigateToString(
                """<!doctype html><meta charset="utf-8"><body style="background:#123">
                <canvas id="c" width="64" height="64"></canvas>
                <script>
                const gl = document.getElementById('c').getContext('webgl');
                gl.clearColor(0.2, 0.9, 0.4, 1.0);
                gl.clear(gl.COLOR_BUFFER_BIT);
                const px = new Uint8Array(4);
                gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, px);
                window.chrome.webview.postMessage(JSON.stringify({
                  t: 'ready',
                  expressions: ['ping'],
                  webgl: !!gl,
                  pixel: Array.from(px),
                  ua: navigator.userAgent.slice(0, 60),
                  inline: true,
                }));
                </script></body>"""
            )
            log("WebView2 就绪（内嵌页面通道自检）")
        else:
            core.Navigate("http://127.0.0.1:%d/index.html" % self.port)
            log("WebView2 就绪，正在加载界面")
        core.NavigationCompleted += self.on_navigation_done

    _WEBVIEW_RETRY_DELAYS = (1.0, 2.0, 3.0, 5.0)

    def retry_webview_init(self, exc) -> bool:
        """初始化失败就退避重试；返回 True 表示已排好下一次重试。

        最常见的失败是重启竞态：旧实例刚被杀掉，WebView2 运行时还占着同一个
        .webview2 用户数据目录（0x800700AA 资源在使用中 / 拒绝访问）。
        隔几秒再试基本都能成功，不该直接弹错误框。
        """
        tries = int(getattr(self, "_webview_init_tries", 0) or 0)
        delays = self._WEBVIEW_RETRY_DELAYS
        if tries >= len(delays) or self.webview is None:
            return False
        delay = delays[tries]
        self._webview_init_tries = tries + 1
        log("WebView2 初始化失败（第 %d 次）：%s；%.0f 秒后重试" % (tries + 1, exc, delay))

        def retry_ui() -> None:
            try:
                self.webview.EnsureCoreWebView2Async(None)
            except Exception as exc2:      # noqa: BLE001
                log("WebView2 重试调用失败：%r" % (exc2,))
                if not self.retry_webview_init(exc2):
                    self.report_fatal("WebView2 初始化失败", exc2)

        def schedule() -> None:
            time.sleep(delay)
            try:
                from System import Action
                self.form.BeginInvoke(Action(retry_ui))
            except Exception as exc2:      # noqa: BLE001
                log("WebView2 重试调度失败：%r" % (exc2,))

        threading.Thread(target=schedule, daemon=True).start()
        return True

    def report_fatal(self, title: str, detail) -> None:
        """出错时弹一个提示框，但绝不卡住：① 可以用环境变量抑制；② 25 秒后自动关掉。"""
        if os.environ.get("CODEXPET_NO_DIALOG"):
            log("（已抑制错误弹窗）%s：%s" % (title, detail))
            return

        def show() -> None:
            try:
                from System.Windows.Forms import MessageBox, MessageBoxButtons, MessageBoxIcon

                MessageBox.Show(
                    "%s\n\n%s\n\n详细信息见日志：%s" % (title, detail, log_path),
                    "Agent 桌宠",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Warning,
                )
            except Exception as exc:  # noqa: BLE001
                log("弹出提示失败：%r" % (exc,))

        def auto_close() -> None:
            time.sleep(25)
            try:
                hwnd = user32.FindWindowW(None, "Agent 桌宠")
                if hwnd:
                    user32.PostMessageW(hwnd, 0x0010, 0, 0)   # WM_CLOSE
            except Exception:
                pass

        threading.Thread(target=show, daemon=True).start()
        threading.Thread(target=auto_close, daemon=True).start()

    def on_navigation_done(self, sender, args) -> None:
        log("页面加载完成：成功=%s 状态=%s" % (args.IsSuccess, args.WebErrorStatus))
        if not args.IsSuccess and not self.ready.is_set():
            self.reload_attempts = getattr(self, "reload_attempts", 0) + 1
            if self.reload_attempts <= 2:
                log("界面没加载起来，2 秒后重试第 %d 次" % self.reload_attempts)

                def retry():
                    time.sleep(2)
                    self.reload_page()

                threading.Thread(target=retry, daemon=True).start()
            else:
                log("界面多次加载失败，请双击 诊断.cmd 查看日志")

    def reload_page(self) -> None:
        """Reload the pet page from any thread (WebView2 objects must be touched on the UI thread)."""
        try:
            from System import Action

            def do():
                core = self.webview.CoreWebView2 if self.webview is not None else None
                if core is None:
                    log("WebView2 还没准备好，稍后再试")
                    return
                core.Reload()

            self.form.BeginInvoke(Action(do))
        except Exception as exc:  # noqa: BLE001
            log("重新加载失败：%r" % (exc,))

    def on_web_message(self, sender, args) -> None:
        try:
            msg = json.loads(args.WebMessageAsJson)
        except Exception:
            return
        kind = msg.get("t")
        if kind == "size":
            rect = msg.get("rect") or [0, 0, 0, 0]
            head = msg.get("headRect") or None
            dpr = float(msg.get("dpr") or 1.0)
            if dpr > 0:
                self.web_dpr = dpr
            self.char_rect = (rect[0] * dpr, rect[1] * dpr, rect[2] * dpr, rect[3] * dpr)
            self.head_rect = (
                (head[0] * dpr, head[1] * dpr, head[2] * dpr, head[3] * dpr) if head else None
            )
            self.panel_open = bool(msg.get("panel"))
        elif kind == "fps":
            self.render_fps = float(msg.get("value") or 0.0)
            # 顺手转发给设置窗口：让"渲染帧率上限"旁边能显示当前实际帧率
            self.send_to_settings_window({"t": "fps", "value": round(self.render_fps, 1)})
            now = time.time()
            if now - self.fps_log_at > 60:
                self.fps_log_at = now
                log(
                    "渲染 FPS：%.0f（窗口 %dx%d，缩放 %.2f）"
                    % (self.render_fps, self.form.ClientSize.Width, self.form.ClientSize.Height, self.pet_scale)
                )
        elif kind == "over":
            value = bool(msg.get("value"))
            if value != self.over_char and (verbose_log or os.environ.get("CODEXPET_TRACE_HIT")):
                log(
                    "像素命中：%s（网页采样 x=%s y=%s alpha=%s；窗口 %s；光标 %s）"
                    % (
                        "角色身上" if value else "透明处（这次点击会穿给桌面）",
                        msg.get("x"),
                        msg.get("y"),
                        msg.get("alpha"),
                        (self.form.Left, self.form.Top),
                        self.last_cursor[:2] if self.last_cursor else None,
                    )
                )
            self.over_char = value
        elif kind == "over-probe":
            self.alpha_probe_ok = bool(msg.get("value"))
            # 画布/CSS/dpr 一起记下来：排查"采样点和光标对不上"时要靠它换算
            log(
                "像素命中检测：%s（画布 %s / CSS %s / dpr %s）"
                % (
                    "可用" if self.alpha_probe_ok else "不可用，退回角色外框判断",
                    msg.get("canvas"),
                    msg.get("css"),
                    msg.get("dpr"),
                )
            )
        elif kind == "panel":
            self.panel_open = bool(msg.get("open"))
        elif kind == "resize":
            self.resize_anchor = "bottom"      # 展开进度卡片时保持底部不动
            self.resize_window(float(msg.get("w") or WINDOW_W), float(msg.get("h") or WINDOW_H))
        elif kind == "open-settings":
            self.show_settings()
        elif kind == "quit":
            log("收到退出指令")
            from System import Action

            self.form.BeginInvoke(Action(self.form.Close))
        elif kind == "reload":
            self.reload_page()
        elif kind == "open-codex":
            self.focus_agent_window()       # 设置页那个"切到窗口"按钮：跟当前 agent 走
        elif kind == "ready":
            self.ready.set()
            extra = ""
            if msg.get("inline"):
                extra = " webgl=%s 像素=%s" % (msg.get("webgl"), msg.get("pixel"))
            log("界面就绪，形象表情数量：%d%s" % (len(msg.get("expressions") or []), extra))
            self.push_settings()
            # 页面刚（重新）加载完时，网页里的卡片状态是默认的“收起” ——
            # 这里按宿主记录的当前状态补一条，否则“重新载入形象”之后卡片会莫名消失。
            self.post({"t": "card", "open": self.inline_card_open})
        elif kind == "status":
            self.status = msg.get("data") or {}
            self.status_ready.set()
        elif kind == "playback-debug":
            data = msg.get("data") or {}
            log(
                "动作硬闸门：%s，清理队列 %s 个（自动动作=%s）"
                % (data.get("reason"), data.get("queued"), data.get("autoMotion"))
            )
        elif kind == "error":
            log("页面报错：%s" % msg.get("message"))
        elif kind == "snapshot":
            self.snapshot = msg.get("data") or ""
            self.snapshot_ready.set()

    def post(self, message: dict) -> None:
        with self.lock:
            self.queue.append(message)

    def flush(self) -> None:
        self.ui_phase("发送消息给页面")
        with self.lock:
            pending, self.queue = self.queue, []
        if not pending or self.webview is None or self.webview.CoreWebView2 is None:
            return
        for message in pending:
            try:
                started = time.time()
                self._flush_started = started
                self._flush_warned = False
                self.webview.CoreWebView2.PostWebMessageAsJson(json.dumps(message, ensure_ascii=False))
                self._flush_started = 0.0
                spent = time.time() - started
                if spent > 0.3:
                    # WebView2（浏览器进程）忙的时候这一步会卡住界面线程 —— 记下来，好定位
                    log("发给页面的 %s 消息耗时 %.1f 秒（WebView2 忙）" % (message.get("t"), spent))
                if spent > 1.0:
                    self.protect_fps("给页面发消息 %.1f 秒" % spent)
            except Exception as exc:  # noqa: BLE001
                log("发送状态失败：%r" % (exc,))

    # -- per-frame work ------------------------------------------------------

    def ui_phase(self, text: str) -> None:
        """记下界面线程当前在做的事（纯字符串赋值，开销可忽略）。"""
        self._ui_phase = text

    def on_tick(self, sender, args) -> None:
        now = time.time()
        gap = now - self._ui_heartbeat
        if gap > 0.8:
            # 2 秒以上的严重卡顿归看门狗；0.8~2 秒的小停顿也记一笔，方便对着时间线查
            log("界面线程停顿 %.1f 秒（最后在做：%s）" % (gap, self._ui_phase))
        self._ui_heartbeat = now             # 给看门狗看：界面线程还活着
        self.ui_phase("界面 tick")
        self.flush()
        self.refresh_rect_cache()
        # 兜底：万一抬起事件没来（钩子被摘、远程桌面抢走之类），别让右键一直处于"被吃掉"状态
        if self._right_swallowed and time.time() - self._rs_down_time > 5.0:
            self._right_swallowed = False
            self._rs_down_at = None
            log("右键状态超时，已复位（兜底）")
        if self._swallow_log:
            self._swallow_log = False
            log("已在系统分发前吃掉这一次右键：下面的窗口收不到，不会弹它的菜单")
        if self._left_swallow_log:
            self._left_swallow_log = False
            log("已在系统分发前吃掉这一次左键：下面的窗口收不到，不会选中文字/误点图标")
        if self._menu_visible:
            menu = self.menu
            if menu is None or menu.IsDisposed or not menu.Visible:
                # 实际已经关了，但 Closed 事件万一没送到：把标记纠偏，
                # 不然钩子会把后面的点击一直当成"菜单开着"处理
                self._menu_visible = False
                self._menu_rect = None
                self._menu_ghost_since = None
                log("菜单状态纠偏：实际已关闭，标记复位")
            else:
                rect = self.menu_screen_rect()
                if rect:
                    self._menu_rect = rect
                # 幽灵菜单自检：菜单还画着、但鼠标捕获已经不在它手里 → 悬停高亮不会亮、
                # 点着也别扭（要先去别处点一下才恢复，还会把菜单模式的快捷键一起吃掉）。
                # 丢了就立刻补回捕获；补不回来（持续 2 秒）就收掉它，下次右键重弹。
                try:
                    captured = bool(menu.Capture)
                except Exception:  # noqa: BLE001
                    captured = True
                if captured:
                    self._menu_ghost_since = None
                else:
                    if not self._menu_ghost_since:
                        self._menu_ghost_since = time.time()
                        log("菜单丢了鼠标捕获，已尝试补回（悬停高亮依赖它）")
                        try:
                            menu.Capture = True
                        except Exception:  # noqa: BLE001
                            pass
                    elif time.time() - self._menu_ghost_since > 2.0:
                        self._menu_ghost_since = None
                        log("菜单捕获一直补不回来（幽灵状态）：已收掉，下次右键重弹")
                        try:
                            menu.Close()
                        except Exception:  # noqa: BLE001
                            pass
        if self._menu_close_at:
            if time.time() - self._menu_close_at > 1.0:
                self._menu_close_at = 0.0      # 过期作废：绝不能影响到下一次菜单
            elif self._menu_visible:
                self._menu_close_at = 0.0
                try:
                    self.menu.Close()
                except Exception as exc:  # noqa: BLE001
                    log("关闭右键菜单失败：%r" % (exc,))
        self.handle_mouse()
        self.drain_wheel()

        hwnd = self.form.Handle.ToInt64()
        now = time.time()
        for event in self.mouse.tick(now):        # 单击 / 双击的判定要靠定时器推动
            cursor = wt.POINT()
            user32.GetCursorPos(ctypes.byref(cursor))
            self.on_mouse_event(event, cursor, now)
        self.stream_cursor(now)
        if now - self.last_stop_check > 0.5:
            self.last_stop_check = now
            if os.path.exists(STOP_FILE):
                try:
                    os.remove(STOP_FILE)
                except OSError:
                    pass
                log("收到停止请求，退出桌宠")
                self.form.Close()
                return
        if self.desktops and self.settings.get("followDesktop", True) and now - self.last_follow > 0.6:
            self.last_follow = now
            self.desktops.follow(hwnd)

        if self.card is not None and not self.card.IsDisposed and self.card.Visible:
            # 位置**或尺寸**变了都要重新摆卡片（缩放桌宠时最容易挡住它）
            here = (self.form.Left, self.form.Top, self.form.ClientSize.Width, self.form.ClientSize.Height)
            if here != self.last_card_pos:
                self.apply_card_mode()
                self.position_card()

    # 鼠标事件不走网页（透明窗口是分层的，子窗口收不到点击），这里直接读键鼠状态来驱动互动
    def handle_mouse(self) -> None:
        # 必须先消费钩子记录的 DOWN/UP。用户在两次 33ms 轮询之间完成一次快速点击时，
        # 只看“当前按键状态”会连按下都没见到；队列保留完整脉冲，所以单击不会丢。
        self.drain_hook_mouse_steps()
        if self.form_input_works:
            return   # 已经能收到真实的窗口鼠标事件，就不再用轮询凑
        if self._menu_visible or time.time() < self._ignore_click_until:
            # 菜单开着（或刚被“点菜单外面”关掉）的这一下：鼠标操作归菜单和下层窗口，
            # 底层一律不判定 —— 否则菜单浮在哪，就会连带触发它下面的那个图标按钮
            # （点“重新载入形象”顺手触发橡皮擦，就是这么来的）。
            self.mouse.pending_single = 0.0     # 丢掉排队中的单击，免得菜单一关就补触发
            return
        cursor = wt.POINT()
        user32.GetCursorPos(ctypes.byref(cursor))
        # 设置窗口、卡片、对话框等同进程窗口可能压在桌宠上面。它们的坐标仍落在
        # 桌宠矩形里，但输入必须归上层窗口；否则滑设置选项会同时缩放/拖动桌宠。
        if self.point_on_input_blocker((cursor.x, cursor.y)) and not self._left_swallowed and not self.mouse.dragging:
            self.mouse.pending_single = 0.0
            return
        # 左键被钩子吞掉后 GetAsyncKeyState 不再可靠；用钩子维护的“逻辑按下状态”
        # 驱动原来的 MouseTracker。这样单击/双击/拖动状态机不用重写，也不会漏掉松手。
        left = self._left_swallowed or bool(user32.GetAsyncKeyState(0x01) & 0x8000)
        right = bool(user32.GetAsyncKeyState(0x02) & 0x8000)
        now = time.time()

        width = self.form.ClientSize.Width
        height = self.form.ClientSize.Height
        local_x = cursor.x - self.form.Left
        local_y = cursor.y - self.form.Top
        inside = 0 <= local_x < width and 0 <= local_y < height
        locked = now < self.zoom_lock_until      # 缩放的瞬间不接受拖动，避免互相打架
        on_char = inside and not locked and self.on_character(local_x, local_y)
        if self._left_swallowed:
            # 按下已经在钩子里确认过“角色像素”，别让网页异步采样的滞后把这次拖动否掉。
            on_char = inside and not locked
        on_head = on_char and self.on_head(local_x, local_y)

        for event in self.mouse.feed(cursor.x, cursor.y, left, right, on_char, on_head, now):
            self.on_mouse_event(event, cursor, now)

    def drain_hook_mouse_steps(self) -> None:
        """把钩子保存的左键 DOWN/UP 依次喂给 MouseTracker。

        钩子回调本身只做命中判断、保存普通元组和返回 0/1；业务动作全部延后到这里。
        这样既不会让 WH_MOUSE_LL 因日志/CLR 调用超时，也能保住“按下→松开都发生在
        两次轮询之间”的快速单击和双击。
        """
        if not self._hook_mouse_steps:
            return
        steps, self._hook_mouse_steps = self._hook_mouse_steps, []
        for phase, xy, at in steps:
            left_down = phase == "down"
            x, y = xy
            if self._rect_cache:
                local_x = x - self._rect_cache[0]
                local_y = y - self._rect_cache[1]
            else:
                local_x, local_y = x, y
            # DOWN 已经在钩子里确认过角色像素；UP 继续按“在角色上”处理，
            # 小幅抖到透明边缘时仍按项目原规则算一次点击，拖动则由 drag-end 收尾。
            on_char = True
            on_head = left_down and self.on_head(local_x, local_y)
            events = self.mouse.feed(x, y, left_down, False, on_char, on_head, at)
            cursor = wt.POINT(int(x), int(y))
            for event in events:
                self.on_mouse_event(event, cursor, at)

    def _cursor_xy(self):
        """钩子里用的极简取点：只读一次 GetCursorPos，不做别的。"""
        cursor = wt.POINT()
        if not user32.GetCursorPos(ctypes.byref(cursor)):
            return None
        return (cursor.x, cursor.y)

    def register_aux_window(self, form) -> None:
        """登记一个必须优先接收输入的辅助顶层窗口。"""
        try:
            self._aux_hwnds.add(int(form.Handle.ToInt64()))
        except Exception:
            pass

    def unregister_aux_window(self, form) -> None:
        try:
            self._aux_hwnds.discard(int(form.Handle.ToInt64()))
        except Exception:
            pass

    def point_on_auxiliary(self, xy) -> bool:
        """这个点是不是落在本桌宠进程的辅助窗口上（设置/卡片/对话框/快速面板）？

        WindowFromPoint 对色键透明的桌宠本体可能答“下面的窗口”，但对不透明的设置窗口
        是可靠的。这里只用它做**排除**：命中同进程、非桌宠顶层窗口就绝不把输入当桌宠的。
        """
        if not xy:
            return False
        for hwnd in list(self._aux_hwnds):
            rect = wt.RECT()
            if user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(rect)):
                if rect.left <= xy[0] < rect.right and rect.top <= xy[1] < rect.bottom:
                    return True
        box = self._menu_rect
        if box and box[0] <= xy[0] < box[2] and box[1] <= xy[1] < box[3]:
            return True
        try:
            owner = user32.WindowFromPoint(wt.POINT(int(xy[0]), int(xy[1])))
            if not owner:
                return False
            root = user32.GetAncestor(wt.HWND(owner), GA_ROOT) or owner
            root_int = int(root)
            if root_int == int(self._hwnd_int or 0):
                return False
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(wt.HWND(root), ctypes.byref(pid))
            return bool(pid.value and pid.value == os.getpid())
        except Exception:
            return False

    @staticmethod
    def _hwnd_value(handle) -> int:
        """把 wt.HWND/ctypes.c_void_p 统一转成整数句柄。"""
        try:
            return int(handle)
        except (TypeError, ValueError):
            try:
                return int(handle.value or 0)
            except Exception:
                return 0

    @staticmethod
    def _is_shell_overlay_process_name(name: str) -> bool:
        base = os.path.basename(str(name or "")).lower()
        return base in SHELL_OVERLAY_PROCESS_NAMES or any(token in base for token in SHELL_OVERLAY_PROCESS_TOKENS)

    def _window_process_name(self, hwnd) -> str:
        """读取顶层窗口进程名；只用于 Shell 面板白名单，不碰桌宠输入路径。"""
        try:
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(wt.HWND(hwnd), ctypes.byref(pid))
            if not pid.value:
                return ""
            cache = getattr(self, "_overlay_process_cache", {})
            cached = cache.get(pid.value)
            now = time.time()
            if cached and now - cached[1] < 30.0:
                return cached[0]
            handle = kernel32.OpenProcess(0x1000, 0, pid.value)
            name = ""
            if handle:
                try:
                    size = ctypes.c_uint(260)
                    buf = ctypes.create_unicode_buffer(260)
                    if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                        name = buf.value
                finally:
                    kernel32.CloseHandle(handle)
            cache[pid.value] = (name, now)
            self._overlay_process_cache = cache
            return name
        except Exception:
            return ""

    def _window_above_pet(self, root) -> bool:
        """按顶层窗口 Z 序判断 root 是否真的盖在桌宠上面。

        不能按“WindowFromPoint 不是桌宠”判断：桌宠是色键透明窗口，系统经常把
        颜色像素下的窗口答成 owner；只有 Z 序在桌宠之前，才是真正的覆盖层。
        """
        try:
            pet = int(self._hwnd_int or 0)
            target = self._hwnd_value(root)
            if not pet or not target or target == pet:
                return False
            # 从桌面最上方开始遍历真正的顶层 Z 序，不依赖 GetWindow(GW_HWNDFIRST)
            # 对 popup/owned window 的解释；快速设置这类 Shell popup 也能正确比较。
            current = self._hwnd_value(user32.GetTopWindow(None))
            seen = 0
            while current and seen < 1024:
                if current == target:
                    return True
                if current == pet:
                    return False
                current = self._hwnd_value(user32.GetWindow(wt.HWND(current), GW_HWNDNEXT))
                seen += 1
        except Exception:
            pass
        return False

    def point_on_shell_overlay(self, xy) -> bool:
        """光标是不是落在任务栏、开始菜单、快速设置/控制中心等覆盖层上。

        先认已知 Shell 窗口类；再按顶层 Z 序兜底。这样 Win11 不同 Build 的
        快速设置窗口类名变化也能拦截，同时不会误伤色键透明桌宠下面的普通窗口。
        """
        if not xy:
            return False
        try:
            owner = user32.WindowFromPoint(wt.POINT(int(xy[0]), int(xy[1])))
            if not owner:
                return False
            root = user32.GetAncestor(wt.HWND(owner), GA_ROOT) or owner
            buf = ctypes.create_unicode_buffer(128)
            user32.GetClassNameW(wt.HWND(root), buf, 128)
            class_name = buf.value
            if class_name in SHELL_OVERLAY_CLASSES:
                return True
            if self._is_shell_overlay_process_name(self._window_process_name(root)):
                return True
            # 快速设置在部分 Build 上是普通顶层窗口，但 Z 序确实高于桌宠；
            # 这条兜底也能处理类名和进程名都变化的情况。
            return self._window_above_pet(root)
        except Exception:
            return False

    def point_on_input_blocker(self, xy) -> bool:
        """任何应当优先接收鼠标输入、阻止桌宠响应的覆盖窗口。"""
        return bool(self.point_on_auxiliary(xy) or self.point_on_shell_overlay(xy))

    def pointer_on_pet(self, xy=None) -> bool:
        """鼠标钩子专用，必须极快：当前这一下是不是点在角色像素上？

        左右键共用同一套命中结论：只看两个现成的缓存 —— 网页报来的像素结论
        over_char、窗口矩形 _rect_cache —— 不写日志、不碰 CLR 属性
        （低层钩子回调慢一点就会被系统摘掉）。xy 可复用钩子刚取到的坐标。
        """
        if not self.over_char or not self._rect_cache:
            return False
        left, top, right, bottom = self._rect_cache
        if xy is None:
            xy = self._cursor_xy()
        return bool(xy and not self.point_on_input_blocker(xy) and left <= xy[0] < right and top <= xy[1] < bottom)

    def right_click_on_pet(self) -> bool:
        """兼容旧调用名；右键命中现在和左键共用 pointer_on_pet()。"""
        return self.pointer_on_pet()

    def wheel_on_pet(self, xy) -> bool:
        """滚轮缩放专用命中：设置等辅助窗口优先排除，再沿用像素/外框判断。"""
        if not self._rect_cache or self.point_on_input_blocker(xy):
            return False
        left, top, right, bottom = self._rect_cache
        if not (left <= xy[0] < right and top <= xy[1] < bottom):
            return False
        return self.on_character(xy[0] - left, xy[1] - top)

    def on_head(self, local_x: float, local_y: float) -> bool:
        """头部区域：网页报了头框就用它，否则退回"角色上方 40%"。"""
        rect = self.head_rect or (
            (self.char_rect[0] + self.char_rect[2] * 0.2, self.char_rect[1], self.char_rect[2] * 0.6, self.char_rect[3] * 0.4)
            if self.char_rect
            else None
        )
        if rect is None:
            return False
        rx, ry, rw, rh = rect
        return rx <= local_x <= rx + rw and ry <= local_y <= ry + rh

    def tool_zone(self, local_x: float, local_y: float) -> str | None:
        """底部三个图标（撤回 / 铅笔 / 橡皮擦）的命中区。

        图标是画死在模型图里的，位置固定，所以按实测比例定位（TOOL_MARK_*）。
        千万别按"角色宽度三等分"来分 —— 那三个图标挤在中间偏左，三等分必然错位。
        """
        if not self.char_rect:
            return None
        rx, ry, rw, rh = self.char_rect
        if rw <= 0 or rh <= 0:
            return None
        fx = (local_x - rx) / rw
        fy = (local_y - ry) / rh
        dy = fy - TOOL_MARK_Y
        if dy < -TOOL_MARK_HALF_Y_UP or dy > TOOL_MARK_HALF_Y_DOWN:
            return None
        best = None
        best_dx = TOOL_MARK_HALF_X
        for name, mark_x in TOOL_MARK_X:
            dx = abs(fx - mark_x)
            if dx <= best_dx:
                best, best_dx = name, dx
        return best

    def on_mouse_event(self, event: str, cursor, now: float) -> None:
        log("鼠标事件：%s (%d,%d)" % (event, cursor.x, cursor.y))
        if self._menu_visible or now < self._ignore_click_until:
            # 这一下点击属于菜单（或刚关掉菜单后的下层窗口），别让底下的图标/摸头跟着响应
            return
        local_x = cursor.x - self.form.Left
        local_y = cursor.y - self.form.Top
        tool = self.tool_zone(local_x, local_y)
        if event in ("pat", "poke"):
            if tool:
                self.trigger_bottom_action(tool, "single", cursor)
            else:
                self.post({"t": "react", "kind": "pat" if event == "pat" else "poke"})
        elif event == "cycle":
            if tool:
                # 图标上双击也按单击处理：手感上"点一下就触发"最直接，
                # 万一习惯了双击也不会变成没反应
                self.trigger_bottom_action(tool, "single", cursor)
            else:
                self.post({"t": "cycle-expression"})
        elif event == "menu":
            if tool:
                self.trigger_bottom_action(tool, "right", cursor)
            else:
                # 只记录诊断、不做拦截：曾经在这里"系统说不是桌宠就不弹菜单"，结果
                # 这台机器上 WindowFromPoint 对所有点都答"不是桌宠"，右键菜单直接全废。
                # 要不要响应，仍然只由网页那边的像素判定说了算。
                owner = self.point_owner_is_pet(cursor.x, cursor.y)
                if owner is not True and (verbose_log or os.environ.get("CODEXPET_TRACE_HIT")):
                    log(
                        "落点诊断（不影响响应）：像素=%s 系统=%s %s"
                        % ("角色身上" if self.over_char else "透明处", owner, self.describe_point_owner(cursor.x, cursor.y))
                    )
                if time.time() - self._menu_shown_at > 0.35:
                    self.show_menu(cursor.x, cursor.y)
        elif event == "drag-start":
            if self.position_locked:
                self.post({"t": "say", "text": "位置已锁定"})
                log("位置已锁定，忽略拖动")
                return
            self.start_drag_loop()
        elif event == "drag-end":
            self.finish_drag()   # 兜底：拖动定时器已经在盯着按键状态了

    def point_owner_is_pet(self, screen_x: float, screen_y: float):
        """问系统：这个点会被投给哪个窗口？True=桌宠 / False=别的窗口 / None=问不出来。

        Windows 的点击穿透（色键洋红 + 分层窗口命中测试）是系统在投递鼠标消息时决定的；
        我们轮询判断只是事后补一刀。所以真正要弹菜单/开始拖动之前，再问一次系统最可靠。
        """
        try:
            owner = user32.WindowFromPoint(wt.POINT(int(screen_x), int(screen_y)))
            if not owner:
                return None
            root = user32.GetAncestor(wt.HWND(owner), GA_ROOT) or owner
            return int(root) == int(self.form.Handle.ToInt64())
        except Exception as exc:  # noqa: BLE001
            log("查询落点归属失败：%r" % (exc,))
            return None

    def describe_point_owner(self, screen_x: float, screen_y: float) -> str:
        """诊断用：这个点系统认为归谁（类名 + 句柄），跟桌宠窗口句柄一起打出来。"""
        try:
            owner = user32.WindowFromPoint(wt.POINT(int(screen_x), int(screen_y)))
            if not owner:
                return "owner=<none>"
            buf = ctypes.create_unicode_buffer(128)
            user32.GetClassNameW(wt.HWND(owner), buf, 128)
            root = user32.GetAncestor(wt.HWND(owner), GA_ROOT)
            rname = "-"
            if root:
                rbuf = ctypes.create_unicode_buffer(128)
                user32.GetClassNameW(wt.HWND(root), rbuf, 128)
                rname = rbuf.value
            return "owner=%s(0x%X) root=%s(0x%X) pet=0x%X" % (
                buf.value, int(owner), rname, int(root) if root else 0, self.form.Handle.ToInt64()
            )
        except Exception as exc:  # noqa: BLE001
            return "err=%r" % (exc,)

    def on_character(self, local_x: float, local_y: float, margin: float = 10) -> bool:
        """只有点在角色身上才算互动，透明边角就让点击穿过去。"""
        if self.alpha_probe_ok and self.over_char is not None:
            # 有像素级判断就用它：这样右键点空白处不会既弹我们的菜单、又弹桌面菜单
            return bool(self.over_char)
        if self.char_rect is None:
            return True
        rx, ry, rw, rh = self.char_rect
        return (rx - margin) <= local_x <= (rx + rw + margin) and (ry - margin) <= local_y <= (ry + rh + margin)

    # -- 滚轮缩放 ------------------------------------------------------------

    def on_mouse_wheel(self, sender, args) -> None:
        if self._wheel_hook:
            return   # 有全局滚轮钩子时以它为准，避免一次滚动被处理两遍
        steps = args.Delta / 120.0
        if not steps:
            return
        self.zoom_by(steps * 0.06)

    def install_wheel_hook(self) -> None:
        """底层鼠标钩子：滚轮缩放 + 吃掉"点在角色上"的右键。

        这个窗口对鼠标几乎是全穿透的（系统不把鼠标消息投给桌宠），所以：
        * 滚轮：只在光标位于角色像素上时才消费，避免影响别的程序；
        * 右键：点在角色像素上就在系统分发之前吃掉（返回 1），下面的窗口收不到、
          也就不会弹它自己的菜单；整条右键链路（按下→跟踪移动→抬起→弹菜单）
          都在这里自己走完 —— 事件被丢弃后 GetAsyncKeyState 也读不到这次按键了。
        """
        self._hwnd_int = self.form.Handle.ToInt64()
        WH_MOUSE_LL = 14
        WM_MOUSEWHEEL = 0x020A
        WM_MOUSEMOVE = 0x0200
        WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0201, 0x0202
        WM_RBUTTONDOWN, WM_RBUTTONUP = 0x0204, 0x0205
        # CallNextHookEx 必须声明参数类型：lparam 在 64 位上是 64 位的，
        # 不声明的话 ctypes 按 32 位整数传，稍微大一点的值就 OverflowError。
        user32.PostMessageW.argtypes = [wt.HWND, ctypes.c_uint, wt.WPARAM, wt.LPARAM]
        user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wt.WPARAM, wt.LPARAM]
        user32.CallNextHookEx.restype = ctypes.c_void_p
        HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_int, wt.WPARAM, wt.LPARAM)

        def hook(code, wparam, lparam):
            if code >= 0:
                # 右键：如果这一下点在角色像素上，就直接把它从系统那里吃掉。
                # 这台机器上桌宠窗口对鼠标是全穿透的（实测 WindowFromPoint 把角色像素
                # 判给了 Progman/桌面），不吞的话下面的窗口会弹出它自己的菜单，
                # 而且画在我们菜单上面 —— 用户看到的"只有系统菜单"就是这么来的。
                # 钩子返回 1 = 这个事件不再往下传；松手那一下按同样的条件一起吞，
                # 免得下面的窗口收到一个孤立的抬起。
                # 菜单开着的时候，左键点在菜单外面：
                # 这里只把菜单的鼠标捕获放掉（ToolStrip 失去捕获会自己 Dismiss），
                # 并且**不吞**这个事件 —— 于是这一次点击既关掉菜单，又照常落到桌面上，
                # 不用"第一次点只关菜单、第二次才生效"。点在菜单里面则一切照旧。
                if wparam == WM_LBUTTONDOWN and self._menu_visible:
                    xy = self._cursor_xy()
                    box = self._menu_rect
                    inside = bool(xy and box and box[0] <= xy[0] < box[2] and box[1] <= xy[1] < box[3])
                    if xy and not inside:
                        self._ignore_click_until = time.time() + 0.4   # 这一下点击别再触发底层按钮
                        user32.ReleaseCapture()          # 先放开捕获：这一次左键就不会被菜单吃掉
                        if self._hwnd_int:
                            user32.PostMessageW(wt.HWND(self._hwnd_int), WM_PET_CLOSE_MENU, 0, 0)
                        self._menu_close_at = time.time()   # 再留一道 tick 兜底（1 秒内有效）
                # 左键：和已验证可用的右键走同一套“系统分发前吞掉”方案。
                # 只有按下确认在角色像素上才开始吞；随后 MOVE 可以照常传给其他程序
                # （对方从未收到 DOWN，因此不会进入选择/拖拽），UP 必须成对吞掉，
                # 否则下层窗口会收到一个孤立的抬起。逻辑按键状态由 handle_mouse()
                # 喂给 MouseTracker，因此这里绝不能在钩子回调里直接做业务/写日志。
                if wparam == WM_LBUTTONDOWN and not self._menu_visible:
                    down_xy = self._cursor_xy()
                    if down_xy and self.pointer_on_pet(down_xy):
                        now = time.time()
                        self._left_swallowed = True
                        self._left_swallow_log = True
                        self._ls_down_at = down_xy
                        self._ls_down_time = now
                        self._hook_mouse_steps.append(("down", down_xy, now))
                        return 1
                if wparam == WM_LBUTTONUP and self._left_swallowed:
                    up_xy = self._cursor_xy()
                    now = time.time()
                    self._left_swallowed = False
                    self._ls_down_at = None
                    self._hook_mouse_steps.append(("up", up_xy or (0, 0), now))
                    return 1
                if wparam == WM_RBUTTONDOWN and not self._menu_visible and self.pointer_on_pet():
                    # 事件被吃掉之后，GetAsyncKeyState 那边可能就看不到这次按键了，
                    # 所以整条右键链路必须在这里自己走完：记住按下位置 → 跟踪移动 → 抬起时决定弹菜单。
                    self._right_swallowed = True
                    self._swallow_log = True
                    self._rs_down_at = self._cursor_xy()
                    self._rs_down_time = time.time()
                    self._rs_peak = 0
                    return 1
                if wparam == WM_MOUSEMOVE and self._right_swallowed:
                    now_xy = self._cursor_xy()
                    if now_xy and self._rs_down_at:
                        far = abs(now_xy[0] - self._rs_down_at[0]) + abs(now_xy[1] - self._rs_down_at[1])
                        if far > self._rs_peak:
                            self._rs_peak = far
                if wparam == WM_RBUTTONUP and self._right_swallowed:
                    self._right_swallowed = False
                    up_xy = self._cursor_xy()
                    if up_xy and self._rs_peak <= 8:      # 没怎么动 = 一次正经右键，弹菜单
                        self._pending_menu_at = up_xy
                    self._rs_down_at = None
                    return 1
                if wparam == WM_MOUSEWHEEL and self.settings.get("wheelZoom", True):
                    wheel_xy = self._cursor_xy()
                    # 上层设置窗口/卡片/对话框里的滚轮必须原样交给它。以前只看桌宠矩形和
                    # 缓存的 over_char，设置压在桌宠上时会被误认成“滚轮缩放桌宠”。
                    if wheel_xy and self.wheel_on_pet(wheel_xy):
                        try:
                            data = ctypes.cast(lparam, ctypes.POINTER(ctypes.c_ulong))
                            mouse_data = data[2]  # MSLLHOOKSTRUCT.mouseData
                            delta = ctypes.c_short((mouse_data >> 16) & 0xFFFF).value
                            self.pending_wheel += delta
                            self.wheel_at = time.time()
                            return 1
                        except Exception:
                            pass
            return user32.CallNextHookEx(None, code, wparam, lparam)

        self._hook_proc = HOOKPROC(hook)   # 必须留着引用，否则会被回收
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        self._wheel_hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._hook_proc, None, 0)
        log("滚轮钩子：%s" % ("已安装" if self._wheel_hook else "安装失败（改用窗口滚轮事件）"))
        self.start_hook_watchdog()

    def start_hook_watchdog(self) -> None:
        """鼠标钩子看门狗：界面线程一旦卡住超过 2 秒，先把底层鼠标钩子摘掉。

        为什么要有它：WH_MOUSE_LL 是"系统级"钩子 —— 它的回调必须由安装它的线程
        （也就是界面线程）及时处理。万一界面线程被某个同步调用卡住（比如等一个
        起不来的子进程），系统就会一直等我们，表现是**整台电脑的鼠标都动不了**。
        真出这种事时，看门狗先把钩子摘掉让鼠标立刻恢复；界面线程缓过来再装回去。
        """
        if self._hook_watchdog:
            return
        self._hook_watchdog = True

        def loop() -> None:
            while True:
                time.sleep(0.5)
                try:
                    gap = time.time() - self._ui_heartbeat
                    stalled = gap > 2.0
                    if gap > 1.5:
                        self.protect_fps("界面线程停顿 %.1f 秒" % gap)
                    if stalled and self._wheel_hook and not self._hook_stalled:
                        self._hook_stalled = True
                        self._stall_started = self._ui_heartbeat
                        handle = self._wheel_hook
                        self._wheel_hook = None
                        # 钩子一旦被摘，成对的 UP 可能永远收不到。清掉钩子侧按钮状态，
                        # 让下一次 tick 把 MouseTracker / 拖动状态正常释放。
                        self._left_swallowed = False
                        self._right_swallowed = False
                        user32.UnhookWindowsHookEx(ctypes.c_void_p(handle))
                        log("看门狗：界面线程卡住超过 2 秒（最后在做：%s），已临时摘掉鼠标钩子（鼠标应当立刻恢复）" % self._ui_phase)
                    elif (not stalled) and self._hook_stalled:
                        self._hook_stalled = False
                        stalled_for = max(0.0, time.time() - getattr(self, "_stall_started", self._ui_heartbeat))
                        log("看门狗：界面线程已恢复（刚才卡了约 %.1f 秒），鼠标钩子已装回" % stalled_for)
                        self.invoke_on_pet(self.reinstall_wheel_hook)
                    # WebView2 卡住时 PostWebMessageAsJson 会把调用线程钉死（不抛异常、也不返回）。
                    # 这两条线索能区分"桌宠页面卡住"还是"设置页面卡住"：
                    if self._flush_started and not self._flush_warned and (time.time() - self._flush_started) > 3.0:
                        self._flush_warned = True
                        log("看门狗：给桌宠页面发消息已卡住 %.1f 秒（桌宠的 WebView2 没响应）" % (time.time() - self._flush_started))
                    if self._settings_posting and self._settings_post_started and not self._settings_post_warned and (time.time() - self._settings_post_started) > 3.0:
                        self._settings_post_warned = True
                        log("看门狗：给设置窗口发消息已卡住 %.1f 秒（设置窗口的 WebView2 没响应）" % (time.time() - self._settings_post_started))
                except Exception as exc:  # noqa: BLE001
                    log("鼠标钩子看门狗出错：%r" % (exc,))

        threading.Thread(target=loop, name="hook-watchdog", daemon=True).start()

    def reinstall_wheel_hook(self) -> None:
        """界面线程恢复后把钩子装回去（必须在界面线程上装）。"""
        try:
            if not self._wheel_hook:
                self.install_wheel_hook()
                log("看门狗：界面线程已恢复，鼠标钩子已装回")
        except Exception as exc:  # noqa: BLE001
            log("重新安装鼠标钩子失败：%r" % (exc,))

    def drain_wheel(self) -> None:
        if not self.pending_wheel:
            return
        picker = getattr(self, "agent_picker", None)
        if picker is not None and not picker.IsDisposed:
            self.pending_wheel = 0        # 快速切换面板开着：滚轮别顺带去缩桌宠
            return
        self.ui_phase("处理滚轮缩放")
        delta, self.pending_wheel = self.pending_wheel, 0
        # 只有光标在桌宠（角色像素）上才缩放
        cursor = wt.POINT()
        user32.GetCursorPos(ctypes.byref(cursor))
        if not self.wheel_on_pet((cursor.x, cursor.y)):
            return
        self.zoom_by((delta / 120.0) * 0.06)

    def zoom_by(self, delta: float) -> None:
        low, high = self.scale_limits()
        target = max(low, min(high, self.pet_scale + delta))
        if abs(target - self.pet_scale) < 0.001:
            return
        self.pet_scale = target
        self.settings["scale"] = target
        self.zoom_lock_until = time.time() + 0.45   # 缩放过程中先别拖动，避免互相打架
        self.resize_anchor = "center"               # 以窗口中心为锚点，缩放时不会飞走
        base = (PANEL_W, PANEL_H) if self.panel_open else (WINDOW_W, WINDOW_H)
        self.resize_window(*base)
        settings_store.save(self.settings)
        self.push_settings()                        # 通知渲染层和设置窗口
        log("滚轮缩放：%.0f%%（窗口 %dx%d）" % (target * 100, self.form.ClientSize.Width, self.form.ClientSize.Height))
        self.apply_card_mode()
        if self.card is not None and not self.card.IsDisposed and self.card.Visible:
            self.position_card()

    # -- 拖动：独立线程 + SetWindowPos，不受 110ms 界面定时器限制 -----------------

    def refresh_rect_cache(self) -> None:
        """把 WinForms 窗口矩形缓存到普通 Python 元组，供低层鼠标钩子快速读取。"""
        try:
            self._rect_cache = (
                self.form.Left,
                self.form.Top,
                self.form.Left + self.form.ClientSize.Width,
                self.form.Top + self.form.ClientSize.Height,
            )
        except Exception:
            pass

    def start_drag_loop(self) -> None:
        """拖动窗口：用一个 15ms 的 UI 定时器按光标位移搬运窗口。

        为什么不用另外几种写法：
        * `WM_NCLBUTTONDOWN/HTCAPTION` 的系统移动循环：在这个窗口上会立刻返回（日志实测 0.00s）。
        * 靠 MouseMove 事件搬窗口：一旦某个事件没送到（WebView2 吃掉 / 异常被吞），就完全不动。
        * 独立线程 144Hz：分层窗口反复合成，把系统鼠标都拖死。

        现在这个做法不依赖鼠标事件，只看"光标现在在哪 + 按键还按着吗"，每 15ms 更新一次位置，
        全程在 UI 线程、单线程、无重绘风暴，松手立刻停。
        """
        self.post({"t": "react", "kind": "drag"})
        if self.drag_timer is None:
            log("拖动定时器未初始化，无法拖动")
            return
        anchor = (self.mouse.press_pos[0], self.mouse.press_pos[1], self.form.Left, self.form.Top)
        self.drag_anchor = anchor
        self.drag_button = "left" if self.mouse.press_button == "left" else "right"
        self.drag_started_at = time.time()
        self.drag_moves = 0
        # 拖动全程把鼠标捕获在这只窗口上。不捕获的话，按住、移动、松手这一串里
        # 只要有一刻没有窗口接住，松手那下就会落到光标底下的窗口/图标上 ——
        # 表现就是"拖桌宠的时候把下面的东西也点了"。
        # 只在"这次按下确实压在角色像素上"时才抓：像素探测还没起来（比如网页还没
        # 加载完）的时候判断本来就不可信，那时抓鼠标只会把用户的鼠标操作抢过来。
        # 钩子已经把 DOWN/UP 成对吞掉，下层从未收到按下，不需要也不应该再抢系统捕获。
        # 捕获只留给“钩子没接管”的兼容路径，避免把菜单/其他窗口的输入抢走。
        if not self._left_swallowed and (self.alpha_probe_ok or self.form_input_works):
            try:
                user32.SetCapture(wt.HWND(self.form.Handle.ToInt64()))
            except Exception as exc:  # noqa: BLE001
                log("拖动前捕获鼠标失败：%r" % (exc,))
        log("开始拖动：锚点 %s（%s 键）" % (anchor, self.drag_button))
        self.drag_timer.Start()

    def on_drag_timer(self, sender, args) -> None:
        anchor = self.drag_anchor
        if not anchor:
            self.drag_timer.Stop()
            return
        if self.drag_button == "left":
            # 被钩子吞掉的左键不能看 GetAsyncKeyState；以钩子维护的逻辑状态为准。
            held = self._left_swallowed or bool(user32.GetAsyncKeyState(0x01) & 0x8000)
        else:
            held = bool(user32.GetAsyncKeyState(0x02) & 0x8000)
        if not held:
            self.finish_drag()
            return
        cursor = wt.POINT()
        user32.GetCursorPos(ctypes.byref(cursor))
        ax, ay, win_x, win_y = anchor
        try:
            self.form.Location = self.Point(int(win_x + cursor.x - ax), int(win_y + cursor.y - ay))
            self.refresh_rect_cache()   # 拖完立刻按下时，钩子不能拿旧窗口矩形误判
            self.drag_moves += 1
        except Exception as exc:  # noqa: BLE001
            log("拖动失败：%r" % (exc,))
            self.finish_drag()

    def finish_drag(self) -> None:
        self.drag_timer.Stop()
        self._press_captured = False
        try:
            user32.ReleaseCapture()
        except Exception as exc:  # noqa: BLE001
            log("释放鼠标捕获失败：%r" % (exc,))
        anchor = self.drag_anchor
        self.drag_anchor = None
        if not anchor:
            return
        used = max(0.001, time.time() - self.drag_started_at)
        save_position(self.form.Left, self.form.Top)
        self.post({"t": "react", "kind": "drop"})
        log(
            "拖动结束：%.2fs，搬动 %d 次（%.0f 次/秒），位置 %s，渲染 %.0f fps"
            % (used, self.drag_moves, self.drag_moves / used, (self.form.Left, self.form.Top), self.render_fps)
        )

    def apply_manual_drag(self, x: float, y: float) -> None:
        """保留空实现：早期版本靠 MouseMove 搬窗口，现在统一由拖动定时器负责。"""

    def end_manual_drag(self) -> None:
        self.finish_drag()

    def stop_drag_loop(self) -> None:
        self.drag_stop.set()

    WM_NCLBUTTONDOWN = 0x00A1
    HTCAPTION = 2

    def native_drag(self) -> None:
        hwnd = wt.HWND(self.form.Handle.ToInt64())
        started = time.time()
        before = (self.form.Left, self.form.Top)
        monitor = threading.Thread(target=self.monitor_cursor, args=(started,), daemon=True)
        monitor.start()
        try:
            user32.ReleaseCapture()
            user32.SendMessageW(hwnd, self.WM_NCLBUTTONDOWN, self.HTCAPTION, 0)
        except Exception as exc:  # noqa: BLE001
            log("原生拖动失败：%r" % (exc,))
        self.drag_monitor_stop.set()
        seconds = max(0.001, time.time() - started)
        samples = self.drag_samples
        self.drag_samples = 0
        save_position(self.form.Left, self.form.Top)
        self.post({"t": "react", "kind": "drop"})
        log(
            "拖动结束：%.2fs，位置 %s -> %s；期间采样 %d 次、指针位置变化 %d 次"
            "（最大间隔 %.0f ms），渲染 %.0f fps"
            % (seconds, before, (self.form.Left, self.form.Top), samples, self.drag_changes, self.drag_max_gap_ms, self.render_fps)
        )
        self.drag_changes = 0
        self.drag_max_gap_ms = 0.0

    def monitor_cursor(self, started: float) -> None:
        """拖动期间按 ~120Hz 采样指针位置：用来判断系统有没有丢鼠标事件。

        只在独立线程里读 GetCursorPos，不做任何耗时操作。
        """
        hop_to_interactive_desktop()   # 新线程默认在沙箱桌面，不切过去读到的是"假鼠标"
        self.drag_monitor_stop.clear()
        last = None
        last_change = started
        self.drag_changes = 0
        self.drag_max_gap_ms = 0.0
        while not self.drag_monitor_stop.is_set():
            cursor = wt.POINT()
            user32.GetCursorPos(ctypes.byref(cursor))
            now = time.time()
            self.drag_samples += 1
            if last is not None and (cursor.x != last[0] or cursor.y != last[1]):
                self.drag_changes += 1
                gap = (now - last_change) * 1000.0
                if gap > self.drag_max_gap_ms:
                    self.drag_max_gap_ms = gap
                last_change = now
            last = (cursor.x, cursor.y)
            time.sleep(0.008)

    # -- 右键菜单（原生菜单，不受透明窗口影响） --------------------------------

    def remember_card_open(self) -> None:
        """把"进度卡片开着没"记进 pet_settings.json：重启后按上次的状态显示（2026-10-11 加）。"""
        try:
            self.settings["cardOpen"] = bool(self.inline_card_open)
            settings_store.save(self.settings)
        except Exception as exc:  # noqa: BLE001
            log("保存进度卡片状态失败：%r" % (exc,))

    def toggle_card(self, show=None) -> None:
        """任务进度卡片。

        固定画在桌宠自己的网页里：宽度 CSS 固定、高度由内容自适应，
        窗口尺寸完全不变 —— 既没有改窗口尺寸的卡顿，也不会影响穿透。
        """
        mode = "inline"
        if mode != "window":
            self.inline_card_open = (not self.inline_card_open) if show is None else bool(show)
            self.post({"t": "card", "open": self.inline_card_open})
            self.remember_card_open()    # 记下来：下次启动还按这个状态显示
            log("进度卡片（窗口内渲染）：%s" % ("显示" if self.inline_card_open else "隐藏"))
            return
        if self.card is None or self.card.IsDisposed:
            self.card_want_visible = (not self.card_want_visible) if show is None else bool(show)
            self.spawn_window_thread("card-window", self.build_card)   # 同样跑在自己的 STA 线程上
            return
        target = (not self.card.Visible) if show is None else bool(show)
        self.invoke_on_card(lambda: self.show_card_window(target))

    def refresh_card_now(self) -> None:
        """右键菜单里的“刷新卡片信息”：立刻重读一次状态并推给卡片。

        平时是 state_loop 每秒自己刷、内容没变就不重发；这个是“用户主动点一下，
        就要马上看到最新”用的 —— 直接读一份推过去，不等那一秒，也不再做去重。
        只刷新数据，不重新载入模型（那是“重新载入形象”的事）。
        """
        try:
            import agent_state

            # 卡片刷新必须读取设置里选中的 agent。以前这里硬编码 Codex，
            # 所以切到 Claude/Gemini 等 agent 后点刷新会先闪一下 Codex 信息，
            # 等下一秒的 state_loop 才换成正确 agent。
            agent_id = str((self.settings or {}).get("agent") or "codex")
            payload = agent_state.snapshot(agent_id)
            payload["usage"] = usage_line(self)
            self.last_payload = payload
            if not self.inline_card_open:
                # 卡片关着就先打开：不然点了“刷新”什么都没看见，会以为坏了
                self.inline_card_open = True
                self.post({"t": "card", "open": True})
                self.remember_card_open()
            self.post({"t": "state", "data": payload})
            self.schedule_card_update(payload)
            log("刷新卡片信息：%s / %s" % (payload.get("state"), payload.get("title") or "（无进行中的任务）"))
        except Exception as exc:  # noqa: BLE001
            log("刷新卡片信息失败：%r" % (exc,))

    def invoke_on_card(self, fn) -> None:
        """卡片窗口在它自己的线程上，跨线程操作必须回到那个线程。"""
        form = self.card
        if form is None or form.IsDisposed:
            return
        try:
            from System import Action

            form.BeginInvoke(Action(fn))
        except Exception as exc:  # noqa: BLE001
            log("操作进度卡片窗口失败：%r" % (exc,))

    def show_card_window(self, visible: bool) -> None:
        if self.card is None or self.card.IsDisposed:
            return
        self.apply_card_mode()
        if visible:
            self.update_card(self.last_payload)
            self.position_card()
            self.card.Show()
            self.card.BringToFront()
        else:
            self.card.Hide()
        log("进度卡片（独立窗口）：%s" % ("显示" if visible else "隐藏"))

    def build_card(self) -> None:
        """进度卡片 = 独立窗口，里面还是原来那张 HTML 卡片（好看，且不会改变桌宠尺寸）。"""
        from System.Drawing import Color, Size
        from System.Windows.Forms import DockStyle, Form, FormBorderStyle, FormStartPosition
        from Microsoft.Web.WebView2.WinForms import WebView2

        form = Form()
        form.Text = "任务进度"
        form.FormBorderStyle = getattr(FormBorderStyle, "None")
        form.StartPosition = FormStartPosition.Manual
        form.ShowInTaskbar = False
        form.TopMost = True
        # 关键：窗口背景必须"挖空"，否则毛玻璃/桌面都被它盖住。
        # 键色故意选一个很暗、内容里不会精确出现的颜色：这样即使某些驱动不认
        # 子窗口的 alpha，最差也只是"更暗的卡片"，不会出现洋红边或者镂空。
        key_color = Color.FromArgb(8, 8, 8)
        form.BackColor = key_color
        form.TransparencyKey = key_color
        scale = self.dpi_scale or 1.0
        compact = self.pet_scale < CARD_COMPACT_BELOW
        self.card_compact = compact
        base_w = CARD_COMPACT_W if compact else CARD_W
        base_h = CARD_COMPACT_H if compact else CARD_H
        form.ClientSize = Size(int(round(base_w * scale)), int(round(base_h * scale)))
        self.apply_card_region(form.ClientSize.Width, form.ClientSize.Height)

        webview = WebView2()
        webview.CreationProperties = self.create_props()
        webview.Dock = DockStyle.Fill
        try:
            webview.DefaultBackgroundColor = Color.Transparent   # 让网页的半透明背景透出桌面/毛玻璃
        except Exception:  # noqa: BLE001
            pass
        form.Controls.Add(webview)
        webview.CoreWebView2InitializationCompleted += self.on_card_ready
        webview.WebMessageReceived += self.on_card_message
        webview.EnsureCoreWebView2Async(None)

        self.card = form
        self.card_webview = webview
        form.FormClosed += lambda s, e: setattr(self, "card", None)
        form.Shown += self.on_card_shown
        if getattr(self, "card_want_visible", True):
            self.show_card_window(True)
        return form

    def on_card_shown(self, sender, args) -> None:
        """卡片窗口刚显示时：搬到你当前的虚拟桌面 + 开毛玻璃。"""
        self.enable_acrylic(self.card)
        if self.desktops is not None and self.card is not None and self.form is not None:
            self.desktops.align_to(int(self.card.Handle.ToInt64()), int(self.form.Handle.ToInt64()))

    def enable_acrylic(self, form) -> None:
        """给卡片窗口开毛玻璃，按"成功率从高到低"依次尝试，并把结果写进日志。

        注意：系统设置里如果关掉了"透明效果"，下面几种方式都会**静默失效**，
        所以先读注册表判断，避免让你以为是代码没生效。
        """
        if not self.settings.get("cardBlur", True):
            try:   # 关掉时要主动清掉已经生效的模糊，否则只是"新开的不加"
                accent = ACCENT_POLICY(0, 0, 0, 0)   # ACCENT_DISABLED
                data = WINDOWCOMPOSITIONATTRIBDATA(
                    19, ctypes.cast(ctypes.byref(accent), ctypes.c_void_p), ctypes.sizeof(accent)
                )
                user32.SetWindowCompositionAttribute(ctypes.c_void_p(int(form.Handle.ToInt64())), ctypes.byref(data))
            except Exception:
                pass
            log("卡片毛玻璃：设置里已关闭（只保留半透明，圆角绝对干净）")
            return
        if not self.system_transparency_enabled():
            log(
                "卡片毛玻璃：系统关闭了透明效果，所以看不到模糊 —— "
                "到「设置 → 个性化 → 颜色 → 透明效果」打开即可（卡片本身不受影响）"
            )
        class ACCENT_POLICY(ctypes.Structure):
            _fields_ = [
                ("AccentState", ctypes.c_int),
                ("AccentFlags", ctypes.c_int),
                ("GradientColor", ctypes.c_uint),
                ("AnimationId", ctypes.c_int),
            ]

        class WINDOWCOMPOSITIONATTRIBDATA(ctypes.Structure):
            _fields_ = [
                ("Attribute", ctypes.c_int),
                ("Data", ctypes.c_void_p),
                ("SizeOfData", ctypes.c_size_t),
            ]

        hwnd = ctypes.c_void_p(int(form.Handle.ToInt64()))
        ACCENT_ENABLE_ACRYLICBLURBEHIND = 4
        ACCENT_ENABLE_BLURBEHIND = 3
        WCA_ACCENT_POLICY = 19
        # 色调由网页那层 rgba(25,28,36,0.62) 决定，这里只给一点点底色（alpha 0x30），
        # 免得两层叠起来变成一块死黑的板子。
        tint = (0x30 << 24) | (36 << 16) | (28 << 8) | 25

        # 注意：这里**故意不用** Win11 的 DWMSBT 背景材质（DwmSetWindowAttribute 38 / DwmExtendFrameIntoClientArea）。
        # 它是按"整块窗口矩形"画的，不认窗口的圆角 Region —— 圆角外面会留一圈方形底色，
        # 也就是之前四个角上的那种残留。改用 SetWindowCompositionAttribute：
        # 这类模糊是跟着窗口形状（Region）裁的，圆角外面的模糊会被切掉。
        for state, label in ((ACCENT_ENABLE_ACRYLICBLURBEHIND, "亚克力"), (ACCENT_ENABLE_BLURBEHIND, "模糊")):
            try:
                accent = ACCENT_POLICY(state, 2, tint, 0)
                data = WINDOWCOMPOSITIONATTRIBDATA(
                    WCA_ACCENT_POLICY,
                    ctypes.cast(ctypes.byref(accent), ctypes.c_void_p),
                    ctypes.sizeof(accent),
                )
                ok = user32.SetWindowCompositionAttribute(hwnd, ctypes.byref(data))
                if ok:
                    log("卡片毛玻璃：已开启（%s，旧接口）" % label)
                    return
                log("卡片毛玻璃：%s 接口返回失败" % label)
            except Exception as exc:  # noqa: BLE001
                log("卡片毛玻璃：%s 接口异常 %r" % (label, exc))
        log("卡片毛玻璃：所有方式都没生效，卡片维持半透明实色（不影响使用）")

    def system_transparency_enabled(self) -> bool:
        try:
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            )
            value, _ = winreg.QueryValueEx(key, "EnableTransparency")
            return bool(value)
        except Exception:
            return True

    def apply_card_region(self, width: int, height: int) -> None:
        """给窗口本身做圆角：先试 Win11 原生的 DWM 圆角，再用 Region 兜底。

        两条一起用也没问题（Region 是更紧的形状），但系统圆角对 DWM 画的毛玻璃
        裁剪更可靠，所以优先启用。
        """
        try:
            dwm = ctypes.WinDLL("dwmapi")
            DWMWA_WINDOW_CORNER_PREFERENCE = 33
            DWMWCP_ROUND = 2
            value = ctypes.c_int(DWMWCP_ROUND)
            hr = dwm.DwmSetWindowAttribute(
                ctypes.c_void_p(int(self.card.Handle.ToInt64())),
                DWMWA_WINDOW_CORNER_PREFERENCE,
                ctypes.byref(value),
                ctypes.sizeof(value),
            )
            if hr == 0:
                log("卡片圆角：已启用 Win11 系统圆角")
            else:
                log("卡片圆角：系统圆角接口返回 0x%08X，改用 Region 裁剪" % (hr & 0xFFFFFFFF))
        except Exception as exc:  # noqa: BLE001
            log("卡片圆角：系统圆角不可用（%r），改用 Region 裁剪" % (exc,))
        try:
            # 直接用 .NET 画一个圆角路径当窗口区域（比 P/Invoke 的 FromHrgn 稳）
            from System.Drawing import Region
            from System.Drawing.Drawing2D import GraphicsPath

            radius = int(round(CARD_RADIUS * (self.dpi_scale or 1.0)))
            radius = max(2, min(radius, min(width, height) // 2))
            path = GraphicsPath()
            path.AddArc(0, 0, radius, radius, 180, 90)
            path.AddArc(width - radius, 0, radius, radius, 270, 90)
            path.AddArc(width - radius, height - radius, radius, radius, 0, 90)
            path.AddArc(0, height - radius, radius, radius, 90, 90)
            path.CloseFigure()
            self.card.Region = Region(path)
        except Exception as exc:  # noqa: BLE001
            log("设置卡片圆角失败：%r" % (exc,))

    def apply_card_mode(self) -> None:
        """桌宠太小就切紧凑卡片；尺寸变化时重算窗口大小与圆角。"""
        if self.card is None or self.card.IsDisposed:
            return
        compact = self.pet_scale < CARD_COMPACT_BELOW
        scale = self.dpi_scale or 1.0
        base_w = CARD_COMPACT_W if compact else CARD_W
        base_h = CARD_COMPACT_H if compact else CARD_H
        width = int(round(base_w * scale))
        height = int(round(base_h * scale))
        if self.card.ClientSize.Width != width or self.card.ClientSize.Height != height:
            self.card.ClientSize = self.Size(width, height)
            self.apply_card_region(width, height)
        if compact != getattr(self, "card_compact", None):
            self.card_compact = compact
            webview = getattr(self, "card_webview", None)
            if webview is not None and webview.CoreWebView2 is not None:
                try:
                    webview.CoreWebView2.PostWebMessageAsJson(
                        json.dumps({"t": "compact", "value": compact})
                    )
                except Exception as exc:  # noqa: BLE001
                    log("切换卡片紧凑模式失败：%r" % (exc,))

    def on_card_ready(self, sender, args) -> None:
        core = sender.CoreWebView2
        if core is None:
            log("进度卡片窗口初始化失败：%s" % args.InitializationException)
            return
        core.Settings.AreDefaultContextMenusEnabled = False
        core.Navigate("http://127.0.0.1:%d/card.html" % self.port)
        log("进度卡片窗口就绪")
        if getattr(self, "card_compact", False):
            try:
                core.PostWebMessageAsJson(json.dumps({"t": "compact", "value": True}))
            except Exception:
                pass

    def on_card_message(self, sender, args) -> None:
        try:
            msg = json.loads(args.WebMessageAsJson)
        except Exception:
            return
        if msg.get("t") == "close-card":
            self.toggle_card(False)
        elif msg.get("t") == "hello":
            self.schedule_card_update(self.last_payload or {})

    def update_card(self, payload: dict | None) -> None:
        if self.card is None or self.card.IsDisposed:
            return
        webview = getattr(self, "card_webview", None)
        if webview is None or webview.CoreWebView2 is None:
            return
        try:
            from System import Action

            message = json.dumps({"t": "state", "data": payload or {}}, ensure_ascii=False)
            core = webview.CoreWebView2
            if core is not None:
                core.PostWebMessageAsJson(message)
        except Exception as exc:  # noqa: BLE001
            log("更新进度卡片失败：%r" % (exc,))

    def position_card(self) -> None:
        if self.card is None or self.card.IsDisposed:
            return
        from System.Windows.Forms import Screen

        area = Screen.FromControl(self.form).WorkingArea
        width = self.card.ClientSize.Width
        height = self.card.ClientSize.Height
        pet = (self.form.Left, self.form.Top, self.form.ClientSize.Width, self.form.ClientSize.Height)
        area_box = (area.Left, area.Top, area.Right, area.Bottom)
        # 模型在窗口里是底部对齐的，头顶上方还有一段空白。这里不光用"模型外框顶部"，
        # 还往下压 18% 模型高度 —— 也就是允许卡片压住头顶那圈发丝/特效，只要不挡脸。
        if self.char_rect:
            head_offset = card_head_offset(self.char_rect)
        else:
            head_offset = 0
        x, y = compute_card_position(pet, (width, height), area_box, CARD_GAP, head_offset)
        self.card.Location = self.Point(int(x), int(y))
        self.last_card_pos = pet

    def schedule_card_update(self, payload: dict) -> None:
        if self.card is None or self.card.IsDisposed or not self.card.Visible:
            return
        self.invoke_on_card(lambda: self.update_card(payload))

    def show_menu(self, screen_x: int, screen_y: int) -> None:
        from System.Windows.Forms import ContextMenuStrip, ToolStripSeparator, ToolStripMenuItem

        if self.menu is None or self.menu.IsDisposed:
            menu = ContextMenuStrip()
            items = [
                ("显示 / 隐藏任务进度", self.toggle_card),
                ("刷新卡片信息", self.refresh_card_now),
                ("摸头", lambda: self.post({"t": "react", "kind": "pat"})),
                ("换个表情", lambda: self.post({"t": "cycle-expression"})),
                ("随机动作", lambda: self.post({"t": "react", "kind": "motion"})),
                ("-", None),
                ("设置…", self.show_settings),
                ("切到 Agent 窗口", self.focus_agent_window),
                ("切换卡片 Agent", self.open_agent_picker),
                ("重新载入形象", self.reload_page),
                ("重画窗口（缩放后出现黑边时点一下）", self.repaint_now),
                ("退出桌宠", lambda: self.form.Close()),
            ]
            for label, handler in items:
                if label == "-":
                    menu.Items.Add(ToolStripSeparator())
                    continue
                item = ToolStripMenuItem(label)

                def on_item_click(sender, args, h=handler):
                    # 记下"是点菜单项关的"：诊断菜单生命周期用
                    self._menu_close_reason = "点选了菜单项"
                    h()

                item.Click += on_item_click
                menu.Items.Add(item)
            menu.Closed += self.on_menu_closed
            self.menu = menu
        if self.menu.Visible:
            # 菜单还开着又收到"弹出"请求（右键连点、或状态不同步）：在一个正显示着的
            # 弹出菜单上再次 Show，会把它弄成"画着但悬停不亮、要先去别处点一下才恢复"
            # 的幽灵状态（2026-10-12 用户报告的 bug）。先收掉，下一次 tick 再弹干净的。
            try:
                self.menu.Close()
            except Exception:  # noqa: BLE001
                pass
            self._pending_menu_at = (screen_x, screen_y)
            log("菜单还开着，先收掉再重弹")
            return
        try:
            # 菜单文字跟着当前选中的 agent 走：切到 Hermes CLI 窗口 / 切到 Codex 窗口 …
            label = "切到 %s 窗口" % self.agent_display_name()
            for item in self.menu.Items:
                item_text = str(getattr(item, "Text", "") or "")
                if item_text.startswith("切到 ") and item_text.endswith("窗口"):
                    item.Text = label
        except Exception:  # noqa: BLE001
            pass
        self.menu.Show(self.Point(screen_x, screen_y))
        self._menu_shown_at = time.time()
        self._menu_close_at = 0.0        # 新菜单：把上一次遗留的关闭请求作废
        self._menu_visible = True
        self._menu_ghost_since = None
        self._menu_rect = self.menu_screen_rect()
        if self._menu_rect:
            # 记下菜单真正落下的位置：点里面的「切换卡片 Agent」时，面板要顶在同一个地方
            self._menu_pos = (self._menu_rect[0], self._menu_rect[1])
        else:
            self._menu_pos = (screen_x, screen_y)
        log("弹出右键菜单")

    def on_menu_closed(self, sender, args) -> None:
        self._menu_visible = False
        self._menu_rect = None
        self._menu_close_at = 0.0
        self._menu_ghost_since = None
        # 关键：点菜单项会让菜单**立刻**关闭，等 33ms 后轮询再跑时菜单已经不可见了 ——
        # 只靠 _menu_visible 挡不住，所以关菜单时统一开一小段忽略窗口，
        # 把"点菜单的那一下"整段吃掉（含按下、松开），不让它落到下面的图标按钮上。
        self._ignore_click_until = time.time() + 0.35
        alive = time.time() - self._menu_shown_at if self._menu_shown_at else 0.0
        reason = self._menu_close_reason or "外部点击 / 系统关闭"
        self._menu_close_reason = ""
        # 每次关闭都记一笔（含原因和存活时长）：排查"悬停不亮"这类生命周期问题用
        log("右键菜单已关闭（%s，存活 %.2fs%s）" % (reason, alive, "，很快就关了" if 0 < alive < 0.6 else ""))

    def menu_screen_rect(self):
        """菜单当前的屏幕矩形（菜单是顶层弹出窗口，Bounds 就是屏幕坐标）。"""
        try:
            if self.menu is None or self.menu.IsDisposed or not self.menu.Visible:
                return None
            bounds = self.menu.Bounds
            return (bounds.Left, bounds.Top, bounds.Right, bounds.Bottom)
        except Exception:  # noqa: BLE001
            return None

    def on_cursor_tick(self, sender, args) -> None:
        """视线的输入就是鼠标位置：~30Hz 送一次，而且当场 flush 出去，不再等下一个 110ms 的 tick。

        （on_tick 也在调 stream_cursor，里面的最小间隔判断会挡住重复的那一次。）
        """
        # 钩子吃掉的右键：菜单在这里弹（33ms 一次，比 110ms 的 tick 跟手）
        if self._pending_menu_at:
            menu_xy, self._pending_menu_at = self._pending_menu_at, None
            self.show_menu(menu_xy[0], menu_xy[1])   # 每次右键都弹，连点也不漏
        self.stream_cursor(time.time())
        self.handle_mouse()      # 鼠标判定也走 33ms：只靠 110ms 那个 tick 会慢一拍
        self.flush()

    def stream_cursor(self, now: float) -> None:
        """Feed the page the real mouse position so the eyes can follow it everywhere on screen."""
        if now - self.last_cursor_send < 0.03:
            return
        cursor = wt.POINT()
        user32.GetCursorPos(ctypes.byref(cursor))
        # 用网页自己报的 dpr：画布和 CSS 的换算就是按它来的。
        # 以前这里用的是窗口 DPI（dpi_scale），两者只要不一致，像素探测就会采样到偏掉的点。
        scale = self.web_dpr or self.dpi_scale or 1.0
        local_x = (cursor.x - self.form.Left) / scale
        local_y = (cursor.y - self.form.Top) / scale
        inside = 0 <= local_x <= self.form.ClientSize.Width / scale and 0 <= local_y <= self.form.ClientSize.Height / scale
        moved = self.last_cursor is None or abs(cursor.x - self.last_cursor[0]) + abs(cursor.y - self.last_cursor[1]) > 1
        if not moved and inside == self.last_cursor[2]:
            return
        self.last_cursor = (cursor.x, cursor.y, inside)
        self.last_cursor_send = now
        self.post({"t": "cursor", "data": {"x": round(local_x, 1), "y": round(local_y, 1), "inside": inside}})

    def resize_window(self, w: float, h: float) -> None:
        """窗口贴着角色走。默认右下角固定；缩放桌宠时以窗口中心为锚点，避免"飞走"。"""
        scale = self.dpi_scale * self.pet_scale
        new_w = int(round(w * scale))
        new_h = int(round(h * scale))
        if getattr(self, "Size", None) is None:
            return
        self.ui_phase("调整窗口大小")
        old = self.form.ClientSize
        if abs(old.Width - new_w) < 3 and abs(old.Height - new_h) < 3:
            return
        before = (self.form.Left, self.form.Top, old.Width, old.Height)
        center_x = self.form.Left + old.Width / 2.0
        center_y = self.form.Top + old.Height / 2.0
        anchor = getattr(self, "resize_anchor", "center")
        self.form.ClientSize = self.Size(new_w, new_h)
        if anchor == "bottom":
            right = self.form.Left + old.Width
            bottom = self.form.Top + old.Height
            self.form.Location = self.Point(right - new_w, bottom - new_h)
        else:
            self.form.Location = self.Point(int(center_x - new_w / 2.0), int(center_y - new_h / 2.0))
        log("窗口调整为 %dx%d" % (new_w, new_h))
        after = (self.form.Left, self.form.Top, self.form.ClientSize.Width, self.form.ClientSize.Height)
        self.schedule_repair_repaint(before, after)

    # -- 缩放后的重绘补救（Win11 黑边） ---------------------------------------
    #
    # 现象：滚轮/滑条把桌宠放大缩小时，偶尔会在窗口原来占的地方留一块黑色，
    # 一直挂着不消失（拖一下窗口、或者切一下别的窗口才会掉）。
    #
    # 原因：这个窗口是"整块挖空背景"的透明分层窗口（TransparencyKey + WebView2 透明
    # 背景），Win11 在它改尺寸之后不保证把"旧的那块"和"新露出来的那条边"重画一遍，
    # 于是旧像素就留在屏幕上了。
    #
    # 处理：尺寸改完之后，短时间内连着重画几次 ——
    #   1) 让窗口和里面的网页自己重画一遍；
    #   2) 用 SetWindowPos 催一次 DWM 重新合成；
    #   3) 把"这次缩放涉及的屏幕区域"（旧窗口 ∪ 新窗口）标脏，让桌面也重画那一块。
    # 全都是重绘动作，不改任何功能，也不动鼠标/拖拽/视线的逻辑。

    def schedule_repair_repaint(self, before, after) -> None:
        """记下这次缩放涉及的区域，并安排一串重画。"""
        try:
            now = time.time()
            rect = (
                min(before[0], after[0]),
                min(before[1], after[1]),
                max(before[0] + before[2], after[0] + after[2]),
                max(before[1] + before[3], after[1] + after[3]),
            )
            if self.repair_rect is None or now - self.repair_rect_at > REPAIR_KEEP_SECONDS:
                self.repair_rect = rect
            else:
                prev = self.repair_rect
                self.repair_rect = (
                    min(prev[0], rect[0]), min(prev[1], rect[1]),
                    max(prev[2], rect[2]), max(prev[3], rect[3]),
                )
            self.repair_rect_at = now
            schedule, nudge_ok = repair_schedule()
            self.repair_left = list(schedule)
            self.repair_total = len(schedule)
            self.repair_nudge = nudge_ok
            self.start_repair_timer()
        except Exception as exc:  # noqa: BLE001
            log("安排重画失败（不影响使用）：%r" % (exc,))

    def start_repair_timer(self) -> None:
        if self.repair_timer is None or not self.repair_left:
            return
        self.repair_timer.Stop()
        self.repair_timer.Interval = self.repair_left[0]
        self.repair_timer.Start()

    def on_repair_timer(self, sender, args) -> None:
        if self.repair_timer is not None:
            self.repair_timer.Stop()
        if not self.repair_left:
            return
        self.repair_left.pop(0)
        first = len(self.repair_left) == self.repair_total - 1
        last = not self.repair_left
        # 第一轮先把"窗口原来占的那块桌面"也叫醒；最后两轮再各叫一次（黑边有时出现得晚），
        # 最后一轮顺便把窗口抖一下。
        self.force_repaint(desktop=first or last or len(self.repair_left) == 1, nudge=last and self.repair_nudge)
        self.start_repair_timer()

    def repaint_now(self) -> None:
        """手动重画：万一缩放后还留着黑边，右键菜单点这一条。"""
        form = getattr(self, "form", None)
        if form is None or form.IsDisposed:
            return
        rect = (form.Left, form.Top, form.ClientSize.Width, form.ClientSize.Height)
        self.schedule_repair_repaint(rect, rect)
        log("手动重画窗口（清理黑边）")

    def force_repaint(self, desktop: bool = True, nudge: bool = False) -> None:
        """把桌宠窗口重画一遍；desktop=True 时连"窗口原来占的那块桌面"一起叫醒。

        nudge=True 时还会把窗口位置左右抖 1px 再抖回来 —— 这一下在 DWM 看来是
        一次真实的移动，会强制它把这块彻底重新合成，专门治那种"重画了也不掉"的黑边。
        """
        form = getattr(self, "form", None)
        if form is None or form.IsDisposed:
            return
        try:
            form.Invalidate(True)
            form.Update()
        except Exception:
            pass
        webview = getattr(self, "webview", None)
        if webview is not None:
            try:
                webview.Invalidate()
                webview.Update()
            except Exception:
                pass
        try:
            user32.SetWindowPos(
                ctypes.c_void_p(int(form.Handle.ToInt64())),
                None, 0, 0, 0, 0,
                SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE,
            )
        except Exception:
            pass
        rect = self.repair_rect if desktop else None
        if rect is not None:
            try:
                r = wt.RECT(int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3]))
                user32.InvalidateRect(None, ctypes.byref(r), False)
            except Exception:
                pass
        if nudge:
            try:
                hwnd = ctypes.c_void_p(int(form.Handle.ToInt64()))
                x, y = form.Left, form.Top
                flags = SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE
                user32.SetWindowPos(hwnd, None, x + 1, y + 1, 0, 0, flags)
                user32.SetWindowPos(hwnd, None, x, y, 0, 0, flags)
            except Exception:
                pass

    # -- bottom tool buttons -------------------------------------------------

    def trigger_bottom_action(self, button: str, event: str, cursor) -> None:
        self.release_mouse_capture()
        mode = self.settings.get("bottomButtonsMode", "assistant")
        actions = self.settings.get("bottomButtonActions") or {}
        action = ((actions.get(button) or {}).get(event) or "none")
        if action == "none":
            self.post({"t": "say", "text": "%s：未配置功能" % TOOL_LABELS.get(button, button)})
            return
        label = TOOL_ACTION_LABELS.get(action, action)
        if not self.confirm_action(label, TOOL_LABELS.get(button, button)):
            log("取消底部按钮操作：%s / %s" % (button, label))
            return
        self.last_bottom_action = {"button": button, "event": event, "action": action, "at": time.time()}
        log("底部按钮确认执行：%s / %s / %s" % (button, event, label))
        self.execute_bottom_action(action, cursor)

    def execute_bottom_action(self, action: str, cursor) -> None:
        if action == "open-settings":
            self.show_settings()
        elif action == "open-menu":
            self.show_menu(cursor.x, cursor.y)
        elif action == "toggle-edit-mode":
            self.edit_mode = not self.edit_mode
            self.post({"t": "say", "text": "编辑模式：" + ("开启" if self.edit_mode else "关闭")})
        elif action == "toggle-position-lock":
            self.position_locked = not self.position_locked
            self.post({"t": "say", "text": "位置：" + ("已锁定" if self.position_locked else "已解锁")})
        elif action == "reset-default":
            self.settings["scale"] = 1.0
            self.settings["opacity"] = 1.0
            settings_store.save(self.settings)
            self.invoke_on_pet(self.apply_settings_ui)
            self.reset_position()
            self.post({"t": "say", "text": "已恢复默认状态"})
        elif action == "undo-expression":
            self.post({"t": "button-page-action", "action": "undo-expression"})
        elif action == "clear-card":
            self.post({"t": "button-page-action", "action": "clear-card"})
        elif action == "hide-pet":
            self.hide_to_tray()
        elif action == "mute-animations":
            self.post({"t": "mute", "seconds": 300})
            self.post({"t": "say", "text": "动画已静音 5 分钟"})
        elif action == "codex-focus":
            self.focus_agent_window()       # 底部按钮那个动作：跟当前 agent 走
        elif action == "codex-new-session":
            if self.focus_codex():
                self.send_key_chord("CTRL", "N")
        elif action == "codex-rename-task":
            value = self.prompt_text("重命名当前任务（复制新名称）", "输入新的任务名称（会复制到剪贴板）")
            if value:
                self.set_clipboard(value)
                self.focus_codex()
                self.post({"t": "say", "text": "新任务名已复制"})
        elif action == "codex-add-note":
            value = self.prompt_text("添加备注", "输入要放进 Codex 输入框的备注")
            if value:
                self.focus_codex()
                self.set_clipboard("备注：" + value)
                self.send_key_chord("CTRL", "V")
        elif action == "codex-interrupt":
            if self.focus_codex():
                self.send_key_chord("ESC")
        elif action == "codex-undo-question":
            if self.focus_codex():
                self.send_key_chord("CTRL", "Z")
        elif action == "codex-clear-context":
            if self.focus_codex():
                self.send_key_chord("CTRL", "N")
        elif action == "codex-end-session":
            hwnd = self.focus_codex()
            if hwnd:
                user32.PostMessageW(wt.HWND(hwnd), 0x0010, 0, 0)

    def confirm_action(self, action_label: str, button_label: str) -> bool:
        from System.Drawing import Font, Size
        from System.Windows.Forms import (
            AutoScaleMode,
            Button,
            DialogResult,
            Form,
            FormBorderStyle,
            FormStartPosition,
            Label,
        )

        # 原来的 430x155 + 默认 9pt 字太小（高 DPI 屏上尤其糊）。
        # 这里按窗口 DPI 放大布局，字号提到 12pt（pt 会随 DPI 自动放大），
        # AutoScaleMode 设成 None —— 尺寸已经按 DPI 给足，别再被自动缩放叠一次。
        scale = float(getattr(self, "dpi_scale", 1.0) or 1.0)

        def px(value: float) -> int:
            return int(round(value * scale))

        form = Form()
        form.Text = "确认底部按钮操作"
        form.ClientSize = Size(px(620), px(250))
        form.StartPosition = FormStartPosition.CenterScreen
        form.TopMost = True
        form.FormBorderStyle = FormBorderStyle.FixedDialog
        form.MaximizeBox = False
        form.MinimizeBox = False
        form.ControlBox = False
        form.ShowInTaskbar = False
        form.AutoScaleMode = getattr(AutoScaleMode, "None")

        ui_font = Font("Microsoft YaHei UI", 16.0)      # 想再大/再小就改这一个数字

        label = Label()
        label.Font = ui_font
        label.Text = "确定要执行“%s”吗？\n\n按钮：%s" % (action_label, button_label)
        label.SetBounds(px(34), px(24), px(552), px(120))
        confirm = Button()
        confirm.Font = ui_font
        confirm.Text = "确认"
        confirm.DialogResult = DialogResult.OK
        confirm.SetBounds(px(300), px(184), px(140), px(48))
        cancel = Button()
        cancel.Font = ui_font
        cancel.Text = "取消"
        cancel.DialogResult = DialogResult.Cancel
        cancel.SetBounds(px(456), px(184), px(140), px(48))
        form.Controls.AddRange([label, confirm, cancel])
        form.AcceptButton = confirm
        form.CancelButton = cancel

        def on_closing(sender, args):
            if form.DialogResult.ToString() == "None":
                form.DialogResult = DialogResult.Cancel

        form.FormClosing += on_closing
        try:
            return form.ShowDialog() == DialogResult.OK
        finally:
            form.Dispose()

    def prompt_text(self, title: str, prompt: str) -> str:
        from System.Drawing import Font, Size
        from System.Windows.Forms import (
            AutoScaleMode,
            Button,
            DialogResult,
            Form,
            FormBorderStyle,
            FormStartPosition,
            Label,
            TextBox,
        )

        # 跟"确认底部按钮操作"同一套放大规则：按 DPI 放大布局 + 12pt 字号
        scale = float(getattr(self, "dpi_scale", 1.0) or 1.0)

        def px(value: float) -> int:
            return int(round(value * scale))

        form = Form()
        form.Text = title
        form.ClientSize = Size(px(620), px(258))
        form.StartPosition = FormStartPosition.CenterScreen
        form.TopMost = True
        form.FormBorderStyle = FormBorderStyle.FixedDialog
        form.MaximizeBox = False
        form.MinimizeBox = False
        form.ControlBox = False
        form.ShowInTaskbar = False
        form.AutoScaleMode = getattr(AutoScaleMode, "None")

        ui_font = Font("Microsoft YaHei UI", 16.0)

        label = Label()
        label.Font = ui_font
        label.Text = prompt
        label.SetBounds(px(34), px(24), px(552), px(44))
        box = TextBox()
        box.Font = ui_font
        box.SetBounds(px(34), px(76), px(552), px(52))
        ok = Button()
        ok.Font = ui_font
        ok.Text = "确定"
        ok.DialogResult = DialogResult.OK
        ok.SetBounds(px(300), px(192), px(140), px(48))
        cancel = Button()
        cancel.Font = ui_font
        cancel.Text = "取消"
        cancel.DialogResult = DialogResult.Cancel
        cancel.SetBounds(px(456), px(192), px(140), px(48))
        form.Controls.AddRange([label, box, ok, cancel])
        form.AcceptButton = ok
        form.CancelButton = cancel

        def on_closing(sender, args):
            if form.DialogResult.ToString() == "None":
                form.DialogResult = DialogResult.Cancel

        form.FormClosing += on_closing
        try:
            if form.ShowDialog() == DialogResult.OK:
                return box.Text.strip()
        finally:
            form.Dispose()
        return ""

    def set_clipboard(self, text: str) -> None:
        try:
            from System.Windows.Forms import Clipboard

            Clipboard.SetText(text)
        except Exception as exc:  # noqa: BLE001
            log("写入剪贴板失败：%r" % (exc,))

    def send_key_chord(self, *keys: str) -> None:
        vk = {
            "CTRL": 0x11, "SHIFT": 0x10, "ALT": 0x12, "ESC": 0x1B,
            "N": 0x4E, "V": 0x56, "Z": 0x5A,
        }
        codes = [vk[key.upper()] for key in keys if key.upper() in vk]
        for code in codes:
            user32.keybd_event(code, 0, 0, 0)
        for code in reversed(codes):
            user32.keybd_event(code, 0, 2, 0)  # KEYEVENTF_KEYUP

    def release_mouse_capture(self) -> None:
        try:
            self.form.Capture = False
            for control in self.form.Controls:
                control.Capture = False
        except Exception:
            pass

    def hide_to_tray(self) -> None:
        if self.tray_icon is None:
            try:
                from System.Drawing import Icon
                from System.Windows.Forms import ContextMenuStrip, NotifyIcon, ToolStripMenuItem

                tray = NotifyIcon()
                tray.Icon = Icon(os.path.join(WEB, "icon.ico"))
                tray.Text = "Agent 桌宠"
                menu = ContextMenuStrip()
                show_item = ToolStripMenuItem("显示桌宠")
                quit_item = ToolStripMenuItem("退出桌宠")
                show_item.Click += lambda sender, args: self.show_from_tray()
                quit_item.Click += lambda sender, args: self.form.Close()
                menu.Items.Add(show_item)
                menu.Items.Add(quit_item)
                tray.ContextMenuStrip = menu
                tray.Click += lambda sender, args: self.show_from_tray()
                tray.DoubleClick += lambda sender, args: self.show_from_tray()
                self.tray_icon = tray
            except Exception as exc:  # noqa: BLE001
                log("创建系统托盘图标失败：%r" % (exc,))
                return
        self.form.Hide()
        self.tray_icon.Visible = True
        log("桌宠已隐藏到系统托盘")

    def show_from_tray(self) -> None:
        if self.tray_icon is not None:
            self.tray_icon.Visible = False
        self.form.Show()
        self.form.Activate()
        log("桌宠已从系统托盘恢复")

    def reset_position(self) -> None:
        rect = self.screen_area()
        self.form.Location = self.Point(rect[2] - self.form.ClientSize.Width - 24, rect[3] - self.form.ClientSize.Height - 8)
        save_position(self.form.Left, self.form.Top)

    # -- misc ----------------------------------------------------------------

    def focus_codex(self) -> int:
        """Bring the Codex window forward.

        The desktop app ships as the packaged "ChatGPT.exe" under OpenAI.Codex, and its
        window title is just "ChatGPT" — so match on the process image, not the title.
        """
        me = int(self.form.Handle.ToInt64())
        found = []

        @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        def visit(hwnd, _):
            if int(hwnd) == me or not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value
            image = self.process_image(hwnd).lower()
            title_l = title.lower()
            score = 0
            if "openai.codex" in image or "\\codex.exe" in image:
                score += 5
            if "chatgpt" in image:
                score += 3
            if "codex" in title_l or "chatgpt" in title_l:
                score += 2
            if score:
                found.append((score, hwnd, title, image))
            return True

        user32.EnumWindows(visit, 0)
        if not found:
            log("没找到 Codex 窗口，可以自己切过去")
            return 0
        found.sort(key=lambda item: -item[0])
        _, hwnd, title, image = found[0]
        self._activate_window(hwnd)
        log("已切到 Codex 窗口：%s (%s)" % (title, os.path.basename(image)))
        return int(hwnd)

    def _activate_window(self, hwnd) -> None:
        """把某个顶层窗口置前（含最小化恢复 + AttachThreadInput 兜底）。"""
        user32.ShowWindow(wt.HWND(hwnd), 9)  # SW_RESTORE
        user32.SetForegroundWindow(wt.HWND(hwnd))
        if int(user32.GetForegroundWindow() or 0) != int(hwnd):
            foreground = user32.GetForegroundWindow()
            target_thread = user32.GetWindowThreadProcessId(wt.HWND(hwnd), None)
            current_thread = kernel32.GetCurrentThreadId()
            if foreground:
                user32.AttachThreadInput(user32.GetWindowThreadProcessId(foreground, None), current_thread, True)
            user32.AttachThreadInput(target_thread, current_thread, True)
            user32.SetForegroundWindow(wt.HWND(hwnd))
            if foreground:
                user32.AttachThreadInput(user32.GetWindowThreadProcessId(foreground, None), current_thread, False)
            user32.AttachThreadInput(target_thread, current_thread, False)

    def agent_display_name(self) -> str:
        """当前接入的 agent 的名字（拿不到就退回 id）。"""
        agent = str((self.settings or {}).get("agent") or "codex")
        try:
            import agent_state

            for item in agent_state.agent_list():
                if item.get("id") == agent:
                    return str(item.get("name") or agent)
        except Exception:  # noqa: BLE001
            pass
        return agent

    def build_agent_picker(self):
        """搭一个「切换卡片 Agent」快速选择面板（样式对齐设置页的 Agent 格）。

        返回还没显示的 Form，位置和 Show 由 open_agent_picker() 负责。
        为什么不用右键菜单的子菜单：① WinForms 的子菜单**悬停就自动展开**；
        ② 桌宠的鼠标钩子把"点在子菜单里"当成"点在菜单外面"——子菜单是单独的弹出层，
        不在主菜单矩形里，那一下点击会被拿去关菜单（ReleaseCapture + WM_PET_CLOSE_MENU），
        子菜单项的 Click 永远不触发（实测"点了没反应"就是这么来的）。独立面板没这两个毛病。
        """
        from System.Drawing import Color, ContentAlignment, Font, Rectangle, Region, Size
        from System.Drawing.Drawing2D import GraphicsPath
        from System.Windows.Forms import (
            Button,
            Cursors,
            FlatStyle,
            Form,
            FormBorderStyle,
            FormStartPosition,
            Label,
            Padding,
        )

        try:
            import agent_state

            entries = [item for item in agent_state.agent_list() if item.get("id")]
        except Exception as exc:  # noqa: BLE001
            log("读取 agent 列表失败（快速切换面板）：%r" % (exc,))
            entries = []
        if not entries:
            log("agent 列表为空，快速切换面板不弹")
            return None

        scale = float(self.dpi_scale or 1.0)

        def px(value: float) -> int:
            return int(round(value * scale))

        # 颜色抄的设置页（web/settings.css）：卡片底 / 边线 / 文字 / 选中态 / 按钮
        card = Color.FromArgb(24, 29, 41)            # --card #181d29
        edge = Color.FromArgb(45, 49, 60)            # rgba(255,255,255,.09) 叠在卡片底色上
        ink = Color.FromArgb(238, 243, 255)          # --ink
        muted = Color.FromArgb(152, 164, 187)        # --muted
        cell_bg = Color.FromArgb(34, 36, 41)         # rgba(255,255,255,.04)
        cell_hover_bg = Color.FromArgb(33, 44, 71)
        cell_hover_edge = Color.FromArgb(67, 90, 148)
        cell_on_bg = Color.FromArgb(38, 53, 95)      # rgba(70,110,220,.3)
        cell_on_edge = Color.FromArgb(89, 120, 202)  # rgba(110,150,255,.75)
        btn_bg = Color.FromArgb(35, 48, 84)          # .btn
        btn_edge = Color.FromArgb(58, 77, 127)
        btn_hover_bg = Color.FromArgb(42, 61, 113)
        ghost_bg = Color.FromArgb(36, 40, 52)        # .btn.ghost
        ghost_hover_bg = Color.FromArgb(46, 51, 66)

        # 尺寸按"右键菜单旁边的小面板"来定：整体大约是设置页格子的 0.7 倍
        gap_x, gap_y = 6.0, 4.0
        cell_w, cell_h = 104.0, 26.0
        pad, title_h, footer_h = 10.0, 18.0, 22.0
        gap_title, gap_footer = 6.0, 7.0
        columns = 2                                   # 和设置页一样：两列，先排满第一列
        rows = max(1, int(math.ceil(len(entries) / float(columns))))
        width = pad * 2 + columns * cell_w + (columns - 1) * gap_x
        height = (
            pad + title_h + gap_title
            + rows * cell_h + (rows - 1) * gap_y
            + gap_footer + footer_h + pad
        )

        current = str((self.settings or {}).get("agent") or "codex")
        chosen = {"id": current}
        cells = []

        def mark_click() -> None:
            # 面板可能压在角色上：点面板时别再让桌宠把那几下当"摸头/换表情"
            self._ignore_click_until = time.time() + 0.35

        def paint(btn, agent_id: str) -> None:
            on = agent_id == chosen["id"]
            btn.BackColor = cell_on_bg if on else cell_bg
            btn.ForeColor = ink
            btn.FlatAppearance.BorderColor = cell_on_edge if on else edge

        def hover(btn, agent_id: str, entered: bool) -> None:
            if agent_id == chosen["id"]:
                return
            if entered:
                btn.BackColor = cell_hover_bg
                btn.FlatAppearance.BorderColor = cell_hover_edge
            else:
                paint(btn, agent_id)

        def choose(agent_id: str) -> None:
            chosen["id"] = agent_id
            mark_click()
            for btn, aid in cells:
                paint(btn, aid)

        form = Form()
        form.FormBorderStyle = getattr(FormBorderStyle, "None")
        form.ShowInTaskbar = False
        form.TopMost = True
        form.StartPosition = FormStartPosition.Manual
        form.BackColor = card
        form.ClientSize = Size(px(width), px(height))
        form.KeyPreview = True
        form.Text = "切换卡片 Agent"
        try:  # 圆角，和设置页的卡片一个手感
            radius = px(8)
            path = GraphicsPath()
            box = Rectangle(0, 0, px(width) - 1, px(height) - 1)
            path.AddArc(box.X, box.Y, radius, radius, 180, 90)
            path.AddArc(box.Right - radius, box.Y, radius, radius, 270, 90)
            path.AddArc(box.Right - radius, box.Bottom - radius, radius, radius, 0, 90)
            path.AddArc(box.X, box.Bottom - radius, radius, radius, 90, 90)
            path.CloseFigure()
            form.Region = Region(path)
        except Exception:  # noqa: BLE001
            pass

        font_title = Font("Microsoft YaHei UI", 9.5)
        font_cell = Font("Microsoft YaHei UI", 8.0)

        title = Label()
        title.Text = "切换卡片 Agent"
        title.ForeColor = ink
        title.Font = font_title
        title.TextAlign = ContentAlignment.MiddleLeft
        title.SetBounds(px(pad), px(pad), px(width - pad * 2), px(title_h))
        title.MouseDown += (lambda sender, args: mark_click())
        form.Controls.Add(title)

        for index, item_info in enumerate(entries):
            agent_id = str(item_info.get("id") or "")
            col, row = index // rows, index % rows
            btn = Button()
            btn.Text = str(item_info.get("name") or agent_id)
            btn.FlatStyle = FlatStyle.Flat
            btn.FlatAppearance.BorderSize = 1
            btn.TextAlign = ContentAlignment.MiddleLeft
            btn.Padding = Padding(px(6), 0, 0, 0)
            btn.Font = font_cell
            btn.AutoEllipsis = True
            btn.UseVisualStyleBackColor = False
            btn.Cursor = Cursors.Hand
            btn.SetBounds(
                px(pad + col * (cell_w + gap_x)),
                px(pad + title_h + gap_title + row * (cell_h + gap_y)),
                px(cell_w),
                px(cell_h),
            )
            btn.Click += (lambda sender, args, aid=agent_id: choose(aid))
            btn.MouseDown += (lambda sender, args: mark_click())
            btn.MouseEnter += (lambda sender, args, b=btn, aid=agent_id: hover(b, aid, True))
            btn.MouseLeave += (lambda sender, args, b=btn, aid=agent_id: hover(b, aid, False))
            paint(btn, agent_id)
            form.Controls.Add(btn)
            cells.append((btn, agent_id))

        def confirm_click(sender=None, args=None) -> None:
            mark_click()
            agent_id = chosen["id"]
            self.close_agent_picker()
            self.switch_agent(agent_id)

        def cancel_click(sender=None, args=None) -> None:
            self.close_agent_picker()

        btn_w = 58.0
        confirm = Button()
        confirm.Text = "确认"
        confirm.FlatStyle = FlatStyle.Flat
        confirm.FlatAppearance.BorderSize = 1
        confirm.FlatAppearance.BorderColor = btn_edge
        confirm.BackColor = btn_bg
        confirm.ForeColor = Color.FromArgb(234, 241, 255)
        confirm.Font = font_cell
        confirm.UseVisualStyleBackColor = False
        confirm.Cursor = Cursors.Hand
        confirm.SetBounds(px(width - pad - btn_w), px(height - pad - footer_h), px(btn_w), px(footer_h))
        confirm.MouseDown += (lambda sender, args: mark_click())
        confirm.MouseEnter += (lambda sender, args: setattr(confirm, "BackColor", btn_hover_bg))
        confirm.MouseLeave += (lambda sender, args: setattr(confirm, "BackColor", btn_bg))
        confirm.Click += confirm_click

        cancel = Button()
        cancel.Text = "取消"
        cancel.FlatStyle = FlatStyle.Flat
        cancel.FlatAppearance.BorderSize = 1
        cancel.FlatAppearance.BorderColor = edge
        cancel.BackColor = ghost_bg
        cancel.ForeColor = muted
        cancel.Font = font_cell
        cancel.UseVisualStyleBackColor = False
        cancel.Cursor = Cursors.Hand
        cancel.SetBounds(px(width - pad - btn_w * 2 - 6), px(height - pad - footer_h), px(btn_w), px(footer_h))
        cancel.MouseDown += (lambda sender, args: mark_click())
        cancel.MouseEnter += (lambda sender, args: setattr(cancel, "BackColor", ghost_hover_bg))
        cancel.MouseLeave += (lambda sender, args: setattr(cancel, "BackColor", ghost_bg))
        cancel.Click += cancel_click

        form.Controls.Add(confirm)
        form.Controls.Add(cancel)
        shown_at = {"t": time.time()}

        def on_key(sender, args) -> None:
            from System.Windows.Forms import Keys

            if args.KeyCode == Keys.Escape:
                cancel_click()

        def on_deactivate(sender, args) -> None:
            # 刚弹出来的一瞬可能先收到一次 Deactivate（焦点还在菜单那边），别把它当"点了外面"
            if time.time() - shown_at["t"] < 0.35:
                return
            cancel_click()

        form.KeyDown += on_key
        form.Deactivate += on_deactivate
        form.MouseDown += (lambda sender, args: mark_click())
        return form

    def open_agent_picker(self) -> None:
        """右键菜单「切换卡片 Agent」：弹出快速选择面板。

        点格子 = 选中（变色），底下「确认」才真正切换 —— 写的是和设置页同一个键
        （pet_settings.json 的 agent）；点面板外面 / 按 Esc / 点「取消」= 不改设置。
        位置：顶在右键总菜单刚才弹的地方（菜单一消失、面板就上来），贴屏幕边时夹回
        工作区保证整块可见（见 agent_picker_location）。
        """
        self.close_agent_picker()
        try:
            form = self.build_agent_picker()
        except Exception as exc:  # noqa: BLE001
            log("快速切换面板创建失败：%r" % (exc,))
            return
        if form is None:
            return
        try:
            from System.Drawing import Point as DrawingPoint
            from System.Windows.Forms import Screen

            pos = getattr(self, "_menu_pos", None)
            if not pos:
                # 没见过菜单就退回桌宠左上角（正常路径不会走到这）
                pos = (self.form.Left, self.form.Top)
            area = Screen.FromPoint(DrawingPoint(int(pos[0]), int(pos[1]))).WorkingArea
            x, y = self.agent_picker_location(area, form.ClientSize, pos)
            form.Location = DrawingPoint(x, y)
        except Exception as exc:  # noqa: BLE001
            log("快速切换面板定位失败（用默认位置）：%r" % (exc,))
        self.agent_picker = form
        form.FormClosed += (lambda sender, args: self.on_agent_picker_closed(form))
        form.Show()
        form.Activate()
        log("弹出 Agent 快速切换面板（当前 %s）" % self.agent_display_name())

    @staticmethod
    def agent_picker_location(area, size, menu_pos):
        """快速切换面板的落点：和右键总菜单同一个地方（左上角对齐），整块保证可见。

        menu_pos 是弹菜单时记下的屏幕坐标（self._menu_pos）。贴屏幕右边/底部时，
        面板比菜单大概，会被夹回工作区里 —— 看起来就是"顺边挪一点，不缺角"。
        """
        x = max(area.Left + 4, min(int(menu_pos[0]), area.Right - size.Width - 4))
        y = max(area.Top + 4, min(int(menu_pos[1]), area.Bottom - size.Height - 4))
        return x, y

    def close_agent_picker(self) -> None:
        form = getattr(self, "agent_picker", None)
        if form is None:
            return
        self.agent_picker = None       # 先清引用：Close() 会触发 FormClosed
        try:
            if not form.IsDisposed:
                form.Close()
        except Exception as exc:  # noqa: BLE001
            log("关闭快速切换面板失败：%r" % (exc,))

    def on_agent_picker_closed(self, form) -> None:
        if getattr(self, "agent_picker", None) is form:
            self.agent_picker = None

    def switch_agent(self, agent_id: str) -> None:
        """右键菜单里的快速切换：换掉设置里接入的 agent。

        写的是和设置页同一个键（pet_settings.json 的 agent）：状态循环下一秒按新 agent
        读记录，卡片 / 表情 / 「切到 X 窗口」的菜单文字都会跟着换。
        """
        agent_id = str(agent_id or "").strip()
        if not agent_id:
            return
        current = str((self.settings or {}).get("agent") or "codex")
        if agent_id == current:
            log("接入的 Agent 没变：%s" % agent_id)
            return
        self.settings["agent"] = agent_id
        try:
            self.settings = settings_store.save(self.settings)
        except Exception as exc:  # noqa: BLE001
            log("保存接入的 Agent 失败：%r" % (exc,))
        log("接入的 Agent 已切换：%s（右键菜单快速设置）" % self.agent_display_name())
        try:
            self.push_settings()          # 同步给设置窗口（开着的话，选中项也会跟着变）
        except Exception as exc:  # noqa: BLE001
            log("推送接入的 Agent 设置失败：%r" % (exc,))
        self.refresh_card_after_switch()  # 按了"确认"就立刻读一份新状态，不等下一秒

    def refresh_card_after_switch(self) -> None:
        """切换 agent 后马上重读一次状态推给卡片（读记录可能扫文件，放后台线程）。"""

        def work() -> None:
            try:
                import agent_state

                agent_id = str((self.settings or {}).get("agent") or "codex")
                payload = agent_state.snapshot(agent_id)
                payload["usage"] = usage_line(self)
            except Exception as exc:  # noqa: BLE001
                log("切换后立即刷新卡片失败：%r" % (exc,))
                return
            self.last_payload = payload
            try:
                self.post({"t": "state", "data": payload})
                self.schedule_card_update(payload)
            except Exception as exc:  # noqa: BLE001
                log("切换后推送卡片失败：%r" % (exc,))

        threading.Thread(target=work, name="agent-switch-refresh", daemon=True).start()

    def focus_agent_window(self) -> int:
        """切到"设置里选的那个 agent"的窗口。

        Codex 走原来的 focus_codex()（桌面版是 ChatGPT.exe，按进程镜像认最准）；
        其它 agent 两步走：
          1. 原生 exe（claude / hermes 这类）→ 找到它的进程，再认"谁拥有承载它的窗口"；
          2. 标题关键词（gemini / openclaw 这类 node 系 CLI 跑在终端里，窗口属于终端进程）。
        终端宿主（Windows Terminal / conhost / PowerShell / cmd）加一点分，避免认错窗口。
        """
        agent = str((self.settings or {}).get("agent") or "codex")
        if agent == "codex":
            return self.focus_codex()
        rules = AGENT_WINDOW_RULES.get(agent)
        if not rules:
            return self.focus_codex()

        try:
            import agent_state
        except Exception:  # noqa: BLE001
            agent_state = None

        tree: set[int] = set()
        if agent_state is not None:
            try:
                for pid in agent_state.find_process_pids(rules.get("procs") or []):
                    tree.update(agent_state.ancestor_pids(pid, 8))
            except Exception as exc:  # noqa: BLE001
                log("查 %s 进程树失败（不影响找窗口）：%r" % (agent, exc))

        me = int(self.form.Handle.ToInt64())
        titles = tuple(str(item).lower() for item in (rules.get("titles") or ()))
        hosts = ("windowsterminal", "openconsole", "conhost", "powershell", "pwsh", "cmd",
                 "wezterm", "alacritty", "mintty")
        found = []
        terminals = []          # 终端类窗口（找不到目标时写进日志，方便补规则）

        @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        def visit(hwnd, _):
            if int(hwnd) == me or not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value
            title_l = title.lower()
            image = self.process_image(hwnd).lower()
            base = os.path.basename(image)
            score = 0
            if titles and any(key in title_l for key in titles):
                score += 5
            if tree and agent_state is not None:
                pid = ctypes.c_ulong(0)
                user32.GetWindowThreadProcessId(wt.HWND(hwnd), ctypes.byref(pid))
                try:
                    if set(agent_state.ancestor_pids(int(pid.value), 8)) & tree:
                        score += 4
                except Exception:  # noqa: BLE001
                    pass
            if base and any(host in base for host in hosts):
                score += 2
                terminals.append("%s [%s]" % (title, base))
            if "桌宠" in title or "codexpet" in title_l:
                score -= 6            # 别切到我们自己
            if agent != "codex" and "codex" in title_l:
                score -= 3            # Codex 的窗口别抢
            if score > 0:
                found.append((score, hwnd, title, image))
            return True

        user32.EnumWindows(visit, 0)
        if not found:
            hint = "；现在开着的终端窗口：%s" % " | ".join(terminals[:6]) if terminals else ""
            log("没找到 %s 的窗口（可能没在运行或在别的虚拟桌面）%s，可以自己切过去" % (agent, hint))
            return 0
        found.sort(key=lambda item: -item[0])
        _, hwnd, title, image = found[0]
        self._activate_window(hwnd)
        log("已切到 %s 的窗口：%s (%s)" % (agent, title, os.path.basename(image)))
        return int(hwnd)

    def process_image(self, hwnd) -> str:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        pid = ctypes.c_ulong(0)
        user32.GetWindowThreadProcessId(wt.HWND(hwnd), ctypes.byref(pid))
        if not pid.value:
            return ""
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(600)
            size = ctypes.c_ulong(600)
            if ctypes.windll.kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return buf.value
            return ""
        finally:
            kernel32.CloseHandle(handle)

    # -- settings window -----------------------------------------------------

    def expression_names(self) -> list[str]:
        if self._expressions is None:
            names = []
            try:
                with open(os.path.join(WEB, "model", "settings.json"), encoding="utf-8") as fh:
                    data = json.load(fh)
                # 表情声明在 FileReferences 里面（渲染库要求的位置）；
                # 早期版本写在最外层，这里两种都读，免得又出现"0 个表情"。
                items = (data.get("FileReferences") or {}).get("Expressions") or data.get("Expressions") or []
                for item in items:
                    if isinstance(item, dict):
                        names.append(item.get("Name") or item.get("File") or "")
            except Exception as exc:  # noqa: BLE001
                log("读取表情列表失败：%r" % (exc,))
            self._expressions = [n for n in names if n]
        return self._expressions

    def motion_inventory(self) -> list[dict]:
        """列出模型里所有动作（组 / 序号 / 文件 / 时长），用来在设置里逐条勾选。"""
        if self._motions is not None:
            return self._motions
        items: list[dict] = []
        try:
            with open(os.path.join(WEB, "model", "settings.json"), encoding="utf-8") as fh:
                groups = (json.load(fh).get("FileReferences") or {}).get("Motions") or {}
            for group, motions in groups.items():
                for index, motion in enumerate(motions or []):
                    rel = motion.get("File") or ""
                    duration = 0.0
                    try:
                        with open(os.path.join(WEB, "model", rel.replace("/", os.sep)), encoding="utf-8") as fh:
                            duration = float(((json.load(fh).get("Meta") or {}).get("Duration")) or 0.0)
                    except Exception:
                        pass
                    items.append(
                        {
                            "id": "%s:%d" % (group, index),
                            "group": group,
                            "index": index,
                            "file": os.path.basename(rel),
                            "duration": round(duration, 1),
                        }
                    )
        except Exception as exc:  # noqa: BLE001
            log("读取动作列表失败：%r" % (exc,))
        self._motions = items
        return items

    def prop_parameters(self) -> list[dict]:
        """列出"道具参数"：被动作驱动的、非身体/表情的标准参数（锤子、猫手、手机、泡泡……）。

        这些道具藏在动作曲线里，关表情/关动作都不好使 —— 直接把参数按死为 0 最直接。
        名字取自 cdi3.json（模型作者写的显示名），所以能看出哪个是"锤子出现"。
        """
        if self._props is not None:
            return self._props
        standard_prefix = (
            "ParamAngle", "ParamBodyAngle", "ParamEye", "ParamBrow", "ParamMouth", "ParamBreath",
            "ParamHair", "ParamArm", "ParamHand", "ParamLeg", "ParamShoulder", "ParamNeck",
            "ParamBase", "ParamOpacity", "ParamRotation", "ParamPosition",
        )
        ids: set[str] = set()
        try:
            with open(os.path.join(WEB, "model", "settings.json"), encoding="utf-8") as fh:
                groups = (json.load(fh).get("FileReferences") or {}).get("Motions") or {}
            for motions in groups.values():
                for motion in motions or []:
                    rel = (motion.get("File") or "").replace("/", os.sep)
                    try:
                        with open(os.path.join(WEB, "model", rel), encoding="utf-8") as fh:
                            for curve in json.load(fh).get("Curves") or []:
                                cid = curve.get("Id") or ""
                                if cid and not cid.startswith(standard_prefix):
                                    ids.add(cid)
                    except Exception:
                        continue
            names = {}
            try:
                with open(os.path.join(WEB, "model", "c_0120.cdi3.json"), encoding="utf-8") as fh:
                    for item in json.load(fh).get("Parameters") or []:
                        names[item.get("Id")] = item.get("Name") or ""
            except Exception:
                pass
            ids.discard("chehui")  # 底部工具按钮，不属于要屏蔽的道具
            ids.discard("bi")
            ids.discard("pi")
            items = [
                {"id": pid, "name": names.get(pid) or pid, "hammer": "锤" in (names.get(pid) or "")}
                for pid in sorted(ids, key=lambda p: (0 if "锤" in (names.get(p) or "") else 1, p))
            ]
        except Exception as exc:  # noqa: BLE001
            log("读取道具参数失败：%r" % (exc,))
            items = []
        self._props = items
        return items

    def show_settings(self) -> None:
        """设置窗口跑在独立 STA 线程上。

        之前是在右键菜单的回调里直接建窗口，那条路径上线程公寓被当成 MTA，
        WebView2 会报 `RPC_E_CHANGED_MODE：无法在设置线程模式后对其加以更改`。
        每个窗口起一个自己的 STA 线程（并在里面自己 CoInitializeEx）就彻底绕开了。

        "已经开着"的分支：最小化到任务栏 / 被别的窗口压住 / 被切到别的虚拟桌面时，
        再点一次「设置…」都要把它叫回眼前 —— 光 Activate() 对最小化的窗口没用。
        """
        form = self.settings_window
        if form is not None and not form.IsDisposed:
            self.restore_settings_window(form)
            return
        self.spawn_window_thread("settings-window", self.build_settings_window)

    def restore_settings_window(self, form) -> None:
        """把已经开着的设置窗口叫回眼前（动作丢回设置窗口自己的 STA 线程上做）。

        最小化的窗口只会响应"把 WindowState 设回 Normal"，Activate/BringToFront 对它
        是无效的 —— 这就是之前"最小化后点设置没反应"的原因。
        """

        def do() -> None:
            try:
                from System.Windows.Forms import FormWindowState

                was_min = form.WindowState == FormWindowState.Minimized
                if was_min:
                    form.WindowState = FormWindowState.Normal
                if not form.Visible:
                    form.Show()
                # 可能被切到别的虚拟桌面去了：搬回桌宠现在所在的桌面（和首次打开同一套逻辑）
                desktops = self.desktops or VirtualDesktops()
                pet_form = getattr(self, "form", None)
                if pet_form is not None:
                    desktops.align_to(int(form.Handle.ToInt64()), int(pet_form.Handle.ToInt64()))
                form.Activate()
                form.BringToFront()
                log("设置窗口叫回桌面（%s）" % ("从最小化还原" if was_min else "置前"))
            except Exception as exc:  # noqa: BLE001
                log("设置窗口置前失败：%r" % (exc,))

        try:
            from System import Action

            form.BeginInvoke(Action(do))
        except Exception:  # noqa: BLE001
            do()

    def spawn_window_thread(self, name: str, build) -> None:
        def run() -> None:
            init_sta()
            import clr

            clr.AddReference("System.Windows.Forms")
            from System.Windows.Forms import Application

            try:
                form = build()
            except Exception as exc:  # noqa: BLE001
                log("%s 创建失败：%r" % (name, exc))
                return
            if form is None:
                return
            Application.Run(form)

        threading.Thread(target=run, name=name, daemon=True).start()

    def build_settings_window(self):
        from System.Drawing import Point as DrawingPoint, Rectangle, Size
        from System.Windows.Forms import (
            DockStyle,
            Form,
            FormStartPosition,
            FormWindowState,
            Screen,
        )
        from Microsoft.Web.WebView2.WinForms import CoreWebView2CreationProperties, WebView2

        form = Form()
        form.Text = "Agent 桌宠 · 设置"
        form.ClientSize = Size(580, 680)
        form.StartPosition = FormStartPosition.CenterScreen
        form.MinimumSize = Size(520, 520)
        # 记住上次摆好的大小/位置：关掉再开不该回到"初始样子"（2026-10-11 加）。
        # 存的坐标跑到屏幕外（换了显示器/改了分辨率）时夹回工作区，保证整块可见。
        saved_rect = load_settings_window_rect()
        if saved_rect:
            sx, sy, sw, sh = saved_rect
            work = Screen.FromPoint(DrawingPoint(sx, sy)).WorkingArea
            bx, by, bw, bh = fit_window_rect(work, sx, sy, sw, sh)
            form.StartPosition = FormStartPosition.Manual
            form.Bounds = Rectangle(bx, by, bw, bh)
        form.TopMost = True          # 先置顶，避免被 Codex/浏览器窗口压在下面
        form.ShowInTaskbar = True    # 也在任务栏留一个，找不到时能点出来
        try:
            form.Icon = self.form.Icon
        except Exception:  # noqa: BLE001
            pass

        webview = WebView2()
        webview.CreationProperties = self.create_props()
        webview.Dock = DockStyle.Fill
        form.Controls.Add(webview)
        webview.CoreWebView2InitializationCompleted += self.on_settings_ready
        webview.WebMessageReceived += self.on_settings_message
        webview.EnsureCoreWebView2Async(None)

        self.settings_window = form
        self.settings_webview = webview
        self.register_aux_window(form)

        def remember_geometry(sender=None, args=None) -> None:
            """用户摆好的大小/位置存起来（最小化/最大化时看 RestoreBounds，那是还原后的样子）。"""
            try:
                if form.WindowState == FormWindowState.Normal:
                    bounds = form.Bounds
                else:
                    bounds = form.RestoreBounds
                save_settings_window_rect(bounds.X, bounds.Y, bounds.Width, bounds.Height)
            except Exception as exc:  # noqa: BLE001
                log("保存设置窗口大小失败：%r" % (exc,))

        form.ResizeEnd += remember_geometry
        form.FormClosing += remember_geometry
        form.FormClosed += self.on_settings_closed
        form.Show()
        form.Activate()
        form.BringToFront()
        # 关键：新窗口默认开在"进程启动时那个桌面"上。你切过虚拟桌面的话，
        # 窗口会开在你看不见的那个桌面上 —— 这里把它搬到你现在看的桌面。
        desktops = self.desktops or VirtualDesktops()
        pet_form = getattr(self, "form", None)
        if pet_form is not None:
            desktops.align_to(int(form.Handle.ToInt64()), int(pet_form.Handle.ToInt64()))
        # 同步虚拟桌面后再次明确置顶：设置窗口始终盖在桌宠上面，桌宠仍可透过其外区域操作。
        form.TopMost = True
        form.BringToFront()
        log("打开设置窗口（置顶已开启；虚拟桌面已同步）")
        return form

    def on_settings_closed(self, sender, args) -> None:
        self.unregister_aux_window(sender)
        self.settings_window = None

    def on_settings_ready(self, sender, args) -> None:
        core = sender.CoreWebView2
        if core is None:
            log("设置窗口初始化失败：%s" % args.InitializationException)
            self.report_fatal("设置窗口初始化失败", args.InitializationException)
            return
        core.Settings.AreDefaultContextMenusEnabled = False
        core.Navigate("http://127.0.0.1:%d/settings.html" % self.port)
        log("设置窗口 WebView2 就绪，正在加载界面")

    def on_settings_message(self, sender, args) -> None:
        try:
            msg = json.loads(args.WebMessageAsJson)
        except Exception:
            return
        kind = msg.get("t")
        if kind == "hello":
            self.push_settings()
        elif kind == "save":
            self.apply_settings(msg.get("data") or {})
        elif kind == "action":
            self.run_action(msg.get("name") or "")
        elif kind == "perf":
            data = msg.get("data") or {}
            if data.get("renderMs"):
                log("设置页渲染耗时 %s 毫秒（只有超过 300ms 才会上报）" % data.get("renderMs"))

    def push_settings(self, force: bool = True) -> None:
        payload = dict(self.settings)
        payload["expressions"] = dict(self.settings.get("expressions") or {})
        low, high = self.scale_limits()
        payload["scaleMin"] = low
        payload["scaleMax"] = high
        payload["avatars"] = []
        payload["availableExpressions"] = self.expression_names()
        payload["motions"] = self.motion_inventory()
        payload["baseSize"] = [WINDOW_W, WINDOW_H]
        payload["petNow"] = dict(self.status or {})   # 桌宠此刻在演什么，设置里能看到
        payload["cacheText"] = self.cache_size_text()  # 排查栏：WebView2 缓存占用
        payload["cacheBytes"] = self.cache_size_bytes()
        payload["desktopLink"] = desktop_link_exists()  # 排查栏：桌面快捷方式状态
        payload["renderFps"] = round(self.render_fps, 1)   # 给"渲染帧率上限"旁边显示当前帧率
        payload["allowed"] = {
            "motions": self.allowed_motion_count(),
            "motionTotal": len(self.motion_inventory()),
            "expressions": self.allowed_expression_count(),
            "expressionTotal": len(self.expression_names()),
        }
        payload["logPath"] = log_path
        payload["home"] = HERE
        payload["autostart"] = os.path.exists(startup_link_path())
        # 注意：这个函数会被界面线程直接调用（滚动缩放、页面就绪…），
        # 所以**只能读缓存**，不能在这里同步跑 tasklist / PowerShell。
        try:
            import agent_state

            payload["agentList"] = agent_state.agent_list()
            payload["agentStatus"] = dict(self.agent_status_cache)
        except Exception as exc:  # noqa: BLE001
            log("读取 agent 列表失败：%r" % (exc,))
            payload["agentList"] = []
            payload["agentStatus"] = {}
        self.refresh_agent_status_async()
        self.refresh_cache_size_async()
        payload["fpsLimit"] = self.effective_fps_limit()   # 卡顿保护期内会临时压低
        message = {"t": "settings", "data": payload}
        # 内容没变就别再投一次：WebView2 忙的时候投递会把调用线程钉住，
        # 少发一次少一次风险（renderFps / petNow 是小字，允许最多 10 秒不刷新）。
        stable = dict(payload)
        stable.pop("renderFps", None)
        stable.pop("petNow", None)
        signature = json.dumps(stable, ensure_ascii=False, sort_keys=True, default=str)
        now = time.time()
        if not force and signature == self._settings_signature and (now - self._settings_pushed_at) < 10.0:
            return
        self._settings_signature = signature
        self._settings_pushed_at = now
        self.post(message)
        self.send_to_settings_window(message)

    def effective_fps_limit(self) -> float:
        """渲染帧率上限的"生效值"：卡顿保护期内压到 60。"""
        try:
            limit = float(self.settings.get("fpsLimit", 60) or 0)
        except (TypeError, ValueError):
            limit = 60.0
        if time.time() < self._fps_protect_until and (limit <= 0 or limit > 60):
            return 60.0
        return limit

    def protect_fps(self, reason: str) -> None:
        """检测到卡顿 → 临时把渲染帧率压到 60（默认 3 分钟，期间再卡就续期）。

        桌宠是 113fps 的大 Live2D 窗口 + 两个 WebView2；显卡/合成器一忙，
        窗口操作和消息投递都会被拖住（上一轮"未响应"的线程栈就是这么来的）。
        先把渲染压力降下来，桌面就不容易被拖卡；3 分钟后自动恢复原帧率。
        """
        now = time.time()
        if now < self._fps_protect_until:
            self._fps_protect_until = now + 180.0     # 还在保护期内又卡了：续期
            return
        self._fps_protect_until = now + 180.0
        log("检测到卡顿（%s）：临时把渲染帧率压到 60，3 分钟后自动恢复" % reason)
        try:
            self.push_settings()                      # 让页面立刻按新上限渲染
        except Exception as exc:  # noqa: BLE001
            log("压低渲染帧率失败：%r" % (exc,))

    def refresh_agent_status_async(self, force: bool = False) -> None:
        """后台线程去检测各 agent 的本地记录（tasklist / PowerShell 都很慢，绝不能挡界面）。

        结果放进 self.agent_status_cache，回来后再 push 一次；15 秒内最多算一轮。
        """
        now = time.time()
        if self.agent_status_busy:
            return
        if not force and (now - self.agent_status_at) < 15.0:
            return
        self.agent_status_busy = True

        def work() -> None:
            try:
                import agent_state

                status = agent_state.detect_all()
            except Exception as exc:  # noqa: BLE001
                log("检测 agent 本地记录失败：%r" % (exc,))
                status = None
            if status is not None:
                self.agent_status_cache = status
            self.agent_status_at = time.time()
            self.agent_status_busy = False
            try:
                self.push_settings(force=False)      # 定时刷新：内容没变就别再投递
            except Exception as exc:  # noqa: BLE001
                log("推送 agent 检测结果失败：%r" % (exc,))

        threading.Thread(target=work, name="agent-status", daemon=True).start()

    # -- WebView2 缓存（设置页排查栏） --------------------------------------

    def cache_size_bytes(self) -> int:
        return int(getattr(self, "_cache_size_bytes", 0) or 0)

    def cache_size_text(self) -> str:
        if not getattr(self, "_cache_size_ready", False):
            return "计算中…"
        mb = self.cache_size_bytes() / (1024.0 * 1024.0)
        if mb >= 1024.0:
            return "%.2f GB" % (mb / 1024.0)
        return "%.1f MB" % mb

    def refresh_cache_size_async(self, force: bool = False) -> None:
        """后台量 .webview2 的体积：好几万个小文件，绝不能在界面线程里爬目录。"""
        now = time.time()
        if getattr(self, "_cache_size_busy", False):
            return
        if not force and (now - getattr(self, "_cache_size_at", 0.0)) < 60.0:
            return
        self._cache_size_busy = True

        def work() -> None:
            total = 0
            root = os.path.join(HERE, ".webview2")
            try:
                for base, _dirs, files in os.walk(root):
                    for name in files:
                        try:
                            total += os.path.getsize(os.path.join(base, name))
                        except OSError:
                            pass
            except Exception as exc:      # noqa: BLE001
                log("统计 WebView2 缓存失败：%r" % (exc,))
                total = 0
            self._cache_size_bytes = total
            self._cache_size_ready = True
            self._cache_size_at = time.time()
            self._cache_size_busy = False
            try:
                self.push_settings(force=False)
            except Exception:      # noqa: BLE001
                pass

        threading.Thread(target=work, name="cache-size", daemon=True).start()

    def clear_webview_cache(self) -> None:
        """清 WebView2 缓存：写请求文件，让 pet-launcher 在旧进程退出后删掉再重启。"""
        flag = os.path.join(HERE, "clear-cache.request")
        try:
            with open(flag, "w", encoding="utf-8") as fh:
                fh.write(str(os.getpid()))
        except OSError as exc:
            log("写清除缓存请求失败：%r" % (exc,))
            return
        log("收到清除 WebView2 缓存请求（当前约 %s），重启时清理" % self.cache_size_text(), force=True)
        try:
            import subprocess

            creation = 0x08000000      # CREATE_NO_WINDOW
            subprocess.Popen(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-WindowStyle", "Hidden", "-File", os.path.join(HERE, "pet-launcher.ps1")],
                cwd=HERE,
                creationflags=creation,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:      # noqa: BLE001
            log("拉起清缓存助手失败：%r" % (exc,))
            return
        from System import Action

        self.form.BeginInvoke(Action(self.form.Close))

    def create_desktop_link(self) -> bool:
        """在桌面创建「Agent 桌宠」快捷方式（设置页按钮；属性和 launcher 首次创建一致）。"""
        link = desktop_link_path()
        icon = os.path.join(HERE, "web", "icon-transparent.ico")
        if not os.path.exists(icon):
            icon = os.path.join(HERE, "web", "icon.ico")
        ps_exe = os.path.join(
            os.environ.get("SystemRoot", r"C:\Windows"),
            "System32", "WindowsPowerShell", "v1.0", "powershell.exe",
        )
        launcher = os.path.join(HERE, "pet-launcher.ps1")
        ps = (
            "$ws = New-Object -ComObject WScript.Shell; "
            "$l = $ws.CreateShortcut('{dst}'); "
            "$l.TargetPath = '{exe}'; "
            "$l.Arguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File \"{launcher}\"'; "
            "$l.WorkingDirectory = '{here}'; "
            "$l.IconLocation = '{icon},0'; "
            "$l.Description = 'Agent 桌宠：Live2D 形象跟着接入的 Agent 实时状态变化'; "
            "$l.Save()"
        ).format(dst=link, exe=ps_exe, launcher=launcher, here=HERE, icon=icon)
        try:
            import subprocess

            subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                cwd=HERE,
                creationflags=0x08000000,
                timeout=25,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:      # noqa: BLE001
            log("创建桌面快捷方式失败：%r" % (exc,))
            self.push_settings()
            return False
        ok = os.path.exists(link)
        if ok:
            self._mark_desktop_link_done()
            log("已创建桌面快捷方式：Agent 桌宠")
        else:
            log("创建桌面快捷方式失败（请查看日志）")
        self.push_settings()
        return ok

    def remove_desktop_link(self) -> None:
        """删掉桌面快捷方式，并保留 desktop-link.done：以后不会再自动重建。"""
        link = desktop_link_path()
        try:
            if os.path.exists(link):
                os.remove(link)
        except OSError as exc:
            log("删除桌面快捷方式失败：%r" % (exc,))
        self._mark_desktop_link_done()
        log("已删除桌面快捷方式（设置里可随时重建）")
        self.push_settings()

    def _mark_desktop_link_done(self) -> None:
        try:
            with open(os.path.join(HERE, "desktop-link.done"), "w", encoding="ascii") as fh:
                fh.write(time.strftime("%Y-%m-%d %H:%M:%S"))
        except OSError:
            pass

    def send_to_settings_window(self, message: dict) -> None:
        if time.time() < getattr(self, "_settings_push_muted_until", 0.0):
            return          # 设置窗口的 WebView2 刚才很慢：先别投递，避免把它顶成"未响应"
        """设置窗口在别的线程上，跨线程碰 WebView2 必须回到它自己的线程（BeginInvoke）。

        WebView2 忙的时候 PostWebMessageAsJson 会**阻塞调用它的线程**——如果每条消息都直接发，
        设置窗口的界面线程就会一阵一阵地卡（鼠标在窗口上就像"暂停"了）。所以这里做两件事：
        1. 只留最新一条待发消息（消息是"全量状态"，旧的没必要发了）；
        2. 同时只有一个发送在跑，发完再看有没有新消息。
        """
        form = self.settings_window
        webview = self.settings_webview
        if form is None or webview is None or form.IsDisposed:
            return
        self._settings_outbox = message
        if getattr(self, "_settings_posting", False):
            return
        self._settings_posting = True
        try:
            from System import Action

            def pump():
                try:
                    while True:
                        current = self._settings_outbox
                        if current is None:
                            break
                        self._settings_outbox = None
                        core = webview.CoreWebView2
                        if core is None:
                            break
                        started = time.time()
                        self._settings_post_started = started
                        self._settings_post_warned = False
                        core.PostWebMessageAsJson(json.dumps(current, ensure_ascii=False))
                        self._settings_post_started = 0.0
                        spent = time.time() - started
                        if spent > 0.3:
                            log("发给设置窗口的消息耗时 %.1f 秒（WebView2 忙）" % spent)
                        if spent > 1.0:
                            self.protect_fps("给设置窗口发消息 %.1f 秒" % spent)
                        if spent > 2.0:
                            self._settings_push_muted_until = time.time() + 30.0
                            log("设置窗口的 WebView2 很慢（%.1f 秒）：暂停给它推消息 30 秒，避免整窗未响应" % spent)
                except Exception as exc:  # noqa: BLE001
                    log("向设置窗口发送配置失败：%r" % (exc,))
                finally:
                    self._settings_posting = False

            form.BeginInvoke(Action(pump))
        except Exception as exc:  # noqa: BLE001
            self._settings_posting = False
            log("向设置窗口发送配置失败：%r" % (exc,))

    def scale_limits(self) -> tuple[float, float]:
        """最小 30%（2026-10-11 抬上来的，以前按"桌面图标大小"≈12%，太小基本没法用）；
        最大至少给到 SCALE_MAX_WISH（160%）。

        早先的算法是"角色不超过半屏"（面积约 1/4 屏幕），屏幕不高的机器上只能放大到
        120%~140%；现在改成：先按半屏算，但至少给到 160%，再用"整个窗口要能放进屏幕"
        兜底（不然窗口上方留给进度卡片的那块会跑到屏幕外面去）。
        """
        from System.Windows.Forms import Screen

        area = Screen.PrimaryScreen.WorkingArea
        dpi = self.dpi_scale or 1.0
        screen_w = area.Width / dpi
        screen_h = area.Height / dpi
        # 纵向按"角色本身的高度"算，而不是整个窗口（窗口上方那块是留给进度卡片的，
        # 按窗口算会把放大上限压到 100%，等于没法放大）
        half_screen = min(screen_w * 0.5 / WINDOW_W, screen_h * 0.5 / CHAR_BASE_H)
        # 兜底"别大得离谱"：允许整个窗口比屏幕高出 20% —— 窗口上方那块是空的（留给卡片），
        # 角色本身贴着底边画，所以顶出去一点不影响看角色，卡片也会自动贴回屏幕内。
        too_big = min(screen_w / WINDOW_W, screen_h / (WINDOW_H * 0.8))
        max_scale = min(max(half_screen, SCALE_MAX_WISH), max(1.0, too_big))
        min_scale = max(SCALE_MIN, ICON_SIZE / float(WINDOW_W), ICON_SIZE / float(WINDOW_H))
        return round(min_scale, 3), round(max(1.0, min(max_scale, 4.0)), 2)

    def apply_settings(self, incoming: dict) -> None:
        """保存设置。注意：这个函数可能跑在设置窗口的线程上，
        所以所有"碰窗口"的动作都要回到桌宠自己的 UI 线程去做（跨线程会让界面卡死）。"""
        started = time.time()
        merged = dict(self.settings)
        merged.update({k: v for k, v in (incoming or {}).items() if k != "availableExpressions"})
        low, high = self.scale_limits()
        try:
            merged["scale"] = max(low, min(high, float(merged.get("scale", 1.0))))
        except (TypeError, ValueError):
            merged["scale"] = 1.0
        self.settings = settings_store.save(merged)
        # 开机自启这类"外部动作"跟着保存一起生效，但要起子进程去写启动项，
        # 一律放后台线程：点「保存」必须立刻有反应，界面线程更不能被拖住。
        want_autostart = bool(merged.get("autostart", os.path.exists(startup_link_path())))
        self.invoke_on_pet(self.apply_settings_ui)
        log(
            "设置已保存：接入 %s / 缩放 %.2f / 跟随桌面 %s / 置顶 %s / 自动表情 %s / 自动动作 %s / 动作 %d/%d / 表情 %d/%d"
            % (
                self.settings.get("agent", "codex"),
                self.settings.get("scale", 1.0),
                self.settings.get("followDesktop"),
                self.settings.get("alwaysOnTop"),
                self.settings.get("autoExpression"),
                self.settings.get("autoMotion"),
                self.allowed_motion_count(),
                len(self.motion_inventory()),
                self.allowed_expression_count(),
                len(self.expression_names()),
            )
        )
        self.push_settings()

        def external() -> None:
            try:
                if want_autostart != os.path.exists(startup_link_path()):
                    self.toggle_autostart(want_autostart)
            except Exception as exc:  # noqa: BLE001
                log("应用启动项设置失败：%r" % (exc,))

        threading.Thread(target=external, name="apply-external", daemon=True).start()
        spent = time.time() - started
        if spent > 0.5:
            log("保存设置这一步耗时 %.1f 秒（正常应该是毫秒级，记下来方便查）" % spent)

    def invoke_on_pet(self, fn) -> None:
        """把动作丢回桌宠的 UI 线程执行（跨线程碰 WinForms 会让界面无响应）。"""
        form = getattr(self, "form", None)
        if form is None:
            fn()
            return
        try:
            from System import Action

            form.BeginInvoke(Action(fn))
        except Exception:
            fn()

    def apply_settings_ui(self) -> None:
        try:
            self.ui_phase("应用设置")
            if getattr(self, "form", None) is None:
                return
            global verbose_log
            verbose_log = bool(self.settings.get("verboseLog", False))
            # 这些属性一写就会走 SetWindowPos / DWM，合成器忙的时候能卡一两秒；
            # 值没变就别去碰它（保存设置时会走这里，每次都写是浪费）。
            want_top = bool(self.settings.get("alwaysOnTop", True))
            if self.form.TopMost != want_top:
                self.ui_phase("设置置顶")
                self.form.TopMost = want_top
            want_opacity = float(self.settings.get("opacity", 1.0))
            if abs(self.form.Opacity - want_opacity) > 0.01:
                self.ui_phase("设置不透明度")
                self.form.Opacity = want_opacity
            self.pet_scale = max(SCALE_MIN, float(self.settings.get("scale", 1.0)))
            base = (PANEL_W, PANEL_H) if self.panel_open else (WINDOW_W, WINDOW_H)
            self.resize_window(*base)
            if self.card is not None and not self.card.IsDisposed:
                self.invoke_on_card(lambda: self.enable_acrylic(self.card))
        except Exception as exc:  # noqa: BLE001
            log("应用窗口设置失败：%r" % (exc,))

    def allowed_motion_count(self) -> int:
        allowed = self.settings.get("motionAllowed")
        if not isinstance(allowed, list):
            allowed = ["*"]
        total = len(self.motion_inventory())
        if "*" in allowed:
            return total
        return len([m for m in self.motion_inventory() if m["id"] in allowed])

    def allowed_expression_count(self) -> int:
        allowed = self.settings.get("expressionAllowed")
        if not isinstance(allowed, list):
            allowed = ["*"]
        total = len(self.expression_names())
        if "*" in allowed:
            return total
        return len([n for n in self.expression_names() if n in allowed])

    def run_action(self, name: str) -> None:
        if name == "quit":
            self.form.Close()
        elif name == "reload":
            self.reload_page()
        elif name == "open-log":
            os.startfile(log_path)  # noqa: S606
        elif name == "open-folder":
            os.startfile(HERE)  # noqa: S606
        elif name == "open-codex":
            self.focus_codex()
        elif name == "toggle-panel":
            self.toggle_card()
        elif name == "reset-position":
            rect = self.screen_area()
            self.form.Location = self.Point(rect[2] - self.form.ClientSize.Width - 24, rect[3] - self.form.ClientSize.Height - 8)
            save_position(self.form.Left, self.form.Top)
            log("桌宠位置已重置到右下角")
        elif name in ("autostart-on", "autostart-off"):
            self.toggle_autostart(name.endswith("on"))
        elif name == "clear-log":
            cleared = []
            try:
                if os.path.exists(log_path):
                    open(log_path, "w", encoding="utf-8").close()
                    cleared.append(os.path.basename(log_path))
            except OSError:
                pass
            log("已清空日志：%s" % (", ".join(cleared) or "无"), force=True)
            self.push_settings()
        elif name == "clear-cache":
            self.clear_webview_cache()
        elif name == "create-desktop-link":
            self.create_desktop_link()
        elif name == "remove-desktop-link":
            self.remove_desktop_link()
        elif name == "motion":
            self.post({"t": "say", "text": "随机动作"})

    def screen_area(self):
        from System.Windows.Forms import Screen

        area = Screen.PrimaryScreen.WorkingArea
        return (area.Left, area.Top, area.Right, area.Bottom)

    def toggle_autostart(self, enable: bool) -> None:
        script = os.path.join(HERE, "pet-launcher.ps1")
        flag = "-Install" if enable else "-Uninstall"
        target = startup_link_path()
        if enable and not os.path.exists(script):
            log("找不到 pet-launcher.ps1，无法设置开机自启")
            return
        try:
            import subprocess

            creation = 0x08000000  # CREATE_NO_WINDOW
            subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, flag],
                cwd=HERE,
                creationflags=creation,
                timeout=30,
            )
            done = os.path.exists(target) if enable else not os.path.exists(target)
            log("开机自启已%s%s" % ("开启" if enable else "关闭", "" if done else "（未能确认，请查看 startup 文件夹）"))
        except Exception as exc:  # noqa: BLE001
            log("切换开机自启失败：%r" % (exc,))
        self.push_settings()

    def run_selftest(self) -> None:
        """Load the UI for real, then ask the page what it thinks it is."""
        deadline = time.time() + self.selftest
        while time.time() < deadline:
            if self.ready.is_set():
                break
            time.sleep(0.2)
        time.sleep(3.0)
        self.post({"t": "status-request"})
        self.status_ready.wait(6.0)
        result = dict(self.status or {"error": "界面没有回报状态"})
        if self.shot:
            self.post({"t": "snapshot-request"})
            if self.snapshot_ready.wait(10.0) and self.snapshot.startswith("data:image/png;base64,"):
                raw = base64.b64decode(self.snapshot.split(",", 1)[1])
                with open(self.shot, "wb") as fh:
                    fh.write(raw)
                result["snapshot"] = self.shot
                result["snapshotBytes"] = len(raw)
            else:
                result["snapshot"] = "failed"

        hwnd = self.form.Handle.ToInt64()
        result["hwnd"] = hwnd
        result["visible"] = bool(user32.IsWindowVisible(wt.HWND(hwnd)))
        result["desktop"] = desktop_name(user32.GetThreadDesktop(kernel32.GetCurrentThreadId()))
        result["exstyle"] = hex(user32.GetWindowLongW(wt.HWND(hwnd), GWL_EXSTYLE) & 0xFFFFFFFF)
        result["virtualDesktop"] = self.desktops.guid_text(self.desktops.desktop_of(hwnd)) if self.desktops else "?"
        result["client"] = [self.form.ClientSize.Width, self.form.ClientSize.Height]
        result["location"] = [self.form.Left, self.form.Top]
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        log("自检完成")


# -------------------------------------------------------------------- state feed


def usage_line(host: PetHost) -> str:
    """进度卡片右下角那行小字：今日新增 / 本会话累积（设置里可切、可关）。

    只读本地记录、不联网；算不出来就返回空字符串，卡片那格留空。
    """
    try:
        import usage_stats

        settings = host.settings or {}
        mode = str(settings.get("usageMode") or "today")
        agent = str(settings.get("agent") or "codex")
        return usage_stats.usage_text(mode, agent)
    except Exception as exc:  # noqa: BLE001
        log("读取用量失败：%r" % (exc,))
        return ""


def state_loop(host: PetHost, interval: float = 1.0) -> None:
    import agent_state

    last_payload = None
    last_send = 0.0
    last_tick = 0.0
    agent_id = ""
    while True:
        try:
            agent_id = str((host.settings or {}).get("agent") or "codex")
            payload = agent_state.snapshot(agent_id)
        except Exception as exc:  # noqa: BLE001
            log("读取 %s 状态失败：%r" % (agent_id or "Agent", exc,))
            payload = {"state": "idle", "label": "空闲", "title": "", "activity": ""}
        payload["usage"] = usage_line(host)      # 卡片右下角那行小字（今日新增 / 本会话累积）
        trimmed = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        host.last_payload = payload
        host.schedule_card_update(payload)
        now = time.time()
        if trimmed != last_payload or now - last_send > 15:
            host.post({"t": "state", "data": payload})
            last_payload = trimmed
            last_send = now
        elif host.inline_card_open and now - last_tick >= 2.0:
            # 卡片没展开就没必要每秒投递；开着也 2 秒一次 —— 每次投递都有卡住的风险
            host.post({"t": "tick", "data": {"elapsed": payload.get("elapsed"), "state": payload.get("state")}})
            last_tick = now
        time.sleep(interval)


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent 桌宠")
    parser.add_argument("--selftest", type=float, default=0, help="加载界面 N 秒后自动截取状态并退出")
    parser.add_argument("--state-only", action="store_true", help="只打印状态，不开窗口")
    parser.add_argument("--agent", default="", help="配合 --state-only：读哪个 agent（默认用设置里的）")
    parser.add_argument("--shot", default="", help="把形象画布导出成 PNG（自检用）")
    args = parser.parse_args()

    if args.state_only:
        import agent_state

        agent_id = args.agent or str(settings_store.load().get("agent") or "codex")
        print(json.dumps(agent_state.snapshot(agent_id), ensure_ascii=False, indent=2))
        return 0

    if not single_instance():
        print("Agent 桌宠已经在运行了", flush=True)
        return 0

    global verbose_log
    verbose_log = bool(settings_store.load().get("verboseLog", False))

    try:
        with open(PID_FILE, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
        if os.path.exists(STOP_FILE):
            os.remove(STOP_FILE)
    except OSError:
        pass

    set_dpi_awareness()
    install_exception_logging()
    install_stack_dumps()
    port = start_server()

    result = {}

    def gui() -> None:
        if not os.environ.get("CODEXPET_NO_HOP"):
            hop_to_interactive_desktop()
        init_sta()
        import clr

        clr.AddReference("System.Windows.Forms")
        from System.Windows.Forms import Application

        host = PetHost(port, args.selftest, args.shot)
        form = host.build()
        threading.Thread(target=state_loop, args=(host,), daemon=True).start()
        if args.selftest:
            threading.Thread(target=lambda: (time.sleep(1.0), host.run_selftest(), Application.Exit()), daemon=True).start()
        Application.Run(form)
        result["done"] = True

    thread = threading.Thread(target=gui)
    thread.start()
    thread.join()
    try:
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())









