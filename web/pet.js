/* Agent 桌宠前端：Live2D 形象 + 状态表情 + 进度卡片 */

const STATE_STYLE = {
  idle: { label: "空闲", color: "#7f8c9b", expr: null, motion: "Idle" },
  thinking: { label: "思考中", color: "#7aa2ff", expr: "呆呆眼", motion: "Idle" },
  working: { label: "执行中", color: "#39c07a", expr: "开心兴奋", motion: "Idle" },
  waiting: { label: "等待你", color: "#ffb340", expr: "感叹号", motion: null },
  review: { label: "审阅中", color: "#b58cff", expr: "方眼镜", motion: null },
  syncing: { label: "同步中", color: "#4fc3d9", expr: "流汗", motion: null },
  blocked: { label: "被挡住", color: "#ff6b6b", expr: "生气", motion: null },
  error: { label: "出错了", color: "#ff4d4d", expr: "哭", motion: null },
  done: { label: "完成了", color: "#ffd166", expr: "爱心眼", motion: "Show" },
  interrupted: { label: "被打断", color: "#9b9b9b", expr: "悲伤", motion: null },
};

/* 每个状态有一"池"表情：主体跟着状态走，但会时不时换一个，不让它显得死板 */
/* 表情池只放"脸部表情"，不放道具/贴纸类。
   `晕晕`（被砸晕那张脸）已经彻底从所有池子里移除 —— 拖动放下时不再出现。 */
const MOOD_POOLS = {
  idle: ["呆呆眼", "脸红", "星星眼", "调皮", "吐舌"],
  thinking: ["呆呆眼", "流汗", "心跳"],
  working: ["开心兴奋", "星星眼", "调皮"],
  waiting: ["脸红", "心跳", "开心兴奋"],
  review: ["方眼镜", "圆眼镜"],
  syncing: ["流汗", "心跳"],
  blocked: ["生气", "阴暗"],
  error: ["哭", "悲伤", "流汗"],
  done: ["爱心眼", "星星眼", "开心兴奋"],
  interrupted: ["悲伤", "流汗"],
};

const DRAG_MOODS = ["开心兴奋", "调皮", "脸红", "生气", "吐舌"];
const DROP_MOODS = ["爱心眼", "生气", "星星眼"];
const POKE_MOODS = ["调皮", "吐舌", "脸红", "星星眼", "开心兴奋"];
const SHY_MOODS = ["脸红", "心跳", "爱心眼"];
const PAT_MOODS = ["脸红", "爱心眼", "开心兴奋", "星星眼"];

const MOOD_CHECK_MS = 2500;      // 多久检查一次"该不该换表情"
const DEFAULT_MOOD_SECONDS = 11; // 用户可调
const DEFAULT_FPS_LIMIT = 60;    // 渲染帧率上限（帧/秒，0 = 不限制；设置里可改）
const DEFAULT_GAZE_RANGE = 320;  // 超出这个距离就不跟了（像素，0 = 完全不跟随）
const GAZE_DEAD_ZONE = 0.12;     // 比例死区：中心这一小圈里眼珠不动，防止盯着看时抖
const GAZE_EYE_TAU = 0.28;       // 眼珠跟随的时间常数（秒）：越大越慢、越柔和
const GAZE_HEAD_TAU = 0.70;      // 头部比眼睛慢半拍：先瞟一眼再转头，不像整颗头被拽着走
const GAZE_BODY_TAU = 1.10;      // 身体再慢一点，转身的层次感就出来了
const GAZE_RETURN_DELAY = 0.16;  // 鼠标离开跟随范围后先顿一下，再开始回正（秒）
const GAZE_RETURN_SECONDS = 0.55;// 回正动画的基准时长（秒），偏得越多越慢一点点
const GAZE_MAX_STEP = 0.05;      // 掉帧时最多按 50ms 计算，避免一步跳过去
const GAZE_SCALE_EYE = 1.0;      // 眼珠参数幅度
const GAZE_SCALE_HEAD = 30.0;    // 头部转动幅度（度）

const bridge = (() => {
  const wv = window.chrome && window.chrome.webview;
  if (wv) {
    return {
      send: (msg) => wv.postMessage(msg),
      onMessage: (fn) => wv.addEventListener("message", (e) => fn(e.data)),
      live: true,
    };
  }
  return {
    send: (msg) => {
      (window.__petSent = window.__petSent || []).push(msg);
      if (window.__petLog) console.log("to-host", msg);
    },
    onMessage: (fn) => {
      window.__petReceive = fn;
    },
    live: false,
  };
})();

const el = (id) => document.getElementById(id);

window.addEventListener("error", (ev) =>
  bridge.send({ t: "error", message: `${ev.message} @ ${ev.filename}:${ev.lineno}:${ev.colno}` })
);
window.addEventListener("unhandledrejection", (ev) =>
  bridge.send({ t: "error", message: "unhandled rejection: " + String(ev.reason && ev.reason.stack ? ev.reason.stack : ev.reason) })
);
for (const level of ["error", "warn"]) {
  const original = console[level].bind(console);
  console[level] = (...args) => {
    bridge.send({ t: "error", message: `${level}: ` + args.map(String).join(" ") });
    original(...args);
  };
}

const ui = {
  chip: el("chip"),
  chipText: el("chip-text"),
  chipDot: el("chip-dot"),
  toast: el("toast"),
  card: el("card"),
  cardDot: el("card-dot"),
  cardState: el("card-state"),
  cardTitle: el("card-title"),
  cardElapsed: el("card-elapsed"),
  cardTools: el("card-tools"),
  cardTokens: el("card-tokens"),
  cardSource: el("card-source"),
  cardActivity: el("card-activity"),
  cardActivityText: el("card-activity-text"),
  cardSay: el("card-say"),
  cardUsage: el("card-usage"),
};

