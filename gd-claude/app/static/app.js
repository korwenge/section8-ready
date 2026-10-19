"use strict";

const TOKEN = new URLSearchParams(location.search).get("t") || "";
const $ = (id) => document.getElementById(id);
const UNIT = 30; // GD units per block
const SNAP = 7.5;

const TOOL_LABELS = {
  level_overview: "reading the level", get_objects: "inspecting objects", look_at_area: "looking at it",
  search_objects: "searching objects", propose_changes: "placing objects", draw_pixel_art: "drawing pixel art",
  convert_image: "converting image", clone_area: "copying deco", clear_proposal: "starting over",
  read_note: "reading notes", save_note: "saving to vault",
};

const state = {
  level: null,
  view: null,          // {shapes, bounds, bg, ground, queued_shapes}
  proposal: { shapes: [], removed: new Set(), adds: 0, removes: 0, channels: {} },
  selection: null,     // [x0, y0, x1, y1] in GD units
  mode: "Decorate",
  image: null,         // data URL
  job: null,
  tool: "select",
  cam: { x: 300, y: 150, scale: 1 },
  status: null,
  saveMtime: null,
};

// --- helpers ------------------------------------------------------------------

async function api(path, body) {
  const opts = { headers: { "X-Token": TOKEN } };
  if (body !== undefined) {
    opts.method = "POST";
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function toast(text, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = text;
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), kind === "err" ? 7000 : 4500);
}

