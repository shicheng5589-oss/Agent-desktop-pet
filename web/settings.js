/* 设置界面：改动先标记为未保存，点「保存并应用」后才真正生效。 */

const STATES = [
  ["thinking", "思考中", "#7aa2ff"],
  ["working", "执行中", "#39c07a"],
  ["waiting", "等待你", "#ffb340"],
  ["review", "审阅 / 找工具", "#b58cff"],
  ["syncing", "同步中", "#4fc3d9"],
  ["blocked", "被挡住", "#ff6b6b"],
  ["error", "出错了", "#ff4d4d"],
  ["done", "完成了", "#ffd166"],
  ["interrupted", "被打断", "#9b9b9b"],
];

const BOTTOM_BUTTONS = [
  ["undo", "↩️ 撤回", "#5b8cff"],
  ["pencil", "📝 铅笔", "#ffb340"],
  ["eraser", "🧽 橡皮擦", "#39c07a"],
];

// 每个按钮用「单击 / 右键」两种触发方式（双击手感不好，已经取消）
const BOTTOM_EVENTS = [
  ["single", "单击"],
  ["right", "右键"],
];

// 每一项：[值, 下拉里显示的名字, 鼠标悬停时的完整说明（可选）]
const BOTTOM_ACTION_OPTIONS = {
  assistant: [
    ["none", "无功能"],
    ["open-settings", "打开设置面板"],
    ["open-menu", "打开右键菜单"],
    ["toggle-edit-mode", "切换编辑模式"],
    ["toggle-position-lock", "锁定位置", "锁定 / 解锁位置"],
    ["reset-default", "恢复默认状态"],
    ["undo-expression", "撤销上次表情", "撤销上一次表情"],
    ["clear-card", "清空进度卡片"],
    ["hide-pet", "隐藏到托盘", "隐藏到系统托盘"],
    ["mute-animations", "暂时静音动画"],
  ],
  codex: [
    ["none", "无功能"],
    ["codex-new-session", "新建会话", "新建 Codex 会话"],
    ["codex-focus", "切到 Agent 窗口", "切到所选的 Agent 的窗口（Codex / Claude / Gemini / OpenClaw / Hermes / DeepSeek Harness / ZCode / WorkBuddy / OpenCode / Kimi Code / Xiaomi MiMo / Qwen Code）"],
    ["codex-rename-task", "重命名任务", "重命名当前任务（复制新名称）"],
    ["codex-add-note", "添加备注"],
    ["codex-interrupt", "打断任务", "打断当前任务"],
    ["codex-undo-question", "撤回输入", "撤回上一步输入"],
    ["codex-clear-context", "清空上下文", "清空当前上下文（新建空白会话）"],
    ["codex-end-session", "结束会话", "结束当前会话（关闭 Codex 窗口）"],
  ],
};

const BOTTOM_DEFAULT_ACTIONS = {
  assistant: {
    undo: { single: "undo-expression", double: "none", right: "open-settings" },
    pencil: { single: "toggle-edit-mode", double: "none", right: "open-menu" },
    eraser: { single: "hide-pet", double: "none", right: "mute-animations" },
  },
  codex: {
    undo: { single: "codex-interrupt", double: "none", right: "none" },
    pencil: { single: "codex-new-session", double: "none", right: "codex-add-note" },
    eraser: { single: "codex-clear-context", double: "none", right: "none" },
  },
};

// 各项的默认值：底下"恢复默认"（整页 / 每一栏）都用这一份
const DEFAULTS = {
  scale: 1,
  opacity: 1,
  gazeRange: 320,
  autoExpression: true,
  autoMotion: true,
  alwaysOnTop: true,
  showChip: true,
  fpsLimit: 60,
  cardStyle: "glass",
  cardGap: 12,
  cardScaleWithPet: true,
  followDesktop: true,
  autostart: false,
  agent: "codex",
  usageMode: "today",
  wheelZoom: true,
  cardBlur: true,
  verboseLog: false,
  moodSeconds: 11,
  moodChance: 80,
  motionChance: 30,
  idleMotionSeconds: 6,
  motionGap: 4,
  bottomButtonsMode: "assistant",
};