const state = {
  key: "idle",
  payload: null,
  cardOpen: false,
  expression: null,
  elapsed: 0,
  settings: null,
  tempExpr: null,
  tempUntil: 0,
  moodAt: 0,
  hoverSince: 0,
  lastShy: 0,
  dragged: false,
  cursor: null,
  overChar: null,
  gaze: {
    x: 0, y: 0, targetX: 0, targetY: 0, ratioX: 0, ratioY: 0,
    headX: 0, headY: 0, bodyX: 0,
    phase: "rest",               // rest（不动）/ follow（跟着鼠标）/ return（回正动画中）
    returnAt: 0, returnDur: 0,
    fromEyeX: 0, fromEyeY: 0, fromHeadX: 0, fromHeadY: 0, fromBodyX: 0,
    clamped: false, distance: 0,
  },
  gazeAt: 0,
  fpsLimit: 60,         // 当前生效的渲染帧率上限（0 = 不限制），由设置决定
  fpsValue: 0,          // 最近一秒统计到的真实帧数
  fpsTicks: 0,
  fpsWindowStart: 0,
  expressionHistory: [],
  muteBackup: null,
  muteTimer: 0,
};

let app = null;
let model = null;
let idleTimer = 0;
let moodTimer = 0;
let expressionCursor = 0;

function exprFor(key) {
  const custom = state.settings && state.settings.expressions;
  if (custom && custom[key] && expressionAllowed(custom[key])) return custom[key];
  const style = STATE_STYLE[key];
  const fallback = style ? style.expr : null;
  return fallback && expressionAllowed(fallback) ? fallback : null;
}

/* 表情白名单：被用户关掉的表情一律不出现（状态映射、随机心情、点击反应都走这里） */
function expressionAllowed(name) {
  if (!name) return false;
  const list = state.settings && Array.isArray(state.settings.expressionAllowed) ? state.settings.expressionAllowed : ["*"];
  return list.includes("*") || list.includes(name);
}

function allowedPool(key) {
  const pool = MOOD_POOLS[key] || MOOD_POOLS.idle;
  return pool.filter(expressionAllowed);
}

/** 从一组表情里随机挑一个 —— 只会挑"白名单里允许"的；全被关掉就返回 null（保持当前表情） */
function pickAllowed(list) {
  const source = (list || []).filter(expressionAllowed);
  if (!source.length) return null;
  return source[Math.floor(Math.random() * source.length)];
}

/** 渲染帧率上限：设置里可调到 30–240，或"不限制"。

    注意必须设在**共享 ticker** 上 —— 模型更新、视线写入、渲染都排在 PIXI.Ticker.shared，
    以前只设了 app.ticker.maxFPS，而页面用的是共享 ticker（sharedTicker: true），
    所以那个上限根本没起作用（日志里能跑到 300+ 帧）。这里两边都设，谁在跑都拦得住。
    0 = 不限制（跑满屏幕刷新率）。 */
function applyFpsLimit() {
  const cfg = state.settings || {};
  const hasValue = cfg.fpsLimit !== undefined && cfg.fpsLimit !== null && cfg.fpsLimit !== "";
  const raw = hasValue ? Number(cfg.fpsLimit) : NaN;   // 设置还没送到时别把 null 当成"不限"
  let limit = DEFAULT_FPS_LIMIT;
  if (Number.isFinite(raw)) limit = raw <= 0 ? 0 : Math.min(360, Math.round(raw));
  const shared = PIXI.Ticker.shared;
  shared.maxFPS = limit;
  if (app && app.ticker && app.ticker !== shared) app.ticker.maxFPS = limit;
  state.fpsLimit = limit;
  return limit;
}

/** 数帧：每秒统计一次真实渲染帧数（比 ticker 自带的平滑 FPS 准，
    设置页显示的"当前 fps"和上报给宿主的都是这个值）。 */
function countFrame() {
  const now = performance.now();
  if (!state.fpsWindowStart) {
    state.fpsWindowStart = now;
    state.fpsTicks = 0;
    return;
  }
  state.fpsTicks += 1;
  const span = now - state.fpsWindowStart;
  if (span >= 1000) {
    state.fpsValue = Math.round((state.fpsTicks * 1000) / span);
    state.fpsTicks = 0;
    state.fpsWindowStart = now;
  }
}

function applySettings(cfg) {
  state.settings = cfg || {};
  applyFpsLimit();
  refreshPlaybackPolicy();
  const style = (cfg && cfg.cardStyle) === "solid" ? "style-solid" : "style-glass";
  if (ui.card) {
    ui.card.classList.remove("style-glass", "style-solid");
    ui.card.classList.add(style);
    applyCardScale();
    layout();
  }
  if (cfg && typeof cfg.showChip === "boolean") {
    if (!cfg.showChip) ui.chip.classList.add("hidden");
    else if (state.payload) ui.chip.classList.toggle("hidden", !(state.payload.title && state.key !== "idle"));
  }
  if (state.key) {
    state.expression = null;
    const expression = exprFor(state.key);
    if (expression) setExpression(expression);
    else resetExpression();
  }
}

function resetExpression() {
  try {
    const mgr = model && model.internalModel && model.internalModel.motionManager;
    if (mgr && mgr.expressionManager && typeof mgr.expressionManager.resetExpression === "function") {
      mgr.expressionManager.resetExpression();
    }
  } catch (err) {
    /* older builds do not expose resetExpression */
  }
}

/** 设置变化时立即收敛播放状态：关闭自动动作时要停掉正在循环的旧动作。 */
function refreshPlaybackPolicy() {
  const cfg = motionConfig();
  if (!cfg.autoMotion || !allowedIndices("Idle").length) stopAllMotions();
  else scheduleIdle();
}

function stopAllMotions(reason = "settings") {
  if (!model) return;
  const mgr = model.internalModel && model.internalModel.motionManager;
  if (!mgr) return;
  const queued = Array.isArray(mgr._motions) ? mgr._motions.length : null;
  try {
    if (typeof mgr.stopAllMotions === "function") mgr.stopAllMotions();
    else if (typeof mgr.stopAllMotion === "function") mgr.stopAllMotion();
    if (Array.isArray(mgr._motions)) mgr._motions.length = 0;
    state.lastMotionAt = Date.now();
    if (queued) {
      bridge.send({ t: "playback-debug", data: { reason, queued, autoMotion: motionConfig().autoMotion } });
    }
  } catch (err) {
    console.warn("stop motions failed", err);
  }
}