function store(key, value) {
  try { localStorage.setItem(key, value); } catch (_) { /* storage may be unavailable */ }
}
function recall(key) {
  try { return localStorage.getItem(key); } catch (_) { return null; }
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function markdown(text) {
  const lines = escapeHtml(text).split("\n");
  let html = "", list = null, para = [];
  const inline = (s) => s.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/(^|\W)\*(\S[^*]*?)\*(?=\W|$)/g, "$1<i>$2</i>");
  const flushPara = () => { if (para.length) { html += `<p>${inline(para.join(" "))}</p>`; para = []; } };
  const flushList = () => { if (list) { html += `</${list}>`; list = null; } };
  for (const line of lines) {
    const bullet = line.match(/^\s*[-*•]\s+(.*)/);
    const numbered = line.match(/^\s*\d+[.)]\s+(.*)/);
    if (bullet || numbered) {
      flushPara();
      const tag = bullet ? "ul" : "ol";
      if (list !== tag) { flushList(); html += `<${tag}>`; list = tag; }
      html += `<li>${inline((bullet || numbered)[1])}</li>`;
    } else if (!line.trim()) {
      flushPara(); flushList();
    } else {
      flushList(); para.push(line.replace(/^#+\s*/, ""));
    }
  }
  flushPara(); flushList();
  return html;
}

const fmtBlocks = (u) => `${+(u / UNIT).toFixed(2)}`;

// --- canvas -----------------------------------------------------------------------

const canvas = $("canvas");
const ctx = canvas.getContext("2d");
let needsDraw = true;

function resize() {
  const dpr = window.devicePixelRatio || 1;
  const r = canvas.getBoundingClientRect();
  canvas.width = Math.max(1, Math.round(r.width * dpr));
  canvas.height = Math.max(1, Math.round(r.height * dpr));
  needsDraw = true;
}

const W = () => canvas.width / (window.devicePixelRatio || 1);
const H = () => canvas.height / (window.devicePixelRatio || 1);
const toScreen = (x, y) => [(x - state.cam.x) * state.cam.scale + W() / 2, H() / 2 - (y - state.cam.y) * state.cam.scale];
const toWorld = (sx, sy) => [(sx - W() / 2) / state.cam.scale + state.cam.x, (H() / 2 - sy) / state.cam.scale + state.cam.y];

function drawShape(s, mode) {
  const [x, y, w, h, rot, baseColor, alpha, kind, , channel] = s;
  const recolour = state.proposal.channels && $("showProposal").checked && state.proposal.channels[channel];
  const color = recolour ? recolour[0] : baseColor;
  const sc = state.cam.scale;
  const [sx, sy] = toScreen(x, y);
  const pw = w * sc, ph = h * sc;
  const r = Math.max(pw, ph);
  if (sx + r < 0 || sy + r < 0 || sx - r > W() || sy - r > H()) return;
  ctx.save();
  ctx.translate(sx, sy);
  if (rot) ctx.rotate((rot * Math.PI) / 180);
  ctx.globalAlpha = mode === "removed" ? 0.25 : Math.max(0.12, Math.min(alpha, 1)) * (mode === "queued" ? 0.9 : 1);
  ctx.fillStyle = color;
  if (kind === "hazard") {
    ctx.beginPath();
    ctx.moveTo(-pw / 2, ph / 2); ctx.lineTo(pw / 2, ph / 2); ctx.lineTo(0, -ph / 2); ctx.closePath();
    ctx.fill();
  } else if (pw < 1.2 && ph < 1.2) {
    ctx.fillRect(-0.6, -0.6, 1.2, 1.2);
  } else {
    ctx.fillRect(-pw / 2, -ph / 2, pw, ph);
    if (kind === "solid" && sc > 0.35) {
      ctx.globalAlpha = 0.35; ctx.strokeStyle = "#ffffff"; ctx.lineWidth = 1;
      ctx.strokeRect(-pw / 2 + 0.5, -ph / 2 + 0.5, pw - 1, ph - 1);
    }
  }
  if (mode === "removed") {
    ctx.globalAlpha = 0.9; ctx.strokeStyle = "#ff5d6c"; ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.moveTo(-pw / 2, -ph / 2); ctx.lineTo(pw / 2, ph / 2);
    ctx.moveTo(pw / 2, -ph / 2); ctx.lineTo(-pw / 2, ph / 2); ctx.stroke();
  } else if (mode === "new" && kind !== "pixel" && sc > 0.25) {
    ctx.globalAlpha = 0.8; ctx.strokeStyle = "#b6f23d"; ctx.lineWidth = 1;
    ctx.strokeRect(-pw / 2, -ph / 2, pw, ph);
  }
  ctx.restore();
}

function draw() {
  needsDraw = false;
  const dpr = window.devicePixelRatio || 1;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const v = state.view;
  const recolour = (v && $("showProposal").checked && state.proposal.channels) || {};
  const bgColor = v && (recolour["1000"] ? recolour["1000"][0] : v.bg);
  const groundColor = v && (recolour["1001"] ? recolour["1001"][0] : v.ground);
  ctx.fillStyle = v ? bgColor : "#0a0c11";
  ctx.fillRect(0, 0, W(), H());
  if (!v) return;
  const sc = state.cam.scale;

  // ground and grid
  const [, groundY] = toScreen(0, 0);
  if (groundY < H()) {
    ctx.fillStyle = groundColor;
    ctx.fillRect(0, Math.max(0, groundY), W(), H() - groundY);
    ctx.fillStyle = "rgba(255,255,255,.55)";
    ctx.fillRect(0, groundY, W(), 1);
  }
  if (sc * UNIT >= 10) {
    const [wx0, wy1] = toWorld(0, 0);
    const [wx1, wy0] = toWorld(W(), H());
    ctx.strokeStyle = "rgba(255,255,255,.06)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (let gx = Math.floor(wx0 / UNIT) * UNIT; gx <= wx1; gx += UNIT) {
      const [px] = toScreen(gx, 0); ctx.moveTo(Math.round(px) + 0.5, 0); ctx.lineTo(Math.round(px) + 0.5, H());
    }
    for (let gy = Math.floor(wy0 / UNIT) * UNIT; gy <= wy1; gy += UNIT) {
      const [, py] = toScreen(0, gy); ctx.moveTo(0, Math.round(py) + 0.5); ctx.lineTo(W(), Math.round(py) + 0.5);
    }
    ctx.stroke();
  }

  const showTriggers = $("showTriggers").checked;
  const showProposal = $("showProposal").checked;
  const removed = showProposal ? state.proposal.removed : new Set();
  v.shapes.forEach((s, i) => {
    if (s[7] === "trigger" && !showTriggers) return;
    drawShape(s, removed.has(i) ? "removed" : "base");
  });
  for (const s of v.queued_shapes) drawShape(s, "queued");
  if (showProposal) for (const s of state.proposal.shapes) drawShape(s, "new");
  drawSelection();
}