// 状态 → 表情 的出厂默认（和 settings_store.py 里的 DEFAULT_EXPRESSIONS 一致）
const STATE_DEFAULT_EXPR = {
  thinking: "呆呆眼",
  working: "开心兴奋",
  waiting: "感叹号",
  review: "方眼镜",
  syncing: "流汗",
  blocked: "生气",
  error: "哭",
  done: "爱心眼",
  interrupted: "晕晕",
};

// 每一栏"恢复默认"管哪些设置
const RESET_SCOPES = {
  appearance: ["scale", "opacity", "gazeRange", "autoExpression", "autoMotion", "alwaysOnTop", "showChip", "fpsLimit"],
  agent: ["agent", "usageMode", "autostart"],
  card: ["cardStyle", "cardGap", "cardScaleWithPet", "followDesktop", "wheelZoom", "cardBlur"],
  bottom: ["bottomButtonsMode"],
  motions: ["moodSeconds", "moodChance", "motionChance", "idleMotionSeconds", "motionGap"],
  debug: ["verboseLog"],
};
const RESET_SCOPE_NAMES = {
  appearance: "外观",
  agent: "Agent",
  card: "卡片",
  bottom: "底部按钮与功能方案",
  states: "状态对应的表情",
  whitelist: "表情白名单",
  motions: "动作与表情播放",
  debug: "排查",
};

const $ = (id) => document.getElementById(id);

const bridge = (() => {
  const wv = window.chrome && window.chrome.webview;
  if (wv) {
    return { send: (m) => wv.postMessage(m), on: (fn) => wv.addEventListener("message", (e) => fn(e.data)) };
  }
  return {
    send: (m) => {
      (window.__sent = window.__sent || []).push(m);
      if (window.__onSend) window.__onSend(m);
    },
    on: (fn) => {
      window.__receive = fn;
    },
  };
})();

let current = null;
let statusTimer = 0;
let baseline = null;   // 最近一次"已应用"的快照；和当前界面不一致就是有未保存的改动
let renderFps = 0;     // 桌宠上报的实时渲染帧率（用来在"渲染帧率上限"旁边显示效果）

function flash(text) {
  $("status").textContent = text;
  clearTimeout(statusTimer);
  statusTimer = setTimeout(() => ($("status").textContent = ""), 1800);
}

function buildStateGrid(expressions, mapping) {
  const grid = $("state-grid");
  grid.innerHTML = "";
  for (const [key, label, color] of STATES) {
    const item = document.createElement("div");
    item.className = "state-item";
    const dot = document.createElement("span");
    dot.className = "dot";
    dot.style.background = color;
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = label;
    const select = document.createElement("select");
    select.dataset.state = key;
    const none = document.createElement("option");
    none.value = "";
    none.textContent = "（不改变表情）";
    select.appendChild(none);
    for (const expr of expressions) {
      const option = document.createElement("option");
      option.value = expr;
      option.textContent = expr;
      select.appendChild(option);
    }
    select.value = (mapping && mapping[key]) || "";
    select.addEventListener("change", () => {
      collect().expressions[key] = select.value;
      push();
    });
    item.append(dot, name, select);
    grid.appendChild(item);
  }
}

function collectBottomActions() {
  const saved = (current && current.bottomButtonActions) || {};
  const result = {};
  for (const [button] of BOTTOM_BUTTONS) {
    result[button] = {};
    for (const event of ["single", "double", "right"]) {
      const select = document.querySelector(
        `#bottom-button-actions select[data-button="${button}"][data-event="${event}"]`
      );
      if (select) {
        result[button][event] = select.value;
      } else {
        // 界面上已经没有这一项了（比如"双击"已经取消）——保留原来存的值，别偷偷清掉
        result[button][event] = (saved[button] && saved[button][event]) || "none";
      }
    }
  }
  return result;
}

