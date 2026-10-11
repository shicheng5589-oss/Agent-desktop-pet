"""桌宠设置：一个很小的 JSON 文件，设置窗口和桌宠本体都读写它。"""

from __future__ import annotations

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(HERE, "pet_settings.json")

DEFAULT_EXPRESSIONS = {
    "thinking": "呆呆眼",
    "working": "开心兴奋",
    "waiting": "感叹号",
    "review": "方眼镜",
    "syncing": "流汗",
    "blocked": "生气",
    "error": "哭",
    "done": "爱心眼",
    "interrupted": "晕晕",
}

BOTTOM_BUTTONS = ("undo", "pencil", "eraser")
BOTTOM_EVENTS = ("single", "double", "right")
BOTTOM_ACTIONS = {
    "none",
    "open-settings", "open-menu", "toggle-edit-mode", "toggle-position-lock",
    "reset-default", "undo-expression", "clear-card", "hide-pet", "mute-animations",
    "codex-new-session", "codex-focus", "codex-rename-task", "codex-add-note",
    "codex-interrupt", "codex-undo-question", "codex-clear-context", "codex-end-session",
}

DEFAULT_BOTTOM_ACTIONS = {
    "assistant": {
        "undo": {"single": "undo-expression", "double": "none", "right": "open-settings"},
        "pencil": {"single": "toggle-edit-mode", "double": "none", "right": "open-menu"},
        "eraser": {"single": "hide-pet", "double": "none", "right": "mute-animations"},
    },
    "codex": {
        "undo": {"single": "codex-interrupt", "double": "none", "right": "none"},
        "pencil": {"single": "codex-new-session", "double": "none", "right": "codex-add-note"},
        "eraser": {"single": "codex-clear-context", "double": "none", "right": "none"},
    },
}

# 三个图标的触发方式："single" = 单击触发（现在的方式，双击手感不好已经取消）；
# 老配置文件里没有这个键，load() 会据此把当时配在"双击"上的功能搬到"单击"上。
DEFAULT_BOTTOM_TRIGGER = "single"


def clean_bottom_actions(mode: str, value) -> dict:
    mode = mode if mode in DEFAULT_BOTTOM_ACTIONS else "assistant"
    source = value if isinstance(value, dict) else {}
    result = {}
    for button in BOTTOM_BUTTONS:
        saved = source.get(button) if isinstance(source.get(button), dict) else {}
        result[button] = {}
        for event in BOTTOM_EVENTS:
            action = saved.get(event, DEFAULT_BOTTOM_ACTIONS[mode][button][event])
            result[button][event] = action if action in BOTTOM_ACTIONS else "none"
    return result


DEFAULTS = {
    "agent": "codex",        # 接入的 Agent（见 agent_state.py 的注册表）：卡片/表情跟着它走
    "scale": 1.0,
    "opacity": 1.0,
    "alwaysOnTop": True,
    "followDesktop": True,
    "showChip": True,
    "cardOpen": False,       # 进度卡片上次是开着还是收起的：启动时按这个状态还原
    "usageMode": "today",    # 卡片底部用量显示：today=今日新增（多 agent 合计）/ session=本会话累积 / off=不显示
    "showPanelOnChange": True,
    "gazeRange": 320.0,      # 视线跟随范围（像素，0 = 不跟随）
    "autoExpression": True,   # 自动表情切换（关闭后只显示状态对应表情）
    "autoMotion": True,       # 自动动作/闲置表演（关闭后停止并保持静止）
    "moodSeconds": 11.0,     # 表情最短保持时间（秒）
    "fpsLimit": 60.0,        # 渲染帧率上限（帧/秒；0 = 不限制，越大越顺滑也越费电）
    "moodChance": 80.0,      # 到点了真的换表情的概率（%）
    "motionChance": 30.0,    # 每次换表情时顺带做个动作的概率（%）
    "idleMotionSeconds": 6.0,# 闲置动作间隔（秒，0 = 不做闲置动作）
    "motionGapSeconds": 4.0, # 两次动作之间至少间隔（秒）
    "motionAllowed": ["*"],  # 允许播放的动作 id（["*"] = 全部；[] = 一个都不放）
    "expressionAllowed": ["*"],  # 允许出现的表情名（["*"] = 全部；[] = 一个都不换）
    "wheelZoom": True,       # 鼠标滚轮缩放（用全局钩子，个别机器上可以关掉）
    "cardBlur": True,        # 卡片毛玻璃（个别驱动上圆角会有残留，可以关掉只保留半透明）
    "cardStyle": "glass",    # "glass" = 最初版的半透明+模糊浮层；"solid" = 简洁实色卡片
    "cardGap": 12.0,         # 进度卡片与角色头顶的间距（CSS 像素）
    "cardScaleWithPet": True,# 卡片字号/内边距是否跟着桌宠一起缩放
    "verboseLog": False,     # 是否把鼠标事件也写进日志（排查用，默认关）
    "bottomButtonsMode": "assistant",  # "assistant" = 桌宠工具；"codex" = Codex 联动
    "bottomTrigger": DEFAULT_BOTTOM_TRIGGER,  # 三个图标的触发方式（见上面注释）
    "bottomButtonActions": clean_bottom_actions("assistant", None),
    "expressions": dict(DEFAULT_EXPRESSIONS),
}


