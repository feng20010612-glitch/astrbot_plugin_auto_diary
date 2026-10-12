// 自动日记本 —— 插件页面前端

const bridge = window.AstrBotPluginPage;

const state = {
  year: new Date().getFullYear(),
  month: new Date().getMonth() + 1,
  dates: {},
  selected: null,
  today: toISO(new Date()),
};

function pad(n) {
  return String(n).padStart(2, "0");
}

function toISO(d) {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function escapeHtml(value) {
  const map = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  return String(value ?? "").replace(/[&<>"']/g, (m) => map[m]);
}

async function apiGet(path, params) {
  if (!bridge || !bridge.apiGet) {
    throw new Error("页面桥接不可用，请从 AstrBot 面板打开本页");
  }
  const res = await bridge.apiGet(path, params);
  if (res && res.ok === false) {
    throw new Error(res.error || "请求失败");
  }
  if (res && Object.prototype.hasOwnProperty.call(res, "data")) {
    return res.data;
  }
  return res;
}

async function apiPost(path, body) {
  if (!bridge || !bridge.apiPost) {
    throw new Error("页面桥接不可用，请从 AstrBot 面板打开本页");
  }
  const res = await bridge.apiPost(path, body);
  if (res && res.ok === false) {
    throw new Error(res.error || "请求失败");
  }
  if (res && Object.prototype.hasOwnProperty.call(res, "data")) {
    return res.data;
  }
  return res;
}

function monthRange(year, month) {
  const start = `${year}-${pad(month)}-01`;
  const last = new Date(year, month, 0).getDate();
  return { start, end: `${year}-${pad(month)}-${pad(last)}` };
}

function setHeader(title, meta) {
  document.getElementById("entryTitle").textContent = title;
  document.getElementById("entryDate").textContent = meta || "";
}

// ---------------- 日历 ----------------

async function loadMonth() {
  document.getElementById("monthLabel").textContent = `${state.year} 年 ${state.month} 月`;
  try {
    state.dates = (await apiGet("dates", monthRange(state.year, state.month))) || {};
  } catch (e) {
    console.error("加载日历失败", e);
    state.dates = {};
  }
  renderCalendar();
}

function renderCalendar() {
  const grid = document.getElementById("calendar");
  grid.innerHTML = "";
  const offset = (new Date(state.year, state.month - 1, 1).getDay() + 6) % 7;
  const dayCount = new Date(state.year, state.month, 0).getDate();

  for (let i = 0; i < offset; i += 1) {
    const blank = document.createElement("div");
    blank.className = "day blank";
    grid.appendChild(blank);
  }

  for (let d = 1; d <= dayCount; d += 1) {
    const iso = `${state.year}-${pad(state.month)}-${pad(d)}`;
    const cell = document.createElement("button");
    cell.className = "day";
    cell.textContent = String(d);
    if (state.dates[iso]) cell.classList.add("has-entry");
    if (iso === state.today) cell.classList.add("today");
    if (iso === state.selected) cell.classList.add("selected");
    cell.addEventListener("click", () => showEntry(iso));
    grid.appendChild(cell);
  }
}

// ---------------- 正文 ----------------

const PLATFORM_LABELS = {
  webchat: "网页",
  aiocqhttp: "QQ",
  qq_official: "QQ官方",
  telegram: "Telegram",
  discord: "Discord",
  lark: "飞书",
  dingtalk: "钉钉",
};

// 从标签里认出这条日记属于哪条线（会话/平台）
// 关键是取「能区分不同会话」的那一段——之前取开头，几条线看着都一样
function sourceOf(tags) {
  const text = String(tags || "");
  const sess = text.match(/sess:([^,]+)/);
  if (sess) {
    const parts = String(sess[1]).split("_").filter(Boolean);
    const platRaw = parts[0] || "";
    const plat = PLATFORM_LABELS[platRaw] || platRaw;
    const tail = parts[parts.length - 1] || "";
    const short = tail.slice(0, 8);
    if (short && plat) return `${plat} · ${short}`;
    if (short) return short;
    return String(sess[1]).slice(0, 14);
  }
  const plat = text.match(/plat:([^,]+)/);
  if (plat) return PLATFORM_LABELS[plat[1]] || plat[1];
  return "";
}

function timeOf(stamp) {
  const m = String(stamp || "").match(/(\d{2}:\d{2})/);
  return m ? m[1] : "";
}

function lineLabel(entry) {
  return [personaOf(entry.tags), sourceOf(entry.tags)].filter(Boolean).join(" · ");
}

function setBackVisible(on) {
  const btn = document.getElementById("backToListBtn");
  if (btn) btn.style.display = on ? "" : "none";
}

async function showEntry(iso) {
  state.selected = iso;
  renderCalendar();
  const body = document.getElementById("entryBody");
  body.innerHTML = '<p class="empty">正在读取…</p>';
  setEditMode(false);
  setBackVisible(false);
  try {
    const data = (await apiGet("entry", { date: iso })) || {};
    currentEntries = data.entries || [];
    currentEntry = currentEntries[0] || null;

    if (!currentEntries.length) {
      setHeader("这一天还没有日记", iso);
      body.innerHTML = '<p class="empty">要等聊完安静下来、我写完才有哦。</p>';
      return;
    }
    if (currentEntries.length === 1) {
      showOneEntry(0);
      return;
    }
    showDayList(iso);
  } catch (e) {
    currentEntries = [];
    currentEntry = null;
    setHeader("读取失败", "");
    body.innerHTML = `<p class="empty">${escapeHtml(e.message)}</p>`;
  }
}

// 这天有好几篇：先列出来，点哪篇看哪篇
function showDayList(iso) {
  const body = document.getElementById("entryBody");
  const day = iso || currentEntries[0].date;
  setHeader(`${day} 的日记`, `共 ${currentEntries.length} 篇`);
  setBackVisible(false);
  body.innerHTML = currentEntries
    .map((entry, i) => {
      const line = lineLabel(entry);
      const snippet = String(entry.content || "").replace(/\s+/g, " ").slice(0, 56);
      return `<div class="day-item" data-i="${i}">
        <div class="day-item-head">
          <span class="day-item-line">${escapeHtml(line || "默认")}</span>
          <span class="day-item-time">${escapeHtml(timeOf(entry.updated_at))}</span>
        </div>
        <div class="day-item-title">${escapeHtml(entry.title || "（无标题）")}</div>
        <div class="day-item-snippet">${escapeHtml(snippet)}…</div>
      </div>`;
    })
    .join("");
  body.querySelectorAll(".day-item").forEach((el) => {
    el.addEventListener("click", () => showOneEntry(Number(el.dataset.i)));
  });
}

// 只看某一篇
function showOneEntry(index) {
  const entry = currentEntries[index];
  if (!entry) return;
  currentEntry = entry;
  const body = document.getElementById("entryBody");
  const line = lineLabel(entry);
  setHeader(entry.title || "（无标题）", [entry.date, line].filter(Boolean).join(" · "));
  setBackVisible(currentEntries.length > 1);
  const delBtn = document.getElementById("deleteDayBtn");
  if (delBtn) delBtn.textContent = currentEntries.length > 1 ? "删除这篇" : "删除这天";
  body.innerHTML = `<div class="entry-text">${escapeHtml(entry.content || "")}</div>`;
}

// 多篇时：单独编辑某一篇
function personaOf(tags) {
  const match = String(tags || "").match(/persona:([^,]+)/);
  return match ? match[1] : "";
}

function renderHits(rows, metaBuilder) {
  const body = document.getElementById("entryBody");
  body.innerHTML = rows
    .map((r) => {
      const who = personaOf(r.tags);
      const line = sourceOf(r.tags);
      return `<div class="hit" data-date="${escapeHtml(r.date)}" data-id="${
        r.id != null ? escapeHtml(String(r.id)) : ""
      }">
        <div class="hit-date">${escapeHtml(r.date)}${
          who ? `<span class="hit-who">${escapeHtml(who)}</span>` : ""
        }${line ? `<span class="hit-line">${escapeHtml(line)}</span>` : ""}</div>
        <div class="hit-title">${escapeHtml(r.title || "（无标题）")}</div>
        <div class="hit-snippet">${escapeHtml(metaBuilder(r))}</div>
      </div>`;
    })
    .join("");
  body.querySelectorAll(".hit").forEach((el) => {
    el.addEventListener("click", () => jumpTo(el.dataset.date, el.dataset.id));
  });
}

// ---------------- 搜索 / 最近 ----------------

async function doSearch() {
  const keyword = document.getElementById("searchInput").value.trim();
  const body = document.getElementById("entryBody");
  if (!keyword) {
    await showEntry(state.selected || state.today);
    return;
  }
  body.innerHTML = '<p class="empty">正在搜索…</p>';
  try {
    const hits = (await apiGet("search", { q: keyword, limit: 30 })) || [];
    setHeader(`搜索「${keyword}」`, `${hits.length} 条结果`);
    if (!hits.length) {
      body.innerHTML = '<p class="empty">没有找到相关日记。</p>';
      return;
    }
    renderHits(hits, (r) => r.snippet || "");
  } catch (e) {
    setHeader("搜索失败", "");
    body.innerHTML = `<p class="empty">${escapeHtml(e.message)}</p>`;
  }
}

async function showRecent() {
  const body = document.getElementById("entryBody");
  body.innerHTML = '<p class="empty">正在读取…</p>';
  try {
    const rows = (await apiGet("list", { limit: 30 })) || [];
    setHeader("最近写过的日记", `${rows.length} 篇`);
    if (!rows.length) {
      body.innerHTML = '<p class="empty">还没有任何日记。</p>';
      return;
    }
    renderHits(rows, (r) => `${r.size || 0} 字 · 更新于 ${r.updated_at || ""}`);
  } catch (e) {
    setHeader("读取失败", "");
    body.innerHTML = `<p class="empty">${escapeHtml(e.message)}</p>`;
  }
}

async function jumpTo(iso, entryId) {
  const [y, m] = iso.split("-");
  const changed = Number(y) !== state.year || Number(m) !== state.month;
  state.year = Number(y);
  state.month = Number(m);
  if (changed) await loadMonth();
  await showEntry(iso);
  // 从列表里点进来的，直接展开那一条，而不是停在这天的清单上
  if (entryId) {
    const index = currentEntries.findIndex((e) => String(e.id) === String(entryId));
    if (index >= 0) showOneEntry(index);
  }
}

// ---------------- 阅读偏好（字体 / 字号） ----------------

const FONT_CHOICES = [
  { label: "微软雅黑", value: '"Microsoft YaHei UI", "PingFang SC", sans-serif' },
  { label: "宋体", value: 'SimSun, "Songti SC", serif' },
  { label: "楷体", value: 'KaiTi, "Kaiti SC", serif' },
  { label: "黑体", value: 'SimHei, "Heiti SC", sans-serif' },
  { label: "仿宋", value: 'FangSong, serif' },
  { label: "等线", value: 'DengXian, sans-serif' },
  { label: "系统默认", value: "system-ui, -apple-system, sans-serif" },
];

const SIZE_CHOICES = [12, 13, 14, 15, 16, 17, 18, 20, 22];
const PREF_KEY = "astrbot-plugin-auto-diary-prefs";

function loadPrefs() {
  try {
    return JSON.parse(localStorage.getItem(PREF_KEY) || "{}");
  } catch (e) {
    return {};
  }
}

function savePrefs(prefs) {
  try {
    localStorage.setItem(PREF_KEY, JSON.stringify(prefs));
  } catch (e) {
    /* 隐身模式下忽略 */
  }
}

function applyPrefs(prefs) {
  const style = document.documentElement.style;
  style.setProperty("--reader-font", prefs.font || FONT_CHOICES[0].value);
  style.setProperty("--reader-size", `${prefs.size || 14}px`);
}

// 内存里也留一份：localStorage 被沙箱禁用时，切换项之间不会互相覆盖
let activePrefs = loadPrefs();
let currentEntry = null;   // 当天第一篇（单篇时就是它）
let currentEntries = [];   // 当天全部日记
let editing = false;

// 存一份到插件目录：换浏览器、刷新页面都还在
function persistPrefs() {
  savePrefs(activePrefs);
  applyPrefs(activePrefs);
  apiPost("prefs", activePrefs).catch((e) => console.warn("偏好保存失败", e));
}

async function initPrefs() {
  let remote = {};
  try {
    remote = (await apiGet("prefs")) || {};
  } catch (e) {
    console.warn("读取偏好失败，先用本地的", e);
  }
  activePrefs = { ...loadPrefs(), ...remote };

  const fontSelect = document.getElementById("fontSelect");
  const sizeSelect = document.getElementById("sizeSelect");

  if (fontSelect) {
    FONT_CHOICES.forEach((f) => {
      const opt = document.createElement("option");
      opt.value = f.value;
      opt.textContent = f.label;
      fontSelect.appendChild(opt);
    });
    fontSelect.value = activePrefs.font || FONT_CHOICES[0].value;
    if (!fontSelect.value) fontSelect.value = FONT_CHOICES[0].value;
    fontSelect.addEventListener("change", () => {
      activePrefs = { ...activePrefs, font: fontSelect.value };
      persistPrefs();
    });
  }

  if (sizeSelect) {
    SIZE_CHOICES.forEach((s) => {
      const opt = document.createElement("option");
      opt.value = String(s);
      opt.textContent = `${s} px`;
      sizeSelect.appendChild(opt);
    });
    sizeSelect.value = String(activePrefs.size || 14);
    sizeSelect.addEventListener("change", () => {
      activePrefs = { ...activePrefs, size: Number(sizeSelect.value) };
      persistPrefs();
    });
  }

  applyPrefs(activePrefs);
}

// ---------------- 弹层 ----------------

function askConfirm(message, options = {}) {
  const {
    title = "确认操作",
    okText = "确定",
    cancelText = "取消",
    okDanger = true,
  } = options;

  return new Promise((resolve) => {
    const mask = document.getElementById("confirmMask");
    const ok = document.getElementById("confirmOk");
    const cancel = document.getElementById("confirmCancel");
    document.getElementById("confirmTitle").textContent = title;
    document.getElementById("confirmMessage").textContent = message;
    ok.textContent = okText;
    ok.className = okDanger ? "danger-btn" : "ghost-btn primary";
    if (cancelText) {
      cancel.style.display = "";
      cancel.textContent = cancelText;
    } else {
      cancel.style.display = "none";
    }
    mask.classList.add("show");

    function cleanup(result) {
      mask.classList.remove("show");
      ok.removeEventListener("click", onOk);
      cancel.removeEventListener("click", onCancel);
      resolve(result);
    }
    function onOk() { cleanup(true); }
    function onCancel() { cleanup(false); }
    ok.addEventListener("click", onOk);
    cancel.addEventListener("click", onCancel);
  });
}

function notify(message, title = "提示") {
  return askConfirm(message, { title, okText: "知道了", cancelText: "" });
}

// 删除后不做弹窗，去「回收站」就能恢复回来

// ---------------- 编辑 ----------------

function setEditMode(on) {
  editing = !!on;
  document.getElementById("viewActions").style.display = on ? "none" : "flex";
  document.getElementById("editActions").style.display = on ? "flex" : "none";
}

function startEdit() {
  if (!currentEntry) return;
  const body = document.getElementById("entryBody");
  body.innerHTML = '<textarea id="editArea" class="edit-area"></textarea>';
  const area = document.getElementById("editArea");
  area.value = currentEntry.content || "";
  area.focus();
  setEditMode(true);
}

function cancelEdit() {
  setEditMode(false);
  if (state.selected) showEntry(state.selected);
}

async function saveEdit() {
  if (!currentEntry) return;
  const area = document.getElementById("editArea");
  if (!area) return;
  try {
    await apiPost("update", { id: currentEntry.id, content: area.value });
    setEditMode(false);
    await showEntry(state.selected);
  } catch (e) {
    await notify(`保存失败：${e.message}`, "出错了");
  }
}

// ---------------- 那年今日 ----------------

async function showOnThisDay() {
  const body = document.getElementById("entryBody");
  body.innerHTML = '<p class="empty">正在翻旧账…</p>';
  try {
    const rows = (await apiGet("onthisday")) || [];
    setHeader("那年今日", `${rows.length} 篇`);
    if (!rows.length) {
      body.innerHTML = '<p class="empty">往年今天还没写过呢。</p>';
      return;
    }
    renderHits(rows, (r) => String(r.content || "").slice(0, 60).replace(/\n/g, " "));
  } catch (e) {
    setHeader("读取失败", "");
    body.innerHTML = `<p class="empty">${escapeHtml(e.message)}</p>`;
  }
}

// ---------------- 导出 ----------------

async function doExport() {
  const fmt = document.getElementById("exportFormat").value || "md";
  try {
    const data = await apiGet("export", { format: fmt });
    const blob = new Blob([data.content || ""], { type: `${data.mime};charset=utf-8` });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = data.filename || `diary.${fmt}`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    setTimeout(() => URL.revokeObjectURL(url), 3000);
    setHeader(`已导出 ${data.count || 0} 篇`, data.filename || "");
  } catch (e) {
    await notify(`导出失败：${e.message}`, "出错了");
  }
}

// ---------------- 回收站 ----------------

async function showTrash() {
  const body = document.getElementById("entryBody");
  body.innerHTML = '<p class="empty">正在读取…</p>';
  try {
    const data = (await apiGet("trash")) || {};
    const rows = data.entries || [];
    const keep = data.keep_days || 0;
    setHeader("回收站", `${rows.length} 篇`);
    if (!rows.length) {
      body.innerHTML = `<p class="empty">回收站是空的。</p>`;
      return;
    }
    body.innerHTML = `
      <div class="trash-head">
        <span class="muted-note">${
          keep > 0
            ? `这里都是被删掉的日记，超过 ${keep} 天会自动彻底删除。`
            : "这里都是被删掉的日记，会一直保留着。"
        }</span>
        <div class="trash-actions">
          <button id="restoreAllBtn" class="ghost-btn primary">全部恢复</button>
          <button id="emptyTrashBtn" class="danger-btn">彻底清空</button>
        </div>
      </div>
      ${rows
        .map(
          (r) => `<div class="hit">
            <div class="hit-date">${escapeHtml(r.date)}</div>
            <div class="hit-title">${escapeHtml(r.title || "（无标题）")}</div>
            <div class="hit-snippet">删除于 ${escapeHtml(r.deleted_at || "")}</div>
            <button class="ghost-btn restore-one" data-id="${r.id}">恢复这篇</button>
          </div>`
        )
        .join("")}`;

    body.querySelectorAll(".restore-one").forEach((el) => {
      el.addEventListener("click", async () => {
        await apiPost("restore", { ids: [Number(el.dataset.id)] });
        await refreshAll();
        await showTrash();
      });
    });
    const all = document.getElementById("restoreAllBtn");
    if (all) {
      all.addEventListener("click", async () => {
        await apiPost("restore", { all: true });
        await refreshAll();
        await showTrash();
      });
    }

    const empty = document.getElementById("emptyTrashBtn");
    if (empty) {
      empty.addEventListener("click", async () => {
        const ok = await askConfirm(
          `确定彻底清空这 ${rows.length} 篇吗？清空后就真的找不回来了。`,
          { title: "彻底清空回收站", okText: "彻底删除" }
        );
        if (!ok) return;
        try {
          const res = await apiPost("trash/empty", {});
          await refreshAll();
          await showTrash();
          await notify(`已彻底删除 ${(res && res.purged) || 0} 篇。`, "完成");
        } catch (e) {
          await notify(`清空失败：${e.message}`, "出错了");
        }
      });
    }
  } catch (e) {
    setHeader("读取失败", "");
    body.innerHTML = `<p class="empty">${escapeHtml(e.message)}</p>`;
  }
}

// ---------------- 删除 ----------------

async function refreshAll() {
  try {
    const stats = (await apiGet("stats")) || {};
    const parts = [`共 ${stats.count || 0} 篇`];
    if (stats.first) parts.push(`从 ${stats.first} 开始`);
    document.getElementById("stats").textContent = parts.join(" · ");
  } catch (e) {
    document.getElementById("stats").textContent = "统计信息读取失败";
  }
  await loadMonth();
  await showEntry(state.selected || state.today);
}

async function deleteCurrentDay() {
  const day = state.selected;
  if (!day) return;
  const multiple = currentEntries.length > 1 && currentEntry;
  const ok = await askConfirm(
    multiple ? "确定删除这一篇吗？之后可以去回收站找回来。" : `确定删除 ${day} 的日记吗？`,
    { title: multiple ? "删除这篇" : "删除这天", okText: "删除" }
  );
  if (!ok) return;
  try {
    const res = await apiPost("delete", multiple ? { id: currentEntry.id } : { date: day });
    const ids = (res && res.ids) || [];
    await refreshAll();
    if (multiple) {
      await showEntry(day);
    } else {
      setHeader(ids.length ? `已删除 ${day} 的日记（可在回收站恢复）` : "这一天本来就没有日记", day);
    }
  } catch (e) {
    await notify(`删除失败：${e.message}`, "出错了");
  }
}

async function clearAllEntries() {
  const stats = await apiGet("stats").catch(() => ({}));
  const total = (stats && stats.count) || 0;
  const ok = await askConfirm(`确定清空全部 ${total} 篇日记吗？`, {
    title: "清空全部日记",
    okText: "确认清空",
  });
  if (!ok) return;
  try {
    const res = await apiPost("delete", { all: true });
    const ids = (res && res.ids) || [];
    await refreshAll();
    setHeader(`已清空 ${ids.length} 篇日记（可在回收站恢复）`, "");
  } catch (e) {
    await notify(`清空失败：${e.message}`, "出错了");
  }
}

// ---------------- 初始化 ----------------

async function init() {
  await initPrefs();
  document.getElementById("prevMonth").addEventListener("click", async () => {
    state.month -= 1;
    if (state.month === 0) { state.month = 12; state.year -= 1; }
    await loadMonth();
  });
  document.getElementById("nextMonth").addEventListener("click", async () => {
    state.month += 1;
    if (state.month === 13) { state.month = 1; state.year += 1; }
    await loadMonth();
  });
  document.getElementById("todayBtn").addEventListener("click", () => jumpTo(state.today));
  document.getElementById("recentBtn").addEventListener("click", showRecent);
  document.getElementById("deleteDayBtn").addEventListener("click", deleteCurrentDay);
  document.getElementById("editBtn").addEventListener("click", startEdit);
  document.getElementById("backToListBtn").addEventListener("click", () => showDayList(state.selected));
  document.getElementById("saveBtn").addEventListener("click", saveEdit);
  document.getElementById("cancelBtn").addEventListener("click", cancelEdit);
  document.getElementById("onThisDayBtn").addEventListener("click", showOnThisDay);
  document.getElementById("exportBtn").addEventListener("click", doExport);
  document.getElementById("trashBtn").addEventListener("click", showTrash);
  document.getElementById("clearAllBtn").addEventListener("click", clearAllEntries);
  document.getElementById("searchInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter") doSearch();
  });

  try {
    const stats = (await apiGet("stats")) || {};
    const parts = [`共 ${stats.count || 0} 篇`];
    if (stats.first) parts.push(`从 ${stats.first} 开始`);
    document.getElementById("stats").textContent = parts.join(" · ");
  } catch (e) {
    document.getElementById("stats").textContent = "统计信息读取失败";
    console.error(e);
  }

  await loadMonth();
  await showEntry(state.today);
}

init();
