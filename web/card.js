/* 任务进度卡片（独立窗口）：显示当前 Agent 在做什么 */

const $ = (id) => document.getElementById(id);

const bridge = (() => {
  const wv = window.chrome && window.chrome.webview;
  if (wv) return { send: (m) => wv.postMessage(m), on: (fn) => wv.addEventListener("message", (e) => fn(e.data)) };
  return {
    send: (m) => {
      (window.__sent = window.__sent || []).push(m);
    },
    on: (fn) => {
      window.__receive = fn;
    },
  };
})();

const STATE_COLORS = {
  idle: "#7f8c9b",
  thinking: "#7aa2ff",
  working: "#39c07a",
  waiting: "#ffb340",
  review: "#b58cff",
  syncing: "#4fc3d9",
  blocked: "#ff6b6b",
  error: "#ff4d4d",
  done: "#ffd166",
  interrupted: "#9b9b9b",
};

function humanTime(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  if (sec < 60) return `${sec} 秒`;
  const m = Math.floor(sec / 60);
  if (m < 60) return `${m} 分 ${sec % 60} 秒`;
  return `${Math.floor(m / 60)} 小时 ${m % 60} 分`;
}

let current = null;

function render(payload) {
  current = payload || {};
  const color = STATE_COLORS[current.state] || STATE_COLORS.idle;
  $("dot").style.background = color;
  $("state").textContent = current.label || "空闲";
  $("state").style.color = color;
  $("title").textContent = current.title || "当前没有进行中的任务";
  $("elapsed").textContent = current.title ? humanTime(current.elapsed) : "—";
  $("tools").textContent = current.tools == null ? "—" : String(current.tools);
  $("tokens").textContent = current.tokens || "—";
  $("source").textContent = current.source || "—";
  $("activity").textContent = current.activity || "等待 " + (current.source || "Agent") + " 开始工作…";
  $("say").textContent = current.say || "";
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
  if (!msg) return;
  if (msg.t === "state") render(msg.data);
  if (msg.t === "compact") document.body.classList.toggle("compact", Boolean(msg.value));
});

document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") bridge.send({ t: "close-card" });
});

bridge.send({ t: "hello" });