def load() -> dict:
    data = dict(DEFAULTS)
    data["expressions"] = dict(DEFAULT_EXPRESSIONS)
    migrated = False
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as fh:
            saved = json.load(fh)
    except Exception:
        return data
    if isinstance(saved, dict):
        # 老配置（2026-09-25 之前）三个图标是"双击"触发的，文件里没有 bottomTrigger。
        # 这种情况把当时配在"双击"上的功能搬到"单击"上，省得重新配一遍。
        # 只要保存过一次（save() 会写进 bottomTrigger），就不会再搬第二次。
        migrated = "bottomTrigger" not in saved
        for key, value in saved.items():
            if key == "expressions" and isinstance(value, dict):
                merged = dict(DEFAULT_EXPRESSIONS)
                merged.update({k: v for k, v in value.items() if isinstance(v, str)})
                data["expressions"] = merged
            elif key == "bottomButtonsMode":
                data[key] = value if value in DEFAULT_BOTTOM_ACTIONS else "assistant"
            elif key == "bottomButtonActions":
                data[key] = clean_bottom_actions(data.get("bottomButtonsMode", "assistant"), value)
            elif key in DEFAULTS:
                data[key] = value
    if migrated:
        actions = data.get("bottomButtonActions")
        if isinstance(actions, dict):
            for button in BOTTOM_BUTTONS:
                entry = actions.get(button)
                if not isinstance(entry, dict):
                    continue
                double = entry.get("double")
                if isinstance(double, str) and double and double != "none":
                    entry["single"] = double
    return data


def save(data: dict) -> dict:
    clean = dict(DEFAULTS)
    clean["expressions"] = dict(DEFAULT_EXPRESSIONS)
    if isinstance(data, dict):
        for key, value in data.items():
            if key == "expressions" and isinstance(value, dict):
                merged = dict(DEFAULT_EXPRESSIONS)
                merged.update({k: v for k, v in value.items() if isinstance(v, str)})
                clean["expressions"] = merged
            elif key == "bottomButtonsMode":
                clean[key] = value if value in DEFAULT_BOTTOM_ACTIONS else "assistant"
            elif key == "bottomButtonActions":
                clean[key] = clean_bottom_actions(clean.get("bottomButtonsMode", "assistant"), value)
            elif key in DEFAULTS:
                clean[key] = value
    clean["agent"] = str(clean.get("agent") or "codex").strip()[:24] or "codex"
    clean["usageMode"] = clean.get("usageMode") if clean.get("usageMode") in ("today", "session", "off") else "today"
    clean["scale"] = max(0.05, min(4.0, float(clean["scale"])))
    clean["opacity"] = max(0.35, min(1.0, float(clean["opacity"])))
    clean["gazeRange"] = max(0.0, min(2000.0, float(clean["gazeRange"])))
    clean["autoExpression"] = bool(clean["autoExpression"])
    clean["autoMotion"] = bool(clean["autoMotion"])
    clean["moodSeconds"] = max(2.0, min(120.0, float(clean["moodSeconds"])))
    clean["fpsLimit"] = max(0.0, min(360.0, float(clean["fpsLimit"])))   # 0 = 不限制
    clean["moodChance"] = max(0.0, min(100.0, float(clean["moodChance"])))
    clean["motionChance"] = max(0.0, min(100.0, float(clean["motionChance"])))
    clean["idleMotionSeconds"] = max(0.0, min(300.0, float(clean["idleMotionSeconds"])))
    clean["motionGapSeconds"] = max(0.0, min(120.0, float(clean["motionGapSeconds"])))
    clean["cardGap"] = max(-20.0, min(60.0, float(clean["cardGap"])))
    clean["cardScaleWithPet"] = bool(clean["cardScaleWithPet"])
    clean["verboseLog"] = bool(clean["verboseLog"])
    if not isinstance(clean.get("motionAllowed"), list):
        clean["motionAllowed"] = []
    if not isinstance(clean.get("expressionAllowed"), list):
        clean["expressionAllowed"] = ["*"]
    clean["alwaysOnTop"] = bool(clean["alwaysOnTop"])
    clean["followDesktop"] = bool(clean["followDesktop"])
    clean["showChip"] = bool(clean["showChip"])
    clean["cardOpen"] = bool(clean.get("cardOpen", False))
    clean["showPanelOnChange"] = bool(clean["showPanelOnChange"])
    with open(SETTINGS_FILE, "w", encoding="utf-8") as fh:
        json.dump(clean, fh, ensure_ascii=False, indent=2)
    return clean


if __name__ == "__main__":
    print(json.dumps(load(), ensure_ascii=False, indent=2))