/** 硬闸门：渲染库自己触发的动作也会被拦住，不只拦我们的 playMotion()。 */
function installPlaybackGuards() {
  if (!model || state.playbackGuardInstalled) return;
  const mgr = model.internalModel && model.internalModel.motionManager;
  if (!mgr) return;
  const originalModelMotion = model.motion.bind(model);
  model.motion = (...args) => (motionConfig().autoMotion ? originalModelMotion(...args) : false);
  for (const name of ["startMotion", "startMotionPriority"]) {
    const original = mgr[name];
    if (typeof original !== "function") continue;
    mgr[name] = (...args) => (motionConfig().autoMotion ? original.apply(mgr, args) : false);
  }
  if (typeof mgr.updateMotion === "function") {
    const originalUpdate = mgr.updateMotion.bind(mgr);
    mgr.updateMotion = (dt, seconds) => {
      if (!motionConfig().autoMotion) {
        if (Array.isArray(mgr._motions) && mgr._motions.length) mgr.stopAllMotions();
        return false;
      }
      return originalUpdate(dt, seconds);
    };
  }
  state.playbackGuardInstalled = true;
}

/* ------------------------------------------------------------------ model */

async function boot() {
  app = new PIXI.Application({
    backgroundAlpha: 0,
    width: window.innerWidth,
    height: window.innerHeight,
    antialias: true,
    autoDensity: true,
    resolution: window.devicePixelRatio || 1,
    sharedTicker: true, // 模型更新、视线写入、渲染排在同一个 ticker 上，顺序才可控
  });
  app.view.id = "pet-canvas";
  document.body.insertBefore(app.view, document.body.firstChild);
  applyFpsLimit();          // 帧率上限由设置决定（默认 60 帧；见下面的 applyFpsLimit）

  model = await PIXI.live2d.Live2DModel.from("model/settings.json", { autoInteract: false });
  app.stage.addChild(model);
  installPlaybackGuards();
  installParamBlockPatch();
  layout();
  window.addEventListener("resize", () => {
    app.renderer.resize(window.innerWidth, window.innerHeight);
    updateCardDensity();
    applyCardScale();
    if (state.cardOpen && (window.innerHeight < 180 || window.innerWidth < 160)) setCardOpen(false);
    layout();
  });

  // 设置稍后才送达；配置生效前不要启动动作，避免旧的循环动作先播起来。
  setInterval(reportHitbox, 220);
  setInterval(scheduleIdle, 2000);
  setInterval(moodTick, MOOD_CHECK_MS);
  setInterval(reportFps, 3000);
  // 优先级：模型自身更新是 0，渲染是 LOW(25)。
  // 视线/道具屏蔽写在 10、11 —— 都在"模型更新之后、渲染之前"，这样才不会被动作覆盖。
  // 注意：Live2D 模型是挂在**共享 ticker**上更新的，所以这两件事也必须挂到共享 ticker 上，
  // 否则顺序不确定（表现就是"明明屏蔽了，动画还是把它写回来了"）。
  const shared = PIXI.Ticker.shared;
  shared.add(updateGaze, null, 10);
  shared.add(applyBlockedParams, null, 11);
  shared.add(countFrame, null, 99);        // 数帧：给设置页显示"当前 fps"
  if (app.ticker && app.ticker !== shared) {
    app.ticker.add(updateGaze, null, 10);
    app.ticker.add(applyBlockedParams, null, 11);
    app.ticker.add(countFrame, null, 99);
  }
  wireInteractions();
  reportHitbox();
  bridge.send({ t: "ready", expressions: expressionNames() });
}

/** 被屏蔽的道具参数每帧强制归零。

    像"锤子出现 / 锤子旋转 / 锤子 X"这些是藏在动画曲线里的道具，
    改表情白名单、关动作都拦不住（它们就在 idle 动画里），只有直接把参数按住才行。 */
function applyBlockedParams() {
  const blocked = blockedParamIds();
  if (!blocked.length) return;
  const core = model && model.internalModel && model.internalModel.coreModel;
  if (!core || typeof core.setParameterValueById !== "function") return;
  for (const id of blocked) {
    try {
      core.setParameterValueById(id, 0);
    } catch (err) {
      /* 没这个参数就跳过 */
    }
  }
}

/** 把设置里的 blockedParams 解析成具体参数 id 列表。

    ["*"] 表示"屏蔽全部道具"—— 用宿主下发的道具清单展开。
    注：这条清单包含猫手(maoshou)、锤子、星星、包包、手机、键盘等**所有**
    被动作驱动的道具参数，因为这些东西全都藏 idle/其他动作的曲线里。 */
let blockedIdsCache = null;
let blockedIdsSource = null;

function blockedParamIds() {
  const settings = state.settings || {};
  const list = Array.isArray(settings.blockedParams) ? settings.blockedParams : [];
  const props = Array.isArray(settings.propParams) ? settings.propParams : [];
  const key = JSON.stringify([list, props.length]);
  if (blockedIdsCache && blockedIdsSource === key) return blockedIdsCache;
  let ids;
  if (list.includes("*")) {
    ids = props.map((p) => p.id).filter(Boolean);
  } else {
    ids = list.slice();
  }
  blockedIdsCache = ids;
  blockedIdsSource = key;
  return ids;
}

/** 更稳的一层保险：挂在模型自己的 update 后面。

    模型的参数是在 internalModel.update() 里算出来的（动作/物理都在里面），
    所以在这之后立刻按住被屏蔽的参数，就能保证"算完就是 0、画出来也是 0"，
    不依赖 ticker 优先级顺序。 */
function installParamBlockPatch() {
  const im = model && model.internalModel;
  if (!im || im.__petBlockPatched) return;
  const original = im.update.bind(im);
  im.update = (dt, now) => {
    original(dt, now);
    applyBlockedParams();
  };
  im.__petBlockPatched = true;
}

function expressionNames() {
  const mgr = model && model.internalModel && model.internalModel.motionManager;
  const defs = mgr && mgr.expressionManager && mgr.expressionManager.definitions;
  const fromModel = defs ? defs.map((d) => d.name || d.Name).filter(Boolean) : [];
  if (fromModel.length) return fromModel;
  return (state.settings && state.settings.availableExpressions) || [];
}