function drawSelection() {
  const sel = state.selection;
  if (!sel) return;
  const [ax, ay] = toScreen(sel[0], sel[3]);
  const [bx, by] = toScreen(sel[2], sel[1]);
  ctx.globalAlpha = 1;
  ctx.fillStyle = "rgba(5,6,10,.45)";
  ctx.fillRect(0, 0, W(), ay);
  ctx.fillRect(0, by, W(), H() - by);
  ctx.fillRect(0, ay, ax, by - ay);
  ctx.fillRect(bx, ay, W() - bx, by - ay);
  ctx.strokeStyle = "#b6f23d";
  ctx.lineWidth = 1.5;
  ctx.setLineDash([6, 4]);
  ctx.strokeRect(ax, ay, bx - ax, by - ay);
  ctx.setLineDash([]);
  ctx.fillStyle = "#b6f23d";
  for (const [hx, hy] of handlePoints()) ctx.fillRect(hx - 4, hy - 4, 8, 8);
  const label = `${fmtBlocks(sel[2] - sel[0])} × ${fmtBlocks(sel[3] - sel[1])} blocks`;
  ctx.font = "12px Segoe UI, system-ui, sans-serif";
  const tw = ctx.measureText(label).width + 12;
  const ly = ay > 26 ? ay - 24 : by + 6;
  ctx.fillStyle = "rgba(15,17,23,.9)";
  ctx.fillRect(ax, ly, tw, 20);
  ctx.fillStyle = "#b6f23d";
  ctx.fillText(label, ax + 6, ly + 14);
}

function handlePoints() {
  const sel = state.selection;
  const [ax, ay] = toScreen(sel[0], sel[3]);
  const [bx, by] = toScreen(sel[2], sel[1]);
  const mx = (ax + bx) / 2, my = (ay + by) / 2;
  return [[ax, ay, "nw"], [mx, ay, "n"], [bx, ay, "ne"], [bx, my, "e"], [bx, by, "se"], [mx, by, "s"], [ax, by, "sw"], [ax, my, "w"]];
}

function loop() {
  if (needsDraw) draw();
  requestAnimationFrame(loop);
}

function fitTo(x0, y0, x1, y1, pad = 0.08, groundAtBottom = false) {
  const w = Math.max(x1 - x0, 60), h = Math.max(y1 - y0, 60);
  state.cam.scale = Math.min((W() * (1 - pad * 2)) / w, (H() * (1 - pad * 2)) / h, 6);
  state.cam.x = (x0 + x1) / 2;
  // For whole levels keep the ground near the bottom instead of wasting half the view on it.
  state.cam.y = groundAtBottom ? y0 + (H() / 2 - H() * pad) / state.cam.scale : (y0 + y1) / 2;
  needsDraw = true;
}

function fitLevel(firstPart = false) {
  if (!state.view) return;
  const [x0, y0, x1, y1] = state.view.bounds;
  const left = Math.min(x0, 0);
  const right = firstPart ? Math.min(Math.max(x1, 600), left + 2400) : Math.max(x1, 600);
  fitTo(left, Math.min(y0, -60), right, Math.max(y1, 300), 0.06, true);
}

// --- interaction --------------------------------------------------------------------

let drag = null;
let spaceDown = false;
const snap = (v, free) => (free ? v : Math.round(v / SNAP) * SNAP);

canvas.addEventListener("contextmenu", (e) => e.preventDefault());