function buildBottomActionGrid(mode, actions) {
  const container = $("bottom-button-actions");
  container.innerHTML = "";
  const options = BOTTOM_ACTION_OPTIONS[mode] || BOTTOM_ACTION_OPTIONS.assistant;

  // 和「状态对应的表情」完全一样的排版：圆点 + 名称 + 下拉，自动铺满多列
  for (const [button, label, color] of BOTTOM_BUTTONS) {
    for (const [event, eventLabel] of BOTTOM_EVENTS) {
      const item = document.createElement("div");
      item.className = "state-item bottom-action-item";

      const dot = document.createElement("span");
      dot.className = "dot";
      dot.style.background = color;

      const name = document.createElement("span");
      name.className = "name";
      name.textContent = `${label} · ${eventLabel}`;

      const select = document.createElement("select");
      select.dataset.button = button;
      select.dataset.event = event;
      select.setAttribute("aria-label", `${label} ${eventLabel}`);
      for (const [value, text, tip] of options) {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = text;
        if (tip) option.title = tip;
        select.appendChild(option);
      }
      const wanted = actions && actions[button] && actions[button][event];
      select.value = options.some(([value]) => value === wanted) ? wanted : "none";

      const syncTitle = () => {
        const picked = select.selectedOptions[0];
        select.title = picked ? picked.title || picked.textContent : "";
      };
      syncTitle();
      select.addEventListener("change", syncTitle);

      item.append(dot, name, select);
      container.appendChild(item);
    }
  }
}

function collect() {
  const expressions = {};
  document.querySelectorAll("#state-grid select").forEach((sel) => {
    expressions[sel.dataset.state] = sel.value;
  });
  return {
    scale: Number($("scale").value),
    opacity: Number($("opacity").value),
    gazeRange: Number($("gazeRange").value),
    autoExpression: $("autoExpression").checked,
    autoMotion: $("autoMotion").checked,
    moodSeconds: Number($("moodSeconds").value),
    fpsLimit: Number($("fpsLimit").value),
    alwaysOnTop: $("alwaysOnTop").checked,
    showChip: $("showChip").checked,
    followDesktop: $("followDesktop").checked,
    usageMode: $("usageMode").value,
    wheelZoom: $("wheelZoom").checked,
    cardBlur: $("cardBlur").checked,
    cardStyle: $("cardStyle").value,
    cardGap: Number($("cardGap").value),
    cardScaleWithPet: $("cardScaleWithPet").checked,
    verboseLog: $("verboseLog").checked,
    bottomButtonsMode: $("bottomButtonsMode").value,
    bottomButtonActions: collectBottomActions(),
    moodChance: Number($("moodChance").value),
    motionChance: Number($("motionChance").value),
    idleMotionSeconds: Number($("idleMotionSeconds").value),
    motionGapSeconds: Number($("motionGap").value),
    motionAllowed: (() => {
      const boxes = [...document.querySelectorAll("#motion-list input")];
      if (!boxes.length) {
        return current && Array.isArray(current.motionAllowed) ? current.motionAllowed : ["*"];
      }
      const checked = boxes.filter((el) => el.checked).map((el) => el.dataset.id);
      return checked.length === boxes.length ? ["*"] : checked;
    })(),
    expressionAllowed: (() => {
      const boxes = [...document.querySelectorAll("#expression-list input")];
      if (!boxes.length) {
        return current && Array.isArray(current.expressionAllowed) ? current.expressionAllowed : ["*"];
      }
      const checked = boxes.filter((el) => el.checked).map((el) => el.dataset.name);
      return checked.length === boxes.length ? ["*"] : checked;
    })(),
    expressions,
    // 开机自启：保存时和后端的"启动项是否存在"比一下，不一致就装上 / 卸掉
    autostart: $("autostart").checked,
    agent: $("agent").value || "codex",
  };
}

function push() {
  markDirty();
}

function markDirty() {
  const dirty = baseline !== null && JSON.stringify(collect()) !== baseline;
  $("dirty").textContent = dirty ? "● 有未保存的更改" : "";
  $("act-save").disabled = !dirty;
  $("act-revert").disabled = !dirty;
}

function saveNow() {
  const data = collect();
  bridge.send({ t: "save", data });
  baseline = JSON.stringify(data);
  markDirty();
  flash("已应用 ✓");
}

let agentStatus = {};   // 各 agent 的本地记录检测结果（后端 push_settings 送来）