function layout() {
  if (!model) return;
  const w = window.innerWidth;
  const h = window.innerHeight;
  const maxW = w * 0.96;
  const gap = cardGap();                       // 卡片与角色头顶的间距（CSS 像素，固定值）
  const cardOpen = state.cardOpen && ui.card && !ui.card.classList.contains("hidden");
  const cardH = cardOpen ? ui.card.getBoundingClientRect().height : 0;
  const maxH = Math.max(80, h - cardH - gap - 16);
  const scale = Math.min(maxW / model.internalModel.width, maxH / model.internalModel.height);
  model.scale.set(scale);
  model.x = (w - model.width) / 2;
  model.y = h - model.height;
  // 卡片贴着"角色头顶"放：桌宠放大缩小时，这段距离保持不变（以前贴窗口顶部，
  // 一放大中间就空出一大段，看着像飘走了）
  if (cardOpen) {
    const top = Math.max(4, model.y - cardH - gap);
    ui.card.style.top = `${Math.round(top)}px`;
  }
  reportHitbox();
}

function cardGap() {
  const value = Number(state.settings && state.settings.cardGap != null ? state.settings.cardGap : 12);
  return Number.isFinite(value) ? Math.max(-20, Math.min(60, value)) : 12;
}

/** 桌宠放大时卡片跟着放大一点（字号/内边距），不然大桌宠配小卡片会很奇怪 */
function applyCardScale() {
  const base = (state.settings && state.settings.baseSize) || [400, 620];
  const petScale = base[0] ? window.innerWidth / base[0] : 1;
  const linked = !state.settings || state.settings.cardScaleWithPet !== false;
  const value = linked ? Math.min(1.45, Math.max(0.85, 1 + (petScale - 1) * 0.6)) : 1;
  if (ui.card) ui.card.style.setProperty("--card-scale", value.toFixed(3));
}

function characterRect() {
  if (!model) return [0, 0, 0, 0];
  const b = model.getBounds();
  return [Math.round(b.x), Math.round(b.y), Math.round(b.width), Math.round(b.height)];
}

function reportHitbox() {
  bridge.send({
    t: "size",
    rect: characterRect(),
    headRect: headRect(),
    dpr: window.devicePixelRatio || 1,
  });
}

function headRect() {
  const rect = characterRect();
  if (!rect[2]) return null;
  return [
    Math.round(rect[0] + rect[2] * 0.18),
    Math.round(rect[1] + rect[3] * 0.02),
    Math.round(rect[2] * 0.64),
    Math.round(rect[3] * 0.36),
  ];
}

function reportFps() {
  const fps = state.fpsValue || (app && app.ticker ? app.ticker.FPS : 0);
  bridge.send({ t: "fps", value: Math.round(fps) });
}

function canvasElement() {
  return (app && (app.view || app.canvas)) || null;
}

/* ---------------------------------------------------------------- motions */

function motionConfig() {
  const s = state.settings || {};
  const allowed = Array.isArray(s.motionAllowed) ? s.motionAllowed : ["*"];
  return {
    allowed: allowed.includes("*") ? null : new Set(allowed),
    gapMs: Math.max(0, Number(s.motionGapSeconds != null ? s.motionGapSeconds : 4)) * 1000,
    idleMs: Math.max(0, Number(s.idleMotionSeconds != null ? s.idleMotionSeconds : 6)) * 1000,
    motionChance: Number(s.motionChance != null ? s.motionChance : 30),
    moodChance: Number(s.moodChance != null ? s.moodChance : 80),
    autoMotion: s.autoMotion !== false,
    autoExpression: s.autoExpression !== false,
  };
}

function allowedIndices(group) {
  const cfg = motionConfig();
  const mgr = model && model.internalModel && model.internalModel.motionManager;
  const defs = (mgr && mgr.definitions && mgr.definitions[group]) || [];
  const out = [];
  for (let i = 0; i < defs.length; i += 1) {
    if (!cfg.allowed || cfg.allowed.has(`${group}:${i}`)) out.push(i);
  }
  return out;
}

function playMotion(group, index) {
  if (!model) return false;
  const cfg = motionConfig();
  if (!cfg.autoMotion) return false;
  const candidates = allowedIndices(group);
  const wanted = typeof index === "number" ? candidates.filter((i) => i === index) : candidates;
  if (!wanted.length) return false;                       // 这个组的动作被关掉了
  const now = Date.now();
  if (now - (state.lastMotionAt || 0) < cfg.gapMs) return false;  // 还没到最小间隔
  const chosen = wanted[Math.floor(Math.random() * wanted.length)];
  try {
    model.motion(group, chosen);
    state.lastMotion = `${group}:${chosen}`;
    state.lastMotionAt = now;
    return true;
  } catch (err) {
    console.warn("motion failed", group, chosen, err);
    return false;
  }
}

function scheduleIdle() {
  if (!model) return;
  const mgr = model.internalModel.motionManager;
  const cfg = motionConfig();
  if (!cfg.autoMotion) {
    stopAllMotions("quiet-poll");
    return;
  }
  const busy = mgr && typeof mgr.isFinished === "function" ? !mgr.isFinished() : false;
  if (busy) return;
  if (cfg.idleMs <= 0) return;                          // 用户关掉了闲置动作
  const now = Date.now();
  if (now - (state.lastIdleAt || 0) < cfg.idleMs) return;
  state.lastIdleAt = now;
  playMotion("Idle");
}

/* ---------------------------------------------------------------- 心情系统 */

function setTempExpression(name, seconds) {
  if (!name) return false;
  state.tempExpr = name;
  state.tempUntil = Date.now() + seconds * 1000;
  state.expression = null;
  setExpression(name);
  return true;
}

function moodTick() {
  if (!model) return;
  if (state.settings && state.settings.autoExpression === false) return;
  if (Date.now() < state.tempUntil) return;      // 正在演临时情绪（拖动/被戳/害羞）
  if (Date.now() - state.moodAt < moodIntervalMs()) return;
  const cfg = motionConfig();
  if (Math.random() * 100 > cfg.moodChance) return;   // 概率控制（设置里可调）

  const pool = MOOD_POOLS[state.key] || MOOD_POOLS.idle;
  const preferred = state.settings && state.settings.expressions ? state.settings.expressions[state.key] : null;
  const name = preferred && Math.random() < 0.5 ? preferred : pickAllowed(pool);
  setExpression(name);
  state.moodAt = Date.now();

  if (Math.random() * 100 < cfg.motionChance) {
    playMotion(state.key === "idle" ? "Idle" : "Tap");
  }
}