canvas.addEventListener("pointerdown", (e) => {
  if (!state.view) return;
  canvas.setPointerCapture(e.pointerId);
  const panning = e.button === 1 || e.button === 2 || spaceDown || state.tool === "pan";
  if (panning) {
    drag = { type: "pan", sx: e.offsetX, sy: e.offsetY, cx: state.cam.x, cy: state.cam.y };
    $("viewport").classList.add("panning");
    return;
  }
  const [wx, wy] = toWorld(e.offsetX, e.offsetY);
  if (state.selection) {
    const hit = handlePoints().find(([hx, hy]) => Math.abs(hx - e.offsetX) < 8 && Math.abs(hy - e.offsetY) < 8);
    if (hit) { drag = { type: "resize", handle: hit[2] }; return; }
    const [x0, y0, x1, y1] = state.selection;
    if (wx > x0 && wx < x1 && wy > y0 && wy < y1) {
      drag = { type: "move", wx, wy, sel: [...state.selection] };
      return;
    }
  }
  const sx = snap(wx, e.altKey), sy = snap(wy, e.altKey);
  drag = { type: "new", x: sx, y: sy };
  setSelection([sx, sy, sx, sy]);
});

canvas.addEventListener("pointermove", (e) => {
  const [wx, wy] = toWorld(e.offsetX, e.offsetY);
  $("coords").textContent = state.view
    ? `x ${wx.toFixed(0)}  y ${wy.toFixed(0)}   ·   block ${fmtBlocks(wx - 15)}, ${fmtBlocks(wy - 15)}`
    : "";
  if (!drag) {
    if (state.selection && state.tool === "select") {
      const hit = handlePoints().find(([hx, hy]) => Math.abs(hx - e.offsetX) < 8 && Math.abs(hy - e.offsetY) < 8);
      canvas.style.cursor = hit ? `${hit[2]}-resize` : "";
    }
    return;
  }
  if (drag.type === "pan") {
    state.cam.x = drag.cx - (e.offsetX - drag.sx) / state.cam.scale;
    state.cam.y = drag.cy + (e.offsetY - drag.sy) / state.cam.scale;
    needsDraw = true;
  } else if (drag.type === "new") {
    setSelection([drag.x, drag.y, snap(wx, e.altKey), snap(wy, e.altKey)]);
  } else if (drag.type === "move") {
    const dx = snap(wx - drag.wx, e.altKey), dy = snap(wy - drag.wy, e.altKey);
    const s = drag.sel;
    setSelection([s[0] + dx, s[1] + dy, s[2] + dx, s[3] + dy]);
  } else if (drag.type === "resize") {
    const s = [...state.selection];
    const x = snap(wx, e.altKey), y = snap(wy, e.altKey);
    if (drag.handle.includes("w")) s[0] = x;
    if (drag.handle.includes("e")) s[2] = x;
    if (drag.handle.includes("s")) s[1] = y;
    if (drag.handle.includes("n")) s[3] = y;
    setSelection(s, false);
  }
});

canvas.addEventListener("pointerup", () => {
  if (drag && drag.type === "new" && state.selection) {
    const [x0, y0, x1, y1] = state.selection;
    if (Math.abs(x1 - x0) < SNAP || Math.abs(y1 - y0) < SNAP) setSelection(null);
  }
  if (state.selection) setSelection(state.selection); // normalise
  drag = null;
  $("viewport").classList.remove("panning");
});

canvas.addEventListener("wheel", (e) => {
  e.preventDefault();
  const [wx, wy] = toWorld(e.offsetX, e.offsetY);
  const factor = Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0015));
  state.cam.scale = Math.min(20, Math.max(0.01, state.cam.scale * factor));
  const [nx, ny] = toWorld(e.offsetX, e.offsetY);
  state.cam.x += wx - nx;
  state.cam.y += wy - ny;
  needsDraw = true;
}, { passive: false });

function setSelection(sel, normalise = true) {
  if (sel && normalise) {
    sel = [Math.min(sel[0], sel[2]), Math.min(sel[1], sel[3]), Math.max(sel[0], sel[2]), Math.max(sel[1], sel[3])];
  }
  state.selection = sel;
  const info = $("selInfo");
  if (sel && sel[2] - sel[0] >= SNAP && sel[3] - sel[1] >= SNAP) {
    const [x0, y0, x1, y1] = [Math.min(sel[0], sel[2]), Math.min(sel[1], sel[3]), Math.max(sel[0], sel[2]), Math.max(sel[1], sel[3])];
    info.textContent = `${fmtBlocks(x1 - x0)} × ${fmtBlocks(y1 - y0)} blocks at block ${fmtBlocks(x0)}, ${fmtBlocks(y0)}`;
    info.classList.add("set");
    $("clearSel").classList.remove("hidden");
  } else {
    info.textContent = "Drag on the level to select an area";
    info.classList.remove("set");
    $("clearSel").classList.add("hidden");
  }
  needsDraw = true;
}