/** 接入的 Agent：两列网格（第一列先排满，再排第二列）。 */
function fillAgentList(list) {
  const box = $("agent");
  if (!box || box.dataset.filled === "1") return;
  const items = list && list.length ? list : [{ id: "codex", name: "Codex", shared: true }];
  const rows = Math.max(1, Math.ceil(items.length / 2));
  box.style.gridTemplateRows = "repeat(" + rows + ", minmax(0, auto))";
  items.forEach((item) => {
    const cell = document.createElement("button");
    cell.type = "button";
    cell.className = "agent-cell";
    cell.dataset.agentId = item.id;
    cell.dataset.shared = item.shared ? "1" : "0";
    cell.setAttribute("role", "radio");
    cell.setAttribute("aria-checked", "false");
    const name = document.createElement("span");
    name.className = "agent-name";
    name.textContent = item.name;
    const tick = document.createElement("span");
    tick.className = "tick";
    tick.textContent = "✓";
    cell.append(name, tick);
    cell.addEventListener("click", () => selectAgent(item.id));
    box.appendChild(cell);
  });
  box.dataset.filled = "1";
}

/** 选中某个 agent：点亮 + 刷新记录提示。silent = 回填页面时用（不动"未保存"状态）。 */
function selectAgent(id, silent) {
  const box = $("agent");
  if (!box) return;
  const want = id || "codex";
  box.value = want;
  box.querySelectorAll(".agent-cell").forEach((cell) => {
    const on = cell.dataset.agentId === want;
    cell.classList.toggle("active", on);
    cell.setAttribute("aria-checked", on ? "true" : "false");
  });
  updateAgentHint();
  if (!silent) push();
}

/** 选中的 agent 有没有本地记录可读。桌面版/CLI 共用记录的 agent 不提示（它们本来就一直有）。 */
function updateAgentHint() {
  const el = $("agent-hint");
  const sel = $("agent");
  if (!el || !sel) return;
  const id = sel.value || "codex";
  const cell = sel.querySelector('.agent-cell[data-agent-id="' + id + '"]');
  const info = agentStatus[id] || {};
  const shared = (cell && cell.dataset.shared === "1") || id === "codex";
  if (shared) {
    el.textContent = "";
    el.style.display = "none";
    return;
  }
  const bridge = "~/.codexpet/status/" + id + ".json";
  el.style.display = "";
  if (info.found) {
    el.textContent = "✓ 可读本地记录（" + (info.detail || "已检测到") + "）";
    el.style.color = "#3a9d5d";
  } else {
    el.textContent = "○ 还没读到本地记录——用它跑一次就有了；也可以走状态桥：" +
      "python status-bridge.py --agent " + id + " --state working（写进 " + bridge + "）";
    el.style.color = "#8a6d1f";
  }
}