function moodIntervalMs() {
  const seconds = Number((state.settings && state.settings.moodSeconds) || DEFAULT_MOOD_SECONDS);
  return Math.max(2, Math.min(120, seconds)) * 1000;
}

function gazeRange() {
  const range = Number((state.settings && state.settings.gazeRange) ?? DEFAULT_GAZE_RANGE);
  return Number.isFinite(range) && range > 0 ? range : 0;  // 0 = 不跟随
}

function onCursor(data) {
  if (!data) return;
  state.cursor = data;
  const now = Date.now();
  if (data.inside) {
    if (!state.hoverSince) state.hoverSince = now;
    // 被盯着看了一会儿：偶尔害羞一下
    const autoExpression = !state.settings || state.settings.autoExpression !== false;
    if (autoExpression && now - state.hoverSince > 2600 && now - state.lastShy > 22000 && Date.now() > state.tempUntil) {
      state.lastShy = now;
      setTempExpression(pickAllowed(SHY_MOODS), 3.2);
      if (Math.random() < 0.5) playMotion("Tap");
    }
  } else {
    state.hoverSince = 0;
  }
}

/* 视线：按"相对窗口中心的偏移比例"映射，而不是固定像素偏移
   ratioX = dx / (窗口宽/2)，ratioY = dy / (窗口高/2)，都夹在 [-1, 1]。
   窗口一变，分母（innerWidth/innerHeight）下一帧就用新的，所以任何大小都对得上。

   手感（这一版的重点）：
   1) 不再用"每帧靠近 10%"——那种写法帧率一高眼珠就变快，掉帧又会顿一下。
      现在按时间常数平滑，快慢只看秒数；
   2) 眼睛先动、头慢半拍、身体再慢一点，三层错开，转头看着才不像整颗头被拽着走；
   3) 鼠标离开跟随范围（或跟随关掉）之后不再硬拽回中位：先顿一下，
      再走一段缓入缓出的回正动画，收尾是慢下来的，不会"啪"地弹回去。 */
function updateGaze() {
  if (!model) return;
  const g = state.gaze;
  const now = performance.now();
  const dt = clamp(((now - state.gazeAt) || 0) / 1000, 0, GAZE_MAX_STEP);
  state.gazeAt = now;

  const w = window.innerWidth || 1;
  const h = window.innerHeight || 1;
  const range = gazeRange();
  const cursor = state.cursor;
  let targetX = 0;
  let targetY = 0;
  let distance = 0;
  let inRange = false;

  if (cursor && typeof cursor.x === "number") {
    const dx = cursor.x - w / 2;
    const dy = cursor.y - h / 2;
    distance = Math.hypot(dx, dy);
    if (range > 0 && distance <= range) {
      inRange = true;
      targetX = softDeadZone(clamp(dx / (w / 2), -1, 1), GAZE_DEAD_ZONE);
      targetY = softDeadZone(clamp(dy / (h / 2), -1, 1), GAZE_DEAD_ZONE);
    }
  }

  if (inRange) {
    g.phase = "follow";
    g.targetX = targetX;
    g.targetY = targetY;
    const kEye = easeFactor(dt, GAZE_EYE_TAU);
    const kHead = easeFactor(dt, GAZE_HEAD_TAU);
    const kBody = easeFactor(dt, GAZE_BODY_TAU);
    g.ratioX += (targetX - g.ratioX) * kEye;
    g.ratioY += (targetY - g.ratioY) * kEye;
    g.headX += (targetX - g.headX) * kHead;
    g.headY += (targetY - g.headY) * kHead;
    g.bodyX += (targetX - g.bodyX) * kBody;
  } else if (g.phase === "follow") {
    startGazeReturn(now);      // 这一步刚离开跟随范围：起一段回正动画
  } else if (g.phase === "return") {
    const t = g.returnDur > 0 ? (now - g.returnAt) / (g.returnDur * 1000) : 1;
    if (t >= 1) {
      restGaze(g);
    } else {
      const k = easeInOutCubic(clamp(t, 0, 1));   // t < 0 = 还在"先顿一下"的等待里
      g.ratioX = g.fromEyeX * (1 - k);
      g.ratioY = g.fromEyeY * (1 - k);
      g.headX = g.fromHeadX * (1 - k);
      g.headY = g.fromHeadY * (1 - k);
      g.bodyX = g.fromBodyX * (1 - k);
    }
  }

  g.x = g.ratioX;
  g.y = g.ratioY;
  g.distance = Math.round(distance);
  g.clamped = Boolean(cursor && typeof cursor.x === "number" && !inRange);
  applyGazeParameters(g);
}

/** 离开跟随范围的那一刻：记下当前角度，过一小会儿再做回正动画（"视线停留"的感觉）。 */
function startGazeReturn(now) {
  const g = state.gaze;
  const offset = clamp(Math.max(Math.abs(g.ratioX), Math.abs(g.ratioY), Math.abs(g.headX), Math.abs(g.headY)), 0, 1);
  g.phase = "return";
  g.fromEyeX = g.ratioX;
  g.fromEyeY = g.ratioY;
  g.fromHeadX = g.headX;
  g.fromHeadY = g.headY;
  g.fromBodyX = g.bodyX;
  g.returnAt = now + GAZE_RETURN_DELAY * 1000;                  // 等待期里 t 是负的，画面保持不动
  g.returnDur = GAZE_RETURN_SECONDS * (0.55 + 0.45 * offset);   // 偏得越多，回正稍慢一点
}

function restGaze(g) {
  g.phase = "rest";
  g.ratioX = 0;
  g.ratioY = 0;
  g.headX = 0;
  g.headY = 0;
  g.bodyX = 0;
  g.targetX = 0;
  g.targetY = 0;
}

/** 指数平滑系数：按"经过了多少秒"算，所以 60Hz 和 144Hz 的手感一样 */
function easeFactor(dt, tau) {
  return 1 - Math.exp(-Math.max(0, dt) / Math.max(0.02, tau));
}

/** 缓入缓出：起步慢、中间快、收尾慢，回正才像"自己转回来"而不是被拽回去 */
function easeInOutCubic(t) {
  return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
}