function setTool(tool) {
  state.tool = tool;
  document.querySelectorAll(".tool[data-tool]").forEach((b) => b.classList.toggle("active", b.dataset.tool === tool));
  $("viewport").classList.toggle("pan-tool", tool === "pan");
}

document.querySelectorAll(".tool[data-tool]").forEach((b) => b.addEventListener("click", () => setTool(b.dataset.tool)));
$("fitBtn").addEventListener("click", () => fitLevel());
$("zoomSelBtn").addEventListener("click", () => state.selection && fitTo(...state.selection, 0.15));
$("clearSel").addEventListener("click", () => setSelection(null));
$("showProposal").addEventListener("change", () => (needsDraw = true));
$("showTriggers").addEventListener("change", () => (needsDraw = true));

window.addEventListener("keydown", (e) => {
  const typing = ["TEXTAREA", "INPUT", "SELECT"].includes(document.activeElement.tagName);
  if (e.code === "Space" && !typing) { spaceDown = true; e.preventDefault(); }
  if (typing) return;
  if (e.key === "s" || e.key === "S") setTool("select");
  if (e.key === "h" || e.key === "H") setTool("pan");
  if (e.key === "f" || e.key === "F") fitLevel();
  if ((e.key === "z" || e.key === "Z") && state.selection) fitTo(...state.selection, 0.15);
  if (e.key === "Escape") setSelection(null);
});
window.addEventListener("keyup", (e) => { if (e.code === "Space") spaceDown = false; });
window.addEventListener("resize", resize);

// --- levels -------------------------------------------------------------------------

async function loadLevels() {
  const select = $("levelSelect");
  try {
    const levels = await api("/api/levels");
    select.innerHTML = levels.length
      ? levels.map((l) => `<option value="${escapeHtml(l.name)}">${escapeHtml(l.name)}  ·  ${l.objects} objects</option>`).join("")
      : "<option value=''>No levels in your Create tab</option>";
    const last = recall("gdclaude.level");
    const pick = levels.find((l) => l.name === (state.level || last)) || levels[0];
    if (pick) { select.value = pick.name; await openLevel(pick.name, state.level !== pick.name); }
  } catch (err) {
    select.innerHTML = "<option value=''>Couldn't read your save file</option>";
    $("emptyState").classList.remove("hidden");
    toast(err.message, "err");
  }
}

async function openLevel(name, refit = true) {
  const changed = state.level !== name;
  state.level = name;
  store("gdclaude.level", name);
  const view = await api(`/api/level?name=${encodeURIComponent(name)}`);
  state.view = view;
  $("emptyState").classList.add("hidden");
  if (changed) {
    $("chat").querySelectorAll(".msg").forEach((m) => m.remove());
    $("chatHint").classList.remove("hidden");
    setSelection(null);
  }
  if (refit) fitLevel(true);
  await refreshProposal();
  needsDraw = true;
}

$("levelSelect").addEventListener("change", (e) => e.target.value && openLevel(e.target.value));
$("reloadBtn").addEventListener("click", loadLevels);

// --- proposal -----------------------------------------------------------------------

async function refreshProposal() {
  if (!state.level) return;
  const p = await api(`/api/proposal?level=${encodeURIComponent(state.level)}`);
  state.proposal = { ...p, removed: new Set(p.removed) };
  const nColours = Object.keys(p.channels || {}).length;
  const has = p.adds || p.removes || nColours;
  $("proposalBar").classList.toggle("hidden", !has);
  const parts = [];
  if (p.adds) parts.push(`<b>+${p.adds}</b> objects`);
  if (p.removes) parts.push(`<b>−${p.removes}</b> removed`);
  if (nColours) parts.push(`<b>${nColours}</b> colour change${nColours > 1 ? "s" : ""}`);
  $("proposalInfo").innerHTML = `Preview: ${parts.join(" · ")}`;
  needsDraw = true;
}