function render(cfg) {
  const __t0 = performance.now();
  current = cfg;
  if (cfg.scaleMin != null) $("scale").min = cfg.scaleMin;
  if (cfg.scaleMax != null) $("scale").max = cfg.scaleMax;
  $("scale").value = cfg.scale != null ? cfg.scale : 1;
  $("opacity").value = cfg.opacity != null ? cfg.opacity : 1;
  $("gazeRange").value = cfg.gazeRange != null ? cfg.gazeRange : 320;
  $("autoExpression").checked = cfg.autoExpression !== false;
  $("autoMotion").checked = cfg.autoMotion !== false;
  $("moodSeconds").value = cfg.moodSeconds != null ? cfg.moodSeconds : 11;
  $("fpsLimit").value = String(cfg.fpsLimit != null ? cfg.fpsLimit : 60);
  renderFps = Number(cfg.renderFps || 0) || 0;
  $("alwaysOnTop").checked = cfg.alwaysOnTop !== false;
  $("showChip").checked = cfg.showChip !== false;
  $("followDesktop").checked = cfg.followDesktop !== false;
  $("usageMode").value = cfg.usageMode || "today";
  $("wheelZoom").checked = cfg.wheelZoom !== false;
  $("cardBlur").checked = cfg.cardBlur !== false;
  $("cardStyle").value = cfg.cardStyle === "solid" ? "solid" : "glass";
  $("cardGap").value = cfg.cardGap != null ? cfg.cardGap : 12;
  $("cardScaleWithPet").checked = cfg.cardScaleWithPet !== false;
  $("verboseLog").checked = cfg.verboseLog === true;
  const bottomMode = cfg.bottomButtonsMode === "codex" ? "codex" : "assistant";
  $("bottomButtonsMode").value = bottomMode;
  buildBottomActionGrid(bottomMode, cfg.bottomButtonActions || BOTTOM_DEFAULT_ACTIONS[bottomMode]);
  $("moodChance").value = cfg.moodChance != null ? cfg.moodChance : 80;
  $("motionChance").value = cfg.motionChance != null ? cfg.motionChance : 30;
  $("idleMotionSeconds").value = cfg.idleMotionSeconds != null ? cfg.idleMotionSeconds : 6;
  $("motionGap").value = cfg.motionGapSeconds != null ? cfg.motionGapSeconds : 4;
  $("autostart").checked = Boolean(cfg.autostart);
  agentStatus = cfg.agentStatus || {};
  fillAgentList(cfg.agentList);
  selectAgent(cfg.agent || "codex", true);
  updateAgentHint();
  $("expr-count").textContent = (cfg.availableExpressions || []).length;
  syncOutputs();
  $("expr-total").textContent = String((cfg.availableExpressions || []).length);
  const now = cfg.petNow || {};
  const allowed = cfg.allowed || {};
  $("now").textContent =
    `桌宠此刻：表情 ${now.expression || "（无）"} · 最近动作 ${now.lastMotion || "（无）"}` +
    ` · 白名单已允许 动作 ${allowed.motions ?? "?"}/${allowed.motionTotal ?? "?"}` +
    `、表情 ${allowed.expressions ?? "?"}/${allowed.expressionTotal ?? "?"}`;
  $("scale-hint").textContent =
    cfg.scaleMin != null && cfg.scaleMax != null
      ? `可调范围 ${Math.round(cfg.scaleMin * 100)}%–${Math.round(cfg.scaleMax * 100)}%（上限按你的屏幕高度兜底）`
      : "";
  if (cfg.logPath) $("paths").textContent = `日志：${cfg.logPath}`;
  $("cache-size").textContent = cfg.cacheText || "—";
  const hasDesktopLink = Boolean(cfg.desktopLink);
  $("desktop-link").textContent = hasDesktopLink ? "已创建" : "未创建";
  $("desktop-link").dataset.exists = hasDesktopLink ? "1" : "0";
  $("act-desktop-link").textContent = hasDesktopLink ? "删除桌面快捷方式" : "创建桌面快捷方式";
  buildStateGrid(cfg.availableExpressions || [], cfg.expressions || {});
  buildMotionList(cfg.motions || [], cfg.motionAllowed || []);
  buildExpressionList(cfg.availableExpressions || [], cfg.expressionAllowed || ["*"]);
  baseline = JSON.stringify(collect());   // 刚刚同步下来的就是"已应用"的状态
  markDirty();
  const __spent = performance.now() - __t0;
  if (__spent > 300) bridge.send({ t: "perf", data: { renderMs: Math.round(__spent) } });
}

function buildExpressionList(expressions, allowed) {
  const container = $("expression-list");
  container.innerHTML = "";
  const ids = Array.isArray(allowed) ? allowed : ["*"];
  const enabled = ids.includes("*") ? null : new Set(ids);
  for (const name of expressions) {
    const item = document.createElement("label");
    item.className = "motion-item";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.dataset.name = name;
    box.checked = enabled ? enabled.has(name) : true;
    const text = document.createElement("span");
    text.textContent = name;
    item.append(box, text);
    container.appendChild(item);
  }
}

function buildMotionList(motions, allowed) {
  const container = $("motion-list");
  container.innerHTML = "";
  const ids = Array.isArray(allowed) ? allowed : ["*"];
  const enabled = ids.includes("*") ? null : new Set(ids); // null = 全选
  for (const motion of motions) {
    const item = document.createElement("label");
    item.className = "motion-item";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.dataset.id = motion.id;
    box.checked = enabled ? enabled.has(motion.id) : true;
    const text = document.createElement("span");
    text.textContent = `${motion.group} · ${String(motion.file).replace(/\.motion3\.json$/, "")}`;
    const tag = document.createElement("span");
    tag.className = "tag";
    tag.textContent = `${motion.duration}s`;
    item.append(box, text, tag);
    container.appendChild(item);
  }
  $("motion-count").textContent = String(motions.length);
}