/** 软死区：中心附近直接压到 0，跨过边界时是连续的（硬阈值会在边界上"啪"地跳一下） */
function softDeadZone(value, zone) {
  const a = Math.abs(value);
  if (a <= zone) return 0;
  return Math.sign(value) * ((a - zone) / (1 - zone));
}

function clamp(value, low, high) {
  return Math.min(high, Math.max(low, value));
}

function applyGazeParameters(g) {
  const core = model && model.internalModel && model.internalModel.coreModel;
  if (!core) {
    window.__gazeDebug = { error: "no coreModel" };
    return;
  }
  if (typeof core.setParameterValueById !== "function") {
    window.__gazeDebug = { error: "no setParameterValueById", keys: Object.getOwnPropertyNames(Object.getPrototypeOf(core)).slice(0, 20) };
    return;
  }
  setParam(core, "ParamEyeBallX", g.x * GAZE_SCALE_EYE);
  setParam(core, "ParamEyeBallY", -g.y * GAZE_SCALE_EYE);
  setParam(core, "ParamAngleX", g.headX * GAZE_SCALE_HEAD);
  setParam(core, "ParamAngleY", -g.headY * GAZE_SCALE_HEAD);
  setParam(core, "ParamAngleZ", g.headX * GAZE_SCALE_HEAD * 0.25);
  setParam(core, "ParamBodyAngleX", g.bodyX * GAZE_SCALE_HEAD * 0.3);
  window.__gazeDebug = { error: null, phase: g.phase, written: g.x, readBack: readParam(core, "ParamEyeBallX") };
}

function setParam(core, id, value) {
  try {
    core.setParameterValueById(id, value);
  } catch (err) {
    window.__gazeDebug = { error: "set failed: " + id + " " + String(err) };
  }
}

function readParam(core, id) {
  try {
    return Number(core.getParameterValueById(id));
  } catch (err) {
    return null;
  }
}

function setExpression(name, recordHistory = true) {
  if (!model || !name || name === state.expression) return;
  if (!expressionAllowed(name)) return;      // 被关掉的表情直接跳过
  if (recordHistory && state.expression) {
    state.expressionHistory.push(state.expression);
    if (state.expressionHistory.length > 12) state.expressionHistory.shift();
  }
  state.expression = name;
  try {
    const result = model.expression(name);
    if (result && typeof result.catch === "function") result.catch(() => {});
  } catch (err) {
    console.warn("expression failed", name, err);
  }
}

/* ------------------------------------------------------------------ state */

function applyState(payload) {
  state.payload = payload;
  const style = STATE_STYLE[payload.state] || STATE_STYLE.idle;
  const changed = payload.state !== state.key;
  state.key = payload.state;

  ui.chipText.textContent = payload.label || style.label;
  ui.chipDot.style.background = style.color;
  ui.chip.classList.toggle("thinking", payload.state === "thinking" || payload.state === "working");
  const chipAllowed = !state.settings || state.settings.showChip !== false;
  ui.chip.classList.toggle("hidden", !(chipAllowed && payload.title && payload.state !== "idle"));

  // 进度卡片（就在这个页面里，不改窗口尺寸）
  ui.cardDot.style.background = style.color;
  ui.cardState.textContent = payload.label || style.label;
  ui.cardState.style.color = style.color;
  ui.cardTitle.textContent = payload.title || "当前没有进行中的任务";
  ui.cardElapsed.textContent = payload.title ? humanTime(payload.elapsed || 0) : "—";
  ui.cardTools.textContent = payload.tools == null ? "—" : String(payload.tools);
  ui.cardTokens.textContent = payload.tokens || "—";
  ui.cardSource.textContent = payload.source || "—";
  ui.cardActivityText.textContent = payload.activity || "等待 " + (payload.source || "Agent") + " 开始工作…";
  ui.cardSay.textContent = payload.say || "";
  ui.cardUsage.textContent = payload.usage || "";   // 今日新增 / 本会话累积（设置里可切、可关）

  state.elapsed = payload.elapsed || 0;

  const expression = exprFor(payload.state);
  state.tempExpr = null;
  state.tempUntil = 0;
  state.moodAt = Date.now();
  if (expression) setExpression(expression);
  else if (changed) {
    state.expression = null;
    try {
      if (model && model.internalModel.motionManager.expressionManager) {
        model.internalModel.motionManager.expressionManager.resetExpression();
      }
    } catch (err) {
      /* older builds do not expose resetExpression */
    }
  }
  if (changed && style.motion) playMotion(style.motion);
  if (changed) {
    layout();
    toast(`${payload.label || style.label}${payload.title ? " · " + short(payload.title, 18) : ""}`);
  }
}

function tick(payload) {
  if (typeof payload.elapsed === "number") {
    state.elapsed = payload.elapsed;
    if (state.payload && state.payload.title && ui.cardElapsed) {
      ui.cardElapsed.textContent = humanTime(state.elapsed);
    }
  }
}

function humanTime(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  if (sec < 60) return `${sec} 秒`;
  const m = Math.floor(sec / 60);
  if (m < 60) return `${m} 分 ${sec % 60} 秒`;
  return `${Math.floor(m / 60)} 小时 ${m % 60} 分`;
}

function setCardOpen(open) {
  state.cardOpen = Boolean(open);
  const tooSmall = window.innerHeight < 180 || window.innerWidth < 160;
  if (state.cardOpen && tooSmall) {
    state.cardOpen = false;
    toast("桌宠太小了，放大一点再看进度");
  }
  updateCardDensity();
  applyCardScale();
  ui.card.classList.toggle("hidden", !state.cardOpen);
  layout();          // 只重算模型缩放，不动窗口尺寸
  reportHitbox();
}

function updateCardDensity() {
  const small = window.innerWidth < 320 || window.innerHeight < 360;
  ui.card.classList.toggle("compact", small);
}

// 卡片内容变长/变短时也要重算，保证任何时候都不压到角色
if (window.ResizeObserver) {
  const observer = new ResizeObserver(() => layout());
  observer.observe(el("card"));
}

function short(text, n) {
  return text.length > n ? text.slice(0, n) + "…" : text;
}