$("acceptBtn").addEventListener("click", async () => {
  try {
    await api("/api/accept", { level: state.level });
    toast(state.status && state.status.gd_running
      ? "Queued! It goes into your level when you close Geometry Dash."
      : "Queued! Press Apply now (or it applies automatically).", "ok");
    await Promise.all([openLevel(state.level, false), pollStatus()]);
  } catch (err) { toast(err.message, "err"); }
});

$("discardBtn").addEventListener("click", async () => {
  await api("/api/discard", { level: state.level }).catch((err) => toast(err.message, "err"));
  await refreshProposal();
});

// --- chat ---------------------------------------------------------------------------

function addMsg(kind, html) {
  $("chatHint").classList.add("hidden");
  const el = document.createElement("div");
  el.className = `msg ${kind}`;
  el.innerHTML = html;
  $("chat").appendChild(el);
  $("chat").scrollTop = $("chat").scrollHeight;
  return el;
}

function setBusy(busy) {
  $("sendBtn").disabled = busy;
  $("stopBtn").classList.toggle("hidden", !busy);
}

async function send() {
  const prompt = $("prompt").value.trim();
  if (!state.level) return toast("Pick a level first.", "err");
  if (!state.selection) return toast("Drag a box on the level first, to choose where to work.", "err");
  if (!prompt) return $("prompt").focus();
  addMsg("user", escapeHtml(prompt) + (state.image ? `<img src="${state.image}" alt="attached image">` : ""));
  $("prompt").value = "";
  const bubble = addMsg("claude", `<div class="body"><span class="typing"><i></i><i></i><i></i></span></div><div class="activity"></div>`);
  setBusy(true);
  try {
    const { job } = await api("/api/ask", {
      level: state.level, prompt, selection: state.selection, mode: state.mode, image: state.image,
    });
    state.job = job;
    await followJob(job, bubble);
  } catch (err) {
    bubble.remove();
    addMsg("error", escapeHtml(err.message));
  } finally {
    setBusy(false);
    state.job = null;
  }
}

async function followJob(id, bubble) {
  let seen = 0;
  for (;;) {
    await new Promise((r) => setTimeout(r, 450));
    const job = await api(`/api/job?id=${id}`);
    const body = bubble.querySelector(".body");
    if (job.text) body.innerHTML = markdown(job.text);
    const fresh = job.events.slice(seen);
    seen = job.events.length;
    for (const ev of fresh) {
      if (ev.type === "tool") {
        const chip = document.createElement("span");
        chip.textContent = TOOL_LABELS[ev.name] || ev.name;
        bubble.querySelector(".activity").appendChild(chip);
      } else if (ev.type === "note") {
        toast(`Saved to your vault: ${ev.title}`, "ok");
      }
    }
    if (fresh.some((ev) => ev.type === "proposal")) await refreshProposal();
    $("chat").scrollTop = $("chat").scrollHeight;
    if (job.status !== "running") {
      if (job.status === "error") addMsg("error", escapeHtml(job.error));
      if (!job.text && job.status !== "error") body.textContent = "(done)";
      await refreshProposal();
      return;
    }
  }
}

$("composer").addEventListener("submit", (e) => { e.preventDefault(); send(); });
$("prompt").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); send(); }
});
$("stopBtn").addEventListener("click", () => api("/api/stop", { level: state.level }).catch(() => {}));