/** 滑条旁边那些数字 / 单位重算一遍（改了值、填了默认值之后都要刷） */
function syncOutputs() {
  $("scale-out").textContent = Math.round(Number($("scale").value) * 100) + "%";
  $("opacity-out").textContent = Math.round(Number($("opacity").value) * 100) + "%";
  $("gaze-out").textContent = Number($("gazeRange").value) === 0 ? "关闭" : Number($("gazeRange").value) + " px";
  $("mood-out").textContent = Number($("moodSeconds").value) + " 秒";
  $("mood-chance-out").textContent = Number($("moodChance").value) + " %";
  $("motion-chance-out").textContent = Number($("motionChance").value) + " %";
  $("idle-out").textContent =
    Number($("idleMotionSeconds").value) === 0 ? "关闭" : Number($("idleMotionSeconds").value) + " 秒";
  $("motion-gap-out").textContent = Number($("motionGap").value) + " 秒";
  $("card-gap-out").textContent = Number($("cardGap").value) + " px";
  const fpsLimit = Number($("fpsLimit").value);
  const fpsNow = renderFps > 0 ? Math.round(renderFps) + " fps" : "—";
  $("fps-hint").textContent = `（当前 ${fpsNow}，上限 ${fpsLimit === 0 ? "不限" : fpsLimit + " fps"}）`;
}

/** 把指定的几项填回默认值（只动界面，点「保存并应用」才真正生效） */
function applyDefaults(keys) {
  for (const id of keys) {
    const el = $(id);
    if (!el) continue;
    const value = DEFAULTS[id];
    if (el.type === "checkbox") el.checked = Boolean(value);
    else el.value = value;
    if (id === "agent") selectAgent(value, true);
  }
}

/** 状态对应的表情：每一项都回到出厂默认 */
function resetStateExpressions() {
  document.querySelectorAll("#state-grid select").forEach((sel) => {
    const want = STATE_DEFAULT_EXPR[sel.dataset.state] || "";
    sel.value = [...sel.options].some((o) => o.value === want) ? want : "";
  });
}

/** 表情白名单 + 动作白名单：默认就是"全都允许" */
function resetWhitelists() {
  document.querySelectorAll("#expression-list input").forEach((el) => (el.checked = true));
  document.querySelectorAll("#motion-list input").forEach((el) => (el.checked = true));
}

/** 整页恢复默认时，把上面这几样一起重置 */
function resetExpressionsAndLists() {
  resetStateExpressions();
  resetWhitelists();
}

/** 某一栏右上角那个圆圈按钮：只把这一栏恢复默认 */
function resetScope(scope) {
  if (scope === "states") {
    resetStateExpressions();
  } else if (scope === "whitelist") {
    document.querySelectorAll("#expression-list input").forEach((el) => (el.checked = true));
  } else if (scope === "motions") {
    document.querySelectorAll("#motion-list input").forEach((el) => (el.checked = true));
    applyDefaults(RESET_SCOPES.motions);
  } else if (scope === "bottom") {
    applyDefaults(RESET_SCOPES.bottom);
    const mode = $("bottomButtonsMode").value === "codex" ? "codex" : "assistant";
    buildBottomActionGrid(mode, BOTTOM_DEFAULT_ACTIONS[mode]);
  } else {
    applyDefaults(RESET_SCOPES[scope] || []);
  }
  syncOutputs();
  markDirty();
  flash(`已把「${RESET_SCOPE_NAMES[scope] || scope}」填回默认值，点「保存并应用」生效`);
}