let toastTimer = 0;
function toast(text) {
  ui.toast.textContent = text;
  ui.toast.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => ui.toast.classList.add("hidden"), 2200);
}

/* ------------------------------------------------------------ interactions */

// 进度卡片已经改成宿主那边的独立窗口，网页不再做任何"变大变小"，
// 桌宠窗口尺寸一旦确定就永远不动（频繁改尺寸是性能杀手）。

function wireInteractions() {
  // 透明窗口是分层的，网页收不到鼠标事件；点击/拖动由宿主程序读取鼠标状态后发消息过来，
  // 所以这里只保留"被戳到了怎么反应"。
  //
  // 顺带把右键默认行为整页拦掉（捕获阶段，先于任何元素）：WebView2 已经关掉了自带
  // 右键菜单，不拦的话没被消费的 WM_CONTEXTMENU 会冒泡给宿主窗口，把窗口自己的
  // 系统菜单（还原/移动/大小/关闭）勾出来。
  window.addEventListener("contextmenu", (ev) => ev.preventDefault(), true);
  initProbe();
}

/* —— 像素级命中探测 ——

   窗口的"点击穿透"是色键（洋红）做的：网页透明像素 = 洋红 = 鼠标点直接穿到桌面。
   所以"点在不在角色身上"也只能按**像素**判断。宿主一直在等网页发 {t:"over"}，
   而网页以前从来没发过，于是只能退回"角色包围盒"判断 —— 包围盒里的透明缝隙
   （发丝之间、手臂和身体之间、两条腿之间）就变成"又穿过去了、又当成点中了角色"：
   右键同时弹两个菜单，拖动顺带点到下面的窗口，都是这么来的。

   采样时机是关键：画布没开 preserveDrawingBuffer，只有**这一帧渲染刚结束**时读到的
   才是有效像素（实测：挂在 PIXI.Ticker.shared 默认优先级上永远读到 0，因为那一刻
   渲染还没发生）。所以这里包住 renderer.render，在它返回之后立刻读。

   开销控制：一次只读 1 个像素、~20Hz、而且**光标不在窗口里就完全不读** ——
   早期版本每 120ms 读一次整块画布，把 GPU 流水线读停顿了，这次不能再犯。 */
const probe = {
  canvas: null,
  ctx: null,
  usable: null,    // null=还没测过 / true=像素读数可信 / false=不可用（宿主退回包围盒判断）
  over: null,      // 上一次报给宿主的值
  sampled: false,  // 真采到过一次样才允许上报，避免链路没起来时把点击全禁掉
  lastAt: 0,
  lastPt: null,    // 上一次采样的画布坐标（用来判断光标动没动）
};

function initProbe() {
  probe.canvas = document.createElement("canvas");
  probe.canvas.width = 1;
  probe.canvas.height = 1;
  probe.ctx = probe.canvas.getContext("2d", { willReadFrequently: true });
  const renderer = app && app.renderer;
  if (!renderer || typeof renderer.render !== "function") return;
  const original = renderer.render.bind(renderer);
  renderer.render = (...args) => {
    original(...args);
    sampleProbe();
  };
}

/** 光标位置折算成画布像素坐标（画布带 resolution/autoDensity，按比例换算）。 */
function probePoint() {
  const canvas = canvasElement();
  const cur = state.cursor;
  if (!canvas || !cur || !cur.inside || typeof cur.x !== "number") return null;
  const rect = canvas.getBoundingClientRect();
  if (!rect.width || !rect.height) return null;
  const x = Math.floor((cur.x - rect.left) * (canvas.width / rect.width));
  const y = Math.floor((cur.y - rect.top) * (canvas.height / rect.height));
  if (x < 0 || y < 0 || x >= canvas.width || y >= canvas.height) return null;
  return { x, y };
}

function sampleProbe() {
  const pt = probePoint();
  const sameSpot = Boolean(pt && probe.lastPt && pt.x === probe.lastPt.x && pt.y === probe.lastPt.y);
  const now = performance.now();
  // 光标一动就跟帧采（把刚移开就按下的滞后压到 1 帧内）；
  // 光标不动时只是隔一会儿复核一下（模型自己会动），没必要每帧去读 GPU。
  if (now - probe.lastAt < (sameSpot ? 500 : 16)) return;
  probe.lastAt = now;
  probe.lastPt = pt;
  if (!pt) {
    reportProbe(false, 0);                  // 光标不在窗口里，那一定不是"点在角色上"
    return;
  }
  try {
    const ctx = probe.ctx;
    ctx.clearRect(0, 0, 1, 1);
    ctx.drawImage(canvasElement(), pt.x, pt.y, 1, 1, 0, 0, 1, 1);
    const alpha = ctx.getImageData(0, 0, 1, 1).data[3];
    probe.sampled = true;
    if (probe.usable !== true) {
      probe.usable = true;
      const canvas = canvasElement();
      const box = canvas ? canvas.getBoundingClientRect() : null;
      // 画布尺寸/CSS 尺寸/dpr 一起带上：宿主据此就能验证"采样点 = 光标点"
      bridge.send({
        t: "over-probe",
        value: true,
        canvas: canvas ? [canvas.width, canvas.height] : null,
        css: box ? [Math.round(box.width), Math.round(box.height)] : null,
        dpr: window.devicePixelRatio || 1,
      });
    }
    // alpha>8 = 这一格真的画了东西 = 屏幕上不是洋红 = 点下去归桌宠
    reportProbe(alpha > 8, alpha);
  } catch (err) {
    if (probe.usable !== false) {
      probe.usable = false;
      bridge.send({ t: "over-probe", value: false });
    }
  }
}

/** 只在结论变化时上报：宿主 on_character() 用的就是这个值。
    x/y/alpha 只是给日志排查用的，宿主判断只看 value。 */
function reportProbe(over, alpha) {
  const value = Boolean(over);
  if (!probe.sampled || value === probe.over) return;
  probe.over = value;
  const pt = probe.lastPt;
  bridge.send({ t: "over", value, x: pt ? pt.x : null, y: pt ? pt.y : null, alpha: alpha });
}