document.querySelectorAll(".chip").forEach((chip) => chip.addEventListener("click", () => setMode(chip.dataset.mode)));
function setMode(mode) {
  state.mode = mode;
  document.querySelectorAll(".chip").forEach((c) => c.classList.toggle("active", c.dataset.mode === mode));
  const placeholders = {
    "Decorate": "Describe the deco, like \"neon cyberpunk style, purple and cyan glow\"",
    "Pixel art": "What should Claude draw? Like \"a pixel art red dragon breathing fire\"",
    "Image to pixel art": "Attach an image, then press Convert now (free), or ask Claude to use it creatively",
    "Fix structures": "What's wrong? Like \"fix the gaps and make the pillars symmetrical\"",
    "Copy style across the level": "Like \"copy the deco style from blocks 40-60 onto this part\"",
  };
  $("prompt").placeholder = placeholders[mode] || "";
}

// --- images -------------------------------------------------------------------------

function attachImage(file) {
  if (!file || !file.type.startsWith("image/")) return;
  if (file.size > 15 * 1024 * 1024) return toast("That image is too big (max 15 MB).", "err");
  const reader = new FileReader();
  reader.onload = () => {
    state.image = reader.result;
    $("imagePreview").src = state.image;
    $("imageBox").classList.remove("hidden");
    if (state.mode === "Decorate") setMode("Image to pixel art");
  };
  reader.readAsDataURL(file);
}

$("fileInput").addEventListener("change", (e) => attachImage(e.target.files[0]));
$("removeImage").addEventListener("click", () => {
  state.image = null;
  $("imageBox").classList.add("hidden");
  $("fileInput").value = "";
});
window.addEventListener("dragover", (e) => { e.preventDefault(); document.body.classList.add("dragging"); });
window.addEventListener("dragleave", (e) => { if (!e.relatedTarget) document.body.classList.remove("dragging"); });
window.addEventListener("drop", (e) => {
  e.preventDefault();
  document.body.classList.remove("dragging");
  attachImage(e.dataTransfer.files[0]);
});
window.addEventListener("paste", (e) => {
  const item = [...(e.clipboardData || {}).items || []].find((i) => i.type.startsWith("image/"));
  if (item) attachImage(item.getAsFile());
});

$("convertBtn").addEventListener("click", async () => {
  if (!state.selection) return toast("Drag a box on the level first: the image is fitted into it.", "err");
  try {
    const r = await api("/api/convert", {
      level: state.level, selection: state.selection, image: state.image,
      pixel_size: +$("pixelSize").value, colours: +$("colourCount").value,
    });
    addMsg("claude", `<p>Converted your image to <b>${r.pixels}</b> pixels with <b>${r.colours}</b> colours, using <b>${r.objects}</b> objects. Check the preview, then Accept.</p>`);
    await refreshProposal();
  } catch (err) { toast(err.message, "err"); }
});

// --- status, queue, GD --------------------------------------------------------------

async function pollStatus() {
  try {
    const s = await api("/api/status");
    const prev = state.status;
    state.status = s;
    const pill = $("gdStatus");
    pill.classList.toggle("open", s.gd_running);
    pill.classList.toggle("closed", !s.gd_running);
    pill.querySelector(".label").textContent = s.gd_running ? "GD is open" : "GD is closed";
    $("queueCount").textContent = s.queue.length;
    $("queueCount").classList.toggle("has", s.queue.length > 0);
    $("applyBtn").disabled = !s.queue.length || s.gd_running;
    $("applyBtn").title = s.gd_running ? "Close Geometry Dash first (Save and Exit)" : "Write queued changes into your save";
    $("launchBtn").disabled = s.gd_running;
    renderQueue(s.queue);
    if (prev && s.event && s.event !== prev.event) toast(s.event, s.event.startsWith("Couldn't") ? "err" : "ok");
    if (state.saveMtime !== null && s.save_mtime !== state.saveMtime && state.level) {
      await loadLevels(); // GD (or an apply) changed the save: refresh the list and the view
    }
    state.saveMtime = s.save_mtime;
    if (!prev && !s.api_key_set) openSettings(true);
  } catch (_) { /* backend restarting; try again soon */ }
}