function wire() {
  const scale = $("scale");
  const opacity = $("opacity");
  scale.addEventListener("input", () => ($("scale-out").textContent = Math.round(Number(scale.value) * 100) + "%"));
  opacity.addEventListener("input", () => ($("opacity-out").textContent = Math.round(Number(opacity.value) * 100) + "%"));
  scale.addEventListener("change", push);
  opacity.addEventListener("change", push);
  $("gazeRange").addEventListener("input", () => {
    const v = Number($("gazeRange").value);
    $("gaze-out").textContent = v === 0 ? "关闭" : v + " px";
  });
  $("gazeRange").addEventListener("change", push);
  $("moodSeconds").addEventListener("input", () => ($("mood-out").textContent = Number($("moodSeconds").value) + " 秒"));
  $("moodSeconds").addEventListener("change", push);
  ["alwaysOnTop", "showChip", "followDesktop", "wheelZoom", "cardBlur", "usageMode"].forEach((id) =>
    $(id).addEventListener("change", push)
  );
  $("cardStyle").addEventListener("change", push);
  $("cardGap").addEventListener("input", () => {
    $("card-gap-out").textContent = Number($("cardGap").value) + " px";
  });
  $("cardGap").addEventListener("change", push);
  $("cardScaleWithPet").addEventListener("change", push);
  $("verboseLog").addEventListener("change", push);
  const liveLabels = {
    moodChance: ["mood-chance-out", (v) => v + " %"],
    motionChance: ["motion-chance-out", (v) => v + " %"],
    idleMotionSeconds: ["idle-out", (v) => (v === 0 ? "关闭" : v + " 秒")],
    motionGap: ["motion-gap-out", (v) => v + " 秒"],
  };
  for (const [id, [outId, fmt]] of Object.entries(liveLabels)) {
    $(id).addEventListener("input", () => {
      $(outId).textContent = fmt(Number($(id).value));
    });
    $(id).addEventListener("change", push);
  }
  $("bottomButtonsMode").addEventListener("change", () => {
    const mode = $("bottomButtonsMode").value === "codex" ? "codex" : "assistant";
    buildBottomActionGrid(mode, BOTTOM_DEFAULT_ACTIONS[mode]);
    push();
  });
  $("bottom-button-actions").addEventListener("change", push);
  $("motion-list").addEventListener("change", push);
  $("motion-all").addEventListener("click", () => {
    document.querySelectorAll("#motion-list input").forEach((el) => (el.checked = true));
    push();
  });
  $("motion-none").addEventListener("click", () => {
    document.querySelectorAll("#motion-list input").forEach((el) => (el.checked = false));
    push();
  });
  $("motion-calm").addEventListener("click", () => {
    // 一键安静：完全停止自动动作/表情随机切换，只保留状态表情与呼吸。
    document.querySelectorAll("#motion-list input").forEach((el) => (el.checked = false));
    $("autoExpression").checked = false;
    $("autoMotion").checked = false;
    $("gazeRange").value = 0;
    $("moodSeconds").value = 60;
    $("idleMotionSeconds").value = 0;
    $("motionChance").value = 0;
    $("moodChance").value = 0;
    $("gaze-out").textContent = "关闭";
    $("mood-out").textContent = "60 秒";
    $("idle-out").textContent = "关闭";
    $("motion-chance-out").textContent = "0 %";
    $("mood-chance-out").textContent = "0 %";
    document.querySelectorAll("#expression-list input").forEach((el) => (el.checked = true));
    markDirty();
    flash("已填入安静模式，点「保存并应用」生效");
  });
  $("expression-list").addEventListener("change", push);
  $("expr-all").addEventListener("click", () => {
    document.querySelectorAll("#expression-list input").forEach((el) => (el.checked = true));
    push();
  });
  $("expr-none").addEventListener("click", () => {
    document.querySelectorAll("#expression-list input").forEach((el) => (el.checked = false));
    push();
  });
  $("autostart").addEventListener("change", push);   // 和其它设置一样：点保存才生效
  $("fpsLimit").addEventListener("change", push);

  $("act-save").addEventListener("click", saveNow);
  $("act-revert").addEventListener("click", () => {
    if (current) render(current);
    flash("已放弃未保存的更改");
  });
  document.addEventListener("keydown", (ev) => {
    if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "s") {
      ev.preventDefault();
      saveNow();
    }
  });

  const actions = {
    "act-panel": "toggle-panel",
    "act-motion": "motion",
    "act-reset": "reset-position",
    "act-codex": "open-codex",
    "act-log": "open-log",
    "act-clear-log": "clear-log",
    "act-folder": "open-folder",
    "act-reload": "reload",
    "act-clear-cache": "clear-cache",
    "act-quit": "quit",
  };
  for (const [id, name] of Object.entries(actions)) {
    $(id).addEventListener("click", () => {
      if (name === "clear-cache") {
        const size = ($("cache-size") && $("cache-size").textContent) || "—";
        if (!window.confirm(`清除 WebView2 缓存（当前 ${size}）会关闭桌宠、清理后自动重启。\n首次加载会稍慢，继续吗？`)) return;
      }
      bridge.send({ t: "action", name });
      if (name !== "quit") flash("已发送 ✓");
    });
  }

  $("act-desktop-link").addEventListener("click", () => {
    const exists = ($("desktop-link") && $("desktop-link").dataset.exists) === "1";
    bridge.send({ t: "action", name: exists ? "remove-desktop-link" : "create-desktop-link" });
    flash(exists ? "已请求删除 ✓" : "已请求创建 ✓");
  });

  $("act-defaults").addEventListener("click", () => {
    // 只把界面填成默认值（不立即生效），点「保存并应用」后才真正生效
    applyDefaults(Object.keys(DEFAULTS));
    resetExpressionsAndLists();
    buildBottomActionGrid("assistant", BOTTOM_DEFAULT_ACTIONS.assistant);
    syncOutputs();
    markDirty();
    flash("已填入默认值，点「保存并应用」生效");
  });

  // 每一栏右上角那个圆圈按钮：只恢复这一栏
  document.querySelectorAll(".icon-btn[data-reset]").forEach((btn) => {
    btn.addEventListener("click", () => resetScope(btn.dataset.reset));
  });
}