function reaction(kind) {
  if (kind === "pat") {
    setTempExpression(pickAllowed(PAT_MOODS), 3.0);
    playMotion("Tap");
    toast("摸摸头～");
  } else if (kind === "poke") {
    setTempExpression(pickAllowed(POKE_MOODS), 2.6);
    playMotion(Math.random() < 0.5 ? "Tap" : "Show");
  } else if (kind === "drag") {
    state.dragged = true;
    setTempExpression(pickAllowed(DRAG_MOODS), 8);
    playMotion("Tap");
  } else if (kind === "drop") {
    state.dragged = false;
    setTempExpression(pickAllowed(DROP_MOODS), 3.4);
    playMotion(Math.random() < 0.5 ? "Show" : "Tap");
  } else if (kind === "motion") {
    playMotion(Math.random() < 0.5 ? "Tap" : "Show");
    setTempExpression(pickAllowed(MOOD_POOLS[state.key] || MOOD_POOLS.idle), 4);
  }
}

function cycleExpression() {
  const names = expressionNames().filter(expressionAllowed);   // 双击换表情也会跳过被关掉的
  if (!names.length) {
    toast("没有可用的表情（都被白名单关掉了？）");
    return;
  }
  expressionCursor = (expressionCursor + 1) % names.length;
  const name = names[expressionCursor];
  setTempExpression(name, 6);
  toast("表情：" + name);
}

/* ------------------------------------------------------------------- boot */

bridge.onMessage((raw) => {
  let msg = raw;
  if (typeof raw === "string") {
    try {
      msg = JSON.parse(raw);
    } catch (err) {
      return;
    }
  }
  if (!msg || !msg.t) return;
  if (msg.t === "state") applyState(msg.data);
  if (msg.t === "tick") tick(msg.data);
  if (msg.t === "cursor") onCursor(msg.data);
  if (msg.t === "settings") applySettings(msg.data);
  if (msg.t === "card") setCardOpen(msg.open);
  if (msg.t === "react") reaction(msg.kind);
  if (msg.t === "cycle-expression") cycleExpression();
  if (msg.t === "status-request") bridge.send({ t: "status", data: window.__pet.getStatus() });
  if (msg.t === "snapshot-request") {
    try {
      const canvas = canvasElement();
      const url = canvas ? canvas.toDataURL("image/png") : "";
      bridge.send({ t: "snapshot", data: url });
    } catch (err) {
      bridge.send({ t: "snapshot", data: "", error: String(err) });
    }
  }
  if (msg.t === "say") {
    toast(String(msg.text || ""));
  }
  if (msg.t == "button-page-action") {
    if (msg.action === "undo-expression") undoExpression();
    if (msg.action === "clear-card") clearCard();
  }
  if (msg.t == "mute") muteAnimations(Number(msg.seconds) || 300);
});

function undoExpression() {
  const previous = state.expressionHistory.pop();
  if (!previous) {
    toast("没有可撤销的表情");
    return;
  }
  state.expression = null;
  setExpression(previous, false);
  toast("已撤销表情：" + previous);
}

function clearCard() {
  if (state.payload) {
    state.payload = {
      ...state.payload,
      title: "",
      elapsed: 0,
      tools: null,
      tokens: "",
      source: "",
      activity: "任务信息已清空",
      say: "",
    };
  }
  ui.cardTitle.textContent = "当前没有进行中的任务";
  ui.cardElapsed.textContent = "—";
  ui.cardTools.textContent = "—";
  ui.cardTokens.textContent = "—";
  ui.cardSource.textContent = "—";
  ui.cardActivityText.textContent = "任务信息已清空";
  ui.cardUsage.textContent = "";
  ui.cardSay.textContent = "";
  toast("进度卡片已清空");
}

function muteAnimations(seconds) {
  if (!state.settings) return;
  if (!state.muteBackup) {
    state.muteBackup = {
      autoExpression: state.settings.autoExpression !== false,
      autoMotion: state.settings.autoMotion !== false,
    };
  }
  clearTimeout(state.muteTimer);
  state.settings.autoExpression = false;
  state.settings.autoMotion = false;
  refreshPlaybackPolicy();
  state.muteTimer = setTimeout(() => {
    if (state.muteBackup && state.settings) {
      state.settings.autoExpression = state.muteBackup.autoExpression;
      state.settings.autoMotion = state.muteBackup.autoMotion;
    }
    state.muteBackup = null;
    refreshPlaybackPolicy();
    toast("动画静音已结束");
  }, seconds * 1000);
}

setInterval(() => bridge.send({ t: "status", data: window.__pet.getStatus() }), 5000);

window.__pet = {
  applyState,
  onCursor,
  moodTick,
  reaction,
  cycleExpression,
  setCardOpen,
  applySettings,
  setTempExpression,
  state: () => state,
  gaze: () => {
    const fc = model && model.internalModel && model.internalModel.focusController;
    return fc ? { x: fc.x, y: fc.y } : null;
  },
  gazeTarget: () => state.lastGaze || null,
  gazeState: () => ({ ...state.gaze }),
  expressions: () => expressionNames(),
  expressionCursor: () => expressionCursor,
  model: () => model,
  app: () => app,
  getStatus: () => ({
    state: state.key,
    label: STATE_STYLE[state.key] ? STATE_STYLE[state.key].label : state.key,
    expression: state.expression,
    lastMotion: state.lastMotion || null,
    autoMotion: !state.settings || state.settings.autoMotion !== false,
    autoExpression: !state.settings || state.settings.autoExpression !== false,
    playbackGuardInstalled: Boolean(state.playbackGuardInstalled),
    queuedMotions: model && model.internalModel && model.internalModel.motionManager && Array.isArray(model.internalModel.motionManager._motions)
      ? model.internalModel.motionManager._motions.length
      : null,
    rect: characterRect(),
    modelLoaded: !!model,
    modelSize: model ? [model.width, model.height] : null,
    expressions: expressionNames(),
    chip: ui.chipText.textContent,
  }),
  setExpression,
  playMotion,
};

window.__petReceive = window.__petReceive || null;

boot().catch((err) => {
  console.error("pet boot failed", err);
  bridge.send({ t: "error", message: "boot failed: " + String(err && err.stack ? err.stack : err) });
  document.body.innerHTML = `<div style="color:#fff;font:14px sans-serif;padding:12px">桌宠加载失败：${err}</div>`;
});