function renderQueue(queue) {
  $("queuePopover").innerHTML = queue.length
    ? queue.map((q, i) => `<div class="queue-item"><div class="what"><b>${escapeHtml(q.summary)}</b>
        <small>${escapeHtml(q.level)} · +${q.adds}${q.removes ? ` −${q.removes}` : ""}</small></div>
        <button class="icon-btn" data-unqueue="${i}" title="Remove from queue"><svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg></button></div>`).join("")
      + `<div class="muted" style="padding:6px 8px">Applied automatically when Geometry Dash is closed.</div>`
    : `<div class="queue-empty">Nothing queued. Accept a preview to add it here.</div>`;
}

$("queueBtn").addEventListener("click", (e) => { e.stopPropagation(); $("queuePopover").classList.toggle("hidden"); });
document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-unqueue]");
  if (btn) {
    api("/api/unqueue", { index: +btn.dataset.unqueue }).then(() => Promise.all([pollStatus(), openLevel(state.level, false)]));
    return;
  }
  if (!e.target.closest(".queue-wrap")) $("queuePopover").classList.add("hidden");
});

$("applyBtn").addEventListener("click", async () => {
  try {
    const r = await api("/api/apply", {});
    toast(`Applied ${r.added} objects. Open Geometry Dash to see them!`, "ok");
    await pollStatus();
  } catch (err) { toast(err.message, "err"); }
});
$("launchBtn").addEventListener("click", () => api("/api/launch_gd", {}).catch((err) => toast(err.message, "err")));

// --- settings -----------------------------------------------------------------------

async function openSettings(welcome = false) {
  const s = await api("/api/settings");
  $("welcome").classList.toggle("hidden", !welcome);
  $("apiKey").value = "";
  $("apiKey").placeholder = s.api_key_set ? `saved (${s.api_key_hint})` : "sk-ant-…";
  $("apiKeyHint").textContent = s.api_key_set ? "Leave empty to keep the saved key." : "";
  $("vaultPath").value = s.vault;
  $("savePath").value = s.save_path;
  $("saveHint").textContent = s.save_found ? "Found ✓" : "Not found: check the path";
  $("autoApply").checked = s.auto_apply;
  $("model").value = s.model;
  $("catalogInfo").textContent = `Object catalog: ${s.catalog}`;
  const backups = await api("/api/backups");
  $("backups").innerHTML = backups.length
    ? backups.map((b) => `<div class="backup-row"><span>${new Date(b.time * 1000).toLocaleString()}</span>
        <button class="btn ghost small" data-restore="${escapeHtml(b.name)}">Restore</button></div>`).join("")
    : `<div class="backup-row"><span>No backups yet</span></div>`;
  $("settingsModal").classList.remove("hidden");
}

$("settingsBtn").addEventListener("click", () => openSettings(false));
$("closeSettings").addEventListener("click", () => $("settingsModal").classList.add("hidden"));
$("settingsModal").addEventListener("click", (e) => {
  if (e.target === $("settingsModal")) $("settingsModal").classList.add("hidden");
  const restore = e.target.closest("[data-restore]");
  if (restore && confirm("Replace your current save with this backup? (Geometry Dash must be closed.)")) {
    api("/api/restore", { name: restore.dataset.restore })
      .then(() => { toast("Backup restored.", "ok"); loadLevels(); })
      .catch((err) => toast(err.message, "err"));
  }
});
$("browseVault").addEventListener("click", async () => {
  try {
    const r = await api("/api/pick_folder", {});
    if (r.path) $("vaultPath").value = r.path;
  } catch (err) { toast(err.message, "err"); }
});
$("saveSettings").addEventListener("click", async () => {
  const body = { vault: $("vaultPath").value, save_path: $("savePath").value,
                 auto_apply: $("autoApply").checked, model: $("model").value };
  if ($("apiKey").value.trim()) body.api_key = $("apiKey").value.trim();
  try {
    await api("/api/settings", body);
    $("settingsModal").classList.add("hidden");
    toast("Settings saved.", "ok");
    loadLevels();
  } catch (err) { toast(err.message, "err"); }
});

// --- start --------------------------------------------------------------------------

resize();
setMode("Decorate");
requestAnimationFrame(loop);
loadLevels();
pollStatus();
setInterval(pollStatus, 2000);