bridge.on((raw) => {
  let msg = raw;
  if (typeof raw === "string") {
    try {
      msg = JSON.parse(raw);
    } catch (err) {
      return;
    }
  }
  if (msg && msg.t === "settings") render(msg.data || {});
  else if (msg && msg.t === "fps") {
    // 桌宠每 3 秒上报一次实际帧率：只刷新"渲染帧率上限"旁边那行提示
    renderFps = Number(msg.value || 0) || 0;
    syncOutputs();
  }
});

wire();
bridge.send({ t: "hello" });

// 浏览器里直接打开这个页面时，用一份假数据把界面画出来（方便检查排版）
if (!(window.chrome && window.chrome.webview)) {
  setTimeout(() => {
    render({
      scale: 1,
      opacity: 1,
      alwaysOnTop: true,
      followDesktop: true,
      showChip: true,
      autostart: false,
      agent: "codex",
      agentList: [
        { id: "codex", name: "Codex", shared: true },
        { id: "claude", name: "Claude Code CLI", shared: false },
        { id: "gemini", name: "Gemini CLI", shared: false },
        { id: "openclaw", name: "OpenClaw CLI", shared: false },
        { id: "hermes", name: "Hermes CLI", shared: false },
        { id: "opencode", name: "OpenCode", shared: true },
        { id: "dsh", name: "DeepSeek Harness", shared: true },
        { id: "workbuddy", name: "WorkBuddy", shared: true },
        { id: "mimo", name: "Xiaomi MiMo", shared: true },
        { id: "zcode", name: "ZCode", shared: true },
        { id: "kimi", name: "Kimi Code", shared: true },
        { id: "qwen", name: "Qwen Code CLI", shared: false },
      ],
      agentStatus: { claude: { found: true, detail: "最近更新 2 分钟前" } },
      usageMode: "today",
      availableExpressions: ["呆呆眼", "开心兴奋", "感叹号", "方眼镜", "流汗", "生气", "哭", "爱心眼", "晕晕", "调皮", "吐舌", "脸红", "星星眼", "猫猫贴纸", "问号"],
      expressions: { thinking: "呆呆眼", working: "开心兴奋", waiting: "感叹号", done: "爱心眼" },
      motions: [
        { id: "Idle:0", group: "Idle", index: 0, file: "idle.motion3.json", duration: 4 },
        { id: "Idle:1", group: "Idle", index: 1, file: "aidale.motion3.json", duration: 4.8 },
        { id: "Tap:0", group: "Tap", index: 0, file: "自拍简单.motion3.json", duration: 1.3 },
        { id: "Tap:1", group: "Tap", index: 1, file: "开盖.motion3.json", duration: 1 },
        { id: "Tap:2", group: "Tap", index: 2, file: "喷水.motion3.json", duration: 0.5 },
        { id: "Tap:3", group: "Tap", index: 3, file: "chuipaopao.motion3.json", duration: 5 },
        { id: "Show:0", group: "Show", index: 0, file: "自拍.motion3.json", duration: 3.3 },
        { id: "Show:1", group: "Show", index: 1, file: "番茄酱.motion3.json", duration: 5 },
      ],
      motionAllowed: ["*"],
      moodChance: 80,
      motionChance: 30,
      idleMotionSeconds: 6,
      motionGapSeconds: 4,
      cacheText: "372.4 MB",
      desktopLink: true,
      logPath: "C:\\...\\outputs\\CodexPet\\pet.log",
    });
  }, 50);
}
