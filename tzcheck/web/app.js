const view = document.getElementById("view");
const ICON = {
  file: '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path d="M7 3h7l5 5v13H7z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M14 3v5h5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>',
  up: '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path d="M12 19V5M6 11l6-6 6 6" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  close: '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>',
  check: '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
};

const state = {
  meta: null,
  files: [],
  previous: null,
  groups: new Set(),
  useLlm: false,
  public: false,
  user: localGet("tzcheck.user") || "",
};

function localGet(k) {
  try { return localStorage.getItem(k); } catch { return null; }
}

function localSet(k, v) {
  try { localStorage.setItem(k, v); } catch {}
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
}

async function getMeta() {
  if (state.meta) return state.meta;
  const r = await fetch("/api/meta");
  if (!r.ok) throw new Error(`meta ${r.status}`);
  state.meta = await r.json();
  if (!state.groups.size) state.meta.groups.forEach(g => state.groups.add(g.key));
  state.useLlm = !!state.meta.llm;
  return state.meta;
}

function setNav(route) {
  document.querySelectorAll(".nav a[data-route]").forEach(a => {
    if (a.dataset.route === route) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  document.getElementById("nav").classList.remove("open");
  document.getElementById("menu-btn").setAttribute("aria-expanded", "false");
}

function maxFiles() {
  return (state.meta && state.meta.max_files) || 10;
}

function renderFiles() {
  const list = document.getElementById("files");
  const err = document.getElementById("files-err");
  list.innerHTML = state.files.map((f, i) => `
    <li class="file">
      ${ICON.file}
      <span class="file-name" title="${esc(f.name)}">${esc(f.name)}</span>
      <span class="file-role ${i === 0 ? "main" : ""}">${i === 0 ? "основное ТЗ" : "приложение"}</span>
      ${i > 0 ? `<button type="button" class="file-act" data-up="${i}" title="Сделать основным" aria-label="Сделать основным">${ICON.up}</button>` : ""}
      <button type="button" class="file-act" data-rm="${i}" title="Убрать" aria-label="Убрать ${esc(f.name)}">${ICON.close}</button>
    </li>`).join("");
  document.getElementById("file-count").textContent = state.files.length ? `${state.files.length} из ${maxFiles()}` : `до ${maxFiles()} файлов`;
  err.hidden = true;
  updateSubmit();
}

function addFiles(list) {
  const err = document.getElementById("files-err");
  const incoming = Array.from(list || []);
  const room = maxFiles() - state.files.length;
  if (incoming.length > room) {
    err.textContent = `Не больше ${maxFiles()} файлов, лишние не добавлены.`;
    state.files.push(...incoming.slice(0, Math.max(room, 0)));
    renderFiles();
    err.hidden = false;
    return;
  }
  state.files.push(...incoming);
  renderFiles();
}

function renderGroups() {
  const box = document.getElementById("groups");
  box.innerHTML = state.meta.groups.map(g => `
    <button type="button" class="pill" data-g="${esc(g.key)}" aria-pressed="${state.groups.has(g.key)}" title="${esc(g.categories.join(", "))}">${ICON.check}${esc(g.title)}</button>`).join("");
  const cats = document.getElementById("cats");
  cats.textContent = "Выберите хотя бы одну группу";
  cats.hidden = state.groups.size > 0;
  updateSubmit();
}

function updateSubmit() {
  const btn = document.getElementById("submit");
  if (btn) btn.disabled = !state.files.length || !state.groups.size;
}

async function renderStart() {
  setNav("check");
  view.innerHTML = "";
  view.appendChild(document.getElementById("tpl-start").content.cloneNode(true));
  try {
    await getMeta();
  } catch (e) {
    const err = document.getElementById("submit-err");
    err.textContent = "Сервис не отвечает. Обновите страницу.";
    err.hidden = false;
    return;
  }
  const llm = document.getElementById("use-llm");
  llm.checked = state.useLlm && !!state.meta.llm;
  if (!state.meta.llm) {
    llm.disabled = true;
    document.getElementById("llm-row").classList.add("disabled");
    document.getElementById("llm-note").textContent = "модель не настроена";
  } else {
    document.getElementById("llm-note").textContent = state.meta.model ? state.meta.model.replace(/^openrouter\//, "") : "";
  }
  llm.addEventListener("change", () => { state.useLlm = llm.checked; });
  const pub = document.getElementById("public");
  pub.checked = state.public;
  pub.addEventListener("change", () => { state.public = pub.checked; });
  const user = document.getElementById("user");
  user.value = state.user;
  user.addEventListener("input", () => { state.user = user.value; localSet("tzcheck.user", user.value); });

  renderGroups();
  renderFiles();

  document.getElementById("groups").addEventListener("click", e => {
    const b = e.target.closest(".pill");
    if (!b) return;
    const k = b.dataset.g;
    state.groups.has(k) ? state.groups.delete(k) : state.groups.add(k);
    renderGroups();
  });

  const input = document.getElementById("file-input");
  input.addEventListener("change", () => { addFiles(input.files); input.value = ""; });
  const drop = document.getElementById("drop");
  ["dragenter", "dragover"].forEach(t => drop.addEventListener(t, e => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach(t => drop.addEventListener(t, () => drop.classList.remove("over")));
  drop.addEventListener("drop", e => { e.preventDefault(); addFiles(e.dataTransfer.files); });

  document.getElementById("files").addEventListener("click", e => {
    const up = e.target.closest("[data-up]");
    const rm = e.target.closest("[data-rm]");
    if (up) {
      const i = +up.dataset.up;
      const [f] = state.files.splice(i, 1);
      state.files.unshift(f);
      renderFiles();
    } else if (rm) {
      state.files.splice(+rm.dataset.rm, 1);
      renderFiles();
    }
  });

  const prevToggle = document.getElementById("prev-toggle");
  const prevBox = document.getElementById("prev-box");
  const prevInput = document.getElementById("prev-input");
  const prevName = document.getElementById("prev-name");
  const showPrev = () => {
    prevBox.hidden = !state.previous && prevToggle.getAttribute("aria-expanded") !== "true";
    prevName.textContent = state.previous ? state.previous.name : "Выбрать прошлый документ";
    prevName.parentElement.classList.toggle("has", !!state.previous);
  };
  prevToggle.addEventListener("click", () => {
    const open = prevToggle.getAttribute("aria-expanded") !== "true";
    prevToggle.setAttribute("aria-expanded", String(open));
    if (!open) state.previous = null;
    prevBox.hidden = !open;
    showPrev();
  });
  prevInput.addEventListener("change", () => { state.previous = prevInput.files[0] || null; showPrev(); });
  if (state.previous) { prevToggle.setAttribute("aria-expanded", "true"); showPrev(); }

  document.getElementById("check-form").addEventListener("submit", submit);
}

async function submit(e) {
  e.preventDefault();
  const btn = document.getElementById("submit");
  const err = document.getElementById("submit-err");
  if (!state.files.length || !state.groups.size) return;
  err.hidden = true;
  btn.disabled = true;
  btn.textContent = "Отправка";
  const fd = new FormData();
  fd.append("file", state.files[0]);
  state.files.slice(1).forEach(f => fd.append("attachments", f));
  if (state.previous) fd.append("previous", state.previous);
  fd.append("use_llm", state.useLlm && state.meta.llm ? "1" : "0");
  fd.append("checks", Array.from(state.groups).join(","));
  fd.append("public", state.public ? "1" : "0");
  if (state.user.trim()) fd.append("user", state.user.trim());
  try {
    const r = await fetch("/api/jobs", {method: "POST", body: fd});
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      const msg = data.error || (Array.isArray(data.detail) ? data.detail.map(d => d.msg).join("; ") : data.detail) || `Ошибка ${r.status}`;
      throw new Error(msg);
    }
    location.hash = `#/job/${data.job_id}`;
  } catch (ex) {
    err.textContent = `Не удалось запустить проверку: ${ex.message}`;
    err.hidden = false;
    btn.disabled = false;
    btn.textContent = "Проверить";
  }
}


const STEP_ICON = {
  pending: '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><circle cx="12" cy="12" r="8" fill="none" stroke="currentColor" stroke-width="1.6"/></svg>',
  running: '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><circle cx="12" cy="12" r="8" fill="none" stroke="#E3E6EA" stroke-width="2"/><path class="spin" d="M12 4a8 8 0 0 1 8 8" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
  done: '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><circle cx="12" cy="12" r="9" fill="currentColor"/><path d="M8 12.4l2.7 2.7L16.2 9.6" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  skipped: '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path d="M7 12h10" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>',
  not_done: '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path d="M7 12h10" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>',
  error: '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M9 9l6 6M15 9l-6 6" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>',
};

let jobCtl = null;

function stopJob() {
  if (!jobCtl) return;
  jobCtl.alive = false;
  if (jobCtl.es) jobCtl.es.close();
  clearInterval(jobCtl.timer);
  clearTimeout(jobCtl.poll);
  jobCtl = null;
}

function userQ(sep = "?") {
  const u = (state.user || "").trim();
  return u ? `${sep}user=${encodeURIComponent(u)}` : "";
}

function fmtSec(s) {
  if (s == null) return "";
  if (s < 1) return `${s.toFixed(1).replace(".", ",")} с`;
  if (s < 60) return `${Math.round(s)} с`;
  return `${Math.floor(s / 60)} мин ${Math.round(s % 60)} с`;
}

function fmtClock(ms) {
  const t = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, "0")}`;
}

function renderSteps(ctl) {
  const ol = document.getElementById("job-steps");
  if (!ol) return;
  ol.innerHTML = ctl.order.map((id, i) => {
    const st = ctl.steps.get(id);
    const status = st.status || "pending";
    const sec = status === "done" || status === "error" ? fmtSec(st.seconds) : "";
    return `<li class="step ${esc(status)}">
      <span class="step-ico">${STEP_ICON[status] || STEP_ICON.pending}</span>
      <span class="step-title"><span class="step-n">${i + 1}</span>${esc(st.title || id)}</span>
      <span class="step-sec">${esc(sec)}</span>
      <span class="step-text">${esc(st.text || "")}</span>
    </li>`;
  }).join("");
  const finished = ctl.order.filter(id => ["done", "skipped", "not_done", "error"].includes(ctl.steps.get(id).status)).length;
  const pct = ctl.order.length ? Math.round(100 * finished / ctl.order.length) : 0;
  document.getElementById("job-bar-fill").style.width = `${pct}%`;
  document.getElementById("job-bar").setAttribute("aria-valuenow", String(pct));
  const running = ctl.order.map(id => ctl.steps.get(id)).find(s => s.status === "running");
  document.getElementById("job-current").textContent = running ? `${running.title}${running.text ? ": " + running.text : ""}` : "";
}

function applyPlan(ctl, steps) {
  if (!Array.isArray(steps) || !steps.length) return;
  const order = [];
  for (const s of steps) {
    order.push(s.id);
    ctl.steps.set(s.id, {...(ctl.steps.get(s.id) || {}), ...s});
  }
  const kept = ctl.order.filter(id => !order.includes(id) && ctl.steps.get(id).status && ctl.steps.get(id).status !== "pending");
  ctl.order = kept.concat(order);
}

function applyStep(ctl, s) {
  if (!s || !s.id) return;
  ctl.steps.set(s.id, {...(ctl.steps.get(s.id) || {}), ...s});
  if (!ctl.order.includes(s.id)) ctl.order.push(s.id);
}

function setJobState(text, cls) {
  const el = document.getElementById("job-state");
  if (!el) return;
  el.textContent = text;
  el.className = `eyebrow ${cls || ""}`;
}

function groupTitles(keys) {
  return (keys || []).map(k => ((state.meta && state.meta.groups.find(g => g.key === k)) || {}).title || k);
}

function onlyGroups(titles) {
  const all = state.meta ? state.meta.groups.length : 3;
  if (!titles || !titles.length || titles.length >= all) return "";
  return "только " + (titles.length > 1 ? `${titles.slice(0, -1).join(", ")} и ${titles[titles.length - 1]}` : titles[0]);
}

function applySnapshot(ctl, j) {
  if (!j || !ctl.alive) return;
  ctl.job = j;
  if (j.files && j.files.length) {
    document.getElementById("job-file").textContent = j.files[0];
    const parts = j.use_llm === false ? ["без модели"] : [];
    if (j.files.length > 1) parts.push(`приложений: ${j.files.length - 1}`);
    const only = onlyGroups(groupTitles(j.checks));
    if (only) parts.push(only);
    if (j.user) parts.push(`проверяет ${j.user}`);
    document.getElementById("job-meta").textContent = parts.join(", ");
  }
  const t0 = Date.parse(j.started_at || j.created_at || "");
  if (t0) ctl.t0 = t0;
  if (Array.isArray(j.steps) && j.steps.length) applyPlan(ctl, j.steps);
  if (j.status === "queued") setJobState("В очереди", "wait");
  else if (j.status === "running") setJobState("Проверка идёт");
  renderSteps(ctl);
  if (j.status === "done") finishJob(ctl);
  else if (j.status === "error") failJob(ctl, j.error);
  else if (j.status === "lost") lostJob(ctl);
}

function failJob(ctl, error) {
  if (!ctl.alive) return;
  setJobState("Проверка остановилась", "fail");
  clearInterval(ctl.timer);
  if (ctl.es) ctl.es.close();
  const foot = document.getElementById("job-foot");
  foot.hidden = false;
  foot.innerHTML = `<p class="err" style="margin:0;flex-basis:100%">${esc(error || "Ошибка проверки")}</p>
    <a class="btn-secondary" href="#/run/${esc(ctl.id)}">Открыть результат</a>
    <a class="btn-secondary" href="#/">Новая проверка</a>`;
}

function lostJob(ctl) {
  if (!ctl.alive) return;
  setJobState("Проверка прервана", "fail");
  clearInterval(ctl.timer);
  if (ctl.es) ctl.es.close();
  const foot = document.getElementById("job-foot");
  foot.hidden = false;
  foot.innerHTML = `<p class="muted" style="margin:0;flex-basis:100%">Сервис перезапускался, ход этой проверки не сохранился.</p>
    <a class="btn-secondary" href="#/">Запустить заново</a>`;
}

async function finishJob(ctl) {
  if (!ctl.alive || ctl.finishing) return;
  ctl.finishing = true;
  setJobState("Готово", "done");
  clearInterval(ctl.timer);
  if (ctl.es) ctl.es.close();
  document.getElementById("job-bar-fill").style.width = "100%";
  document.getElementById("job-current").textContent = "Загрузка результата";
  try {
    const r = await fetch(`/api/runs/${encodeURIComponent(ctl.id)}${userQ()}`);
    if (r.ok) state.lastRun = {id: ctl.id, data: await r.json()};
  } catch {}
  setTimeout(() => { if (ctl.alive) location.hash = `#/run/${ctl.id}`; }, 600);
}

async function pollJob(ctl) {
  if (!ctl.alive) return;
  try {
    const r = await fetch(`/api/jobs/${encodeURIComponent(ctl.id)}?result=false${userQ("&")}`);
    if (r.status === 404) return lostJob(ctl);
    applySnapshot(ctl, await r.json());
  } catch {}
  const st = ctl.job && ctl.job.status;
  if (ctl.alive && !ctl.finishing && !["error", "lost", "done"].includes(st)) ctl.poll = setTimeout(() => pollJob(ctl), 2000);
}

async function renderJob(id) {
  stopJob();
  setNav("check");
  view.innerHTML = "";
  view.appendChild(document.getElementById("tpl-job").content.cloneNode(true));
  try { await getMeta(); } catch {}
  const ctl = jobCtl = {id, alive: true, steps: new Map(), order: [], t0: Date.now(), job: null};
  ctl.timer = setInterval(() => {
    const el = document.getElementById("job-timer");
    if (el) el.textContent = fmtClock(Date.now() - ctl.t0);
  }, 500);
  if (!("EventSource" in window)) return pollJob(ctl);
  const es = ctl.es = new EventSource(`/api/jobs/${encodeURIComponent(id)}/events${userQ()}`);
  const parse = e => { try { return JSON.parse(e.data); } catch { return null; } };
  es.addEventListener("snapshot", e => applySnapshot(ctl, parse(e)));
  es.addEventListener("plan", e => { const d = parse(e); if (d) { applyPlan(ctl, d.steps); renderSteps(ctl); } });
  es.addEventListener("step", e => { const d = parse(e); if (d) { applyStep(ctl, d); renderSteps(ctl); } });
  es.addEventListener("done", () => finishJob(ctl));
  es.addEventListener("error", e => {
    const d = e.data ? parse(e) : null;
    if (d && d.status === "error") return failJob(ctl, d.error);
    if (es.readyState === EventSource.CLOSED && ctl.alive && !ctl.finishing) pollJob(ctl);
  });
}


const STATUS_ORDER = ["ошибка", "неясно", "ручная проверка", "рекомендация"];
const STATUS_CLS = {"ошибка": "s-error", "неясно": "s-unclear", "ручная проверка": "s-manual", "рекомендация": "s-rec"};
const statusTitle = s => (((state.meta && state.meta.status_titles) || {"ручная проверка": "на проверку"})[s]) || s || "";
const VERDICT_CLS = {blocked: "s-error", fix: "s-unclear", review_high: "s-manual", review: "s-manual", clean: "s-rec", partial: "s-unclear", incomplete: "s-unclear"};
const SEV = {high: "высокий риск", medium: "средний риск", low: "низкий риск"};
const SEV_RANK = {high: 0, medium: 1, low: 2};
const DECISIONS = [["accept", "Принять"], ["reject", "Отклонить"], ["review", "На проверку"]];
const PAGE = 30;
const ICON_DL = '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path d="M12 4v11m0 0-4-4m4 4 4-4M5 19h14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const ICON_COPY = '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><rect x="8" y="8" width="11" height="11" rx="2" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M5 15V6a1 1 0 0 1 1-1h9" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>';
const ICON_SEARCH = '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><circle cx="11" cy="11" r="6.5" fill="none" stroke="currentColor" stroke-width="1.7"/><path d="m16 16 4 4" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>';

let runView = null;

function fmtDate(s) {
  const d = new Date(s);
  if (isNaN(d)) return "";
  return d.toLocaleString("ru-RU", {day: "numeric", month: "long", hour: "2-digit", minute: "2-digit"});
}

function sectionOf(f) {
  return f.section || "main";
}

function filtered(rv) {
  const q = rv.q.trim().toLowerCase();
  let list = rv.data.findings.filter(f => sectionOf(f) === rv.section);
  if (rv.group !== "all") list = list.filter(f => f.group === rv.group);
  if (rv.statuses.size) list = list.filter(f => rv.statuses.has(f.status));
  if (q) list = list.filter(f => [f.id, f.quote, f.explanation, f.fix, f.category, f.location].join(" ").toLowerCase().includes(q));
  if (rv.sort === "risk") list = list.slice().sort((a, b) => (SEV_RANK[a.severity] ?? 3) - (SEV_RANK[b.severity] ?? 3) || STATUS_ORDER.indexOf(a.status) - STATUS_ORDER.indexOf(b.status));
  return list;
}

function normHtml(data, ids) {
  return (ids || []).map(id => {
    const n = (data.norms || {})[id] || {};
    const title = n.title || id;
    const link = n.url ? `<a href="${esc(n.url)}" target="_blank" rel="noopener">${esc(title)}</a>` : esc(title);
    return `<div class="norm"><div class="norm-title">${link}${n.edition ? ` <span class="muted small">${esc(n.edition)}</span>` : ""}</div>${n.text ? `<p class="norm-text">${esc(n.text)}</p>` : ""}</div>`;
  }).join("");
}

const EV_SKIP = new Set(["найдено", "нормы", "source", "norm_ids", "цитата найдена в тексте", "основание из базы норм", "второй блок"]);
const EV_LABEL = {"блок отчёта": "раздел", "довод проверки": "довод второй проверки", "обратная связь": "решения закупщиков"};
const GOST_STATUS = {approved: "утверждён", verified: "сверен", draft: "черновик"};

function evLabel(k) {
  const m = /^уверенность \(модель (при .+)\)$/.exec(k);
  const t = m ? `уверенность ${m[1]}` : EV_LABEL[k] || k;
  return t[0].toUpperCase() + t.slice(1);
}

function evValue(k, v) {
  const plain = x => x === true ? "да" : x === false ? "нет" : Array.isArray(x) ? x.map(plain).join("; ") : x && typeof x === "object" ? Object.entries(x).map(([a, b]) => `${a}: ${plain(b)}`).join("; ") : String(x);
  if (k === "пункт ГОСТ" && v && typeof v === "object") {
    const head = [v.designation, v.clause && `п. ${v.clause}`, GOST_STATUS[v.status] || v.status].filter(Boolean).join(", ");
    return head + (v.quote ? `: «${v.quote}»` : "");
  }
  if (k === "блок отчёта") return String(v).replace(/^на проверку: /, "«На проверку», ");
  if (k === "применимость ГОСТ" && v && typeof v === "object") return [v.why, v.quote && `«${v.quote}»`].filter(Boolean).join("; ");
  return plain(v);
}

function basisTitle(b) {
  return String(b || "").replace(/^модель (при )/, "$1");
}

function evidenceHtml(ev) {
  if (!ev || typeof ev !== "object") return "";
  const rows = Object.entries(ev).filter(([k, v]) => !EV_SKIP.has(k) && v !== null && v !== "" && !(Array.isArray(v) && !v.length));
  if (!rows.length) return "";
  return `<dl class="ev">${rows.map(([k, v]) => `<dt>${esc(evLabel(k))}</dt><dd>${esc(evValue(k, v))}</dd>`).join("")}</dl>`;
}


const TRACE_STEP = {
  found: "Модель нашла",
  reference: "Справочники",
  located: "Цитата в документе",
  incomplete: "Неполный ответ модели",
  category: "Категория",
  absent_search: "Поиск по комплекту",
  norm: "Пункт нормы",
  verify: "Вторая проверка",
  dedupe: "Объединение повторов",
  repeats: "Объединение повторов",
  gost: "Чек-лист ГОСТ",
  route: "Раздел отчёта",
  fixcheck: "Проверка правки",
  final: "Итог",
};
const STAGE_TITLE = {
  passport: "Паспорт закупки", roles: "Роли разделов", tables: "Разметка таблиц", find: "Поиск замечаний",
  refs: "Сверка со справочниками", gost: "Применимость ГОСТ", gost_check: "Сверка с ГОСТ", absent: "Проверка «не указано»",
  cross: "Сверка частей комплекта", verify: "Вторая проверка", fixcheck: "Проверка правок", summary: "Заключение",
};

async function getTrace(rv) {
  if (rv.trace) return rv.trace;
  if (!rv.tracePromise) {
    rv.tracePromise = fetch(`/api/runs/${encodeURIComponent(rv.id)}/trace${userQ()}`)
      .then(r => r.ok ? r.json() : null).catch(() => null)
      .then(t => { rv.trace = t || {calls: [], rejected: []}; rv.callMap = new Map((rv.trace.calls || []).map(c => [c.id, c])); return rv.trace; });
  }
  return rv.tracePromise;
}

function pretty(v) {
  if (v == null) return "";
  if (typeof v === "string") {
    try { return JSON.stringify(JSON.parse(v), null, 2); } catch { return v; }
  }
  return JSON.stringify(v, null, 2);
}

function callMeta(c, full) {
  if (!c) return "";
  const tok = full && c.tokens && (c.tokens.in || c.tokens.out) ? `токены ${c.tokens.in || 0} / ${c.tokens.out || 0}` : "";
  return [c.seconds != null ? fmtSec(c.seconds) : "", tok, c.retry ? `повтор: ${c.retry}` : "", c.error ? `ошибка: ${c.error}` : ""].filter(Boolean).join(", ");
}

const UNIT_KIND = {document: "документ", table: "таблица", position: "позиции", section: "раздел", pair: "две части документа", finding: "замечания"};

function blockTexts(prompt, ids) {
  const text = String(prompt || "");
  const marks = [...text.matchAll(/\[([a-z]\d+(?:r\d+)?)\]\s/g)].map(m => ({id: m[1], start: m.index, body: m.index + m[0].length}));
  const out = new Map();
  marks.forEach((m, i) => {
    if (!ids.includes(m.id) || out.has(m.id)) return;
    const stops = [i + 1 < marks.length ? marks[i + 1].start : text.length, text.indexOf("\n\n", m.body), text.indexOf("<<<", m.body), text.indexOf("\nПозиция ", m.body)].filter(x => x > -1);
    const end = Math.min(...stops);
    out.set(m.id, text.slice(m.body, end).trim());
  });
  return out;
}

function unitHtml(c) {
  const u = c.unit;
  if (!u) return "";
  const blocks = u.blocks || [];
  const title = String(u.title || "");
  const kind = UNIT_KIND[u.kind] || u.kind || "";
  const n = blocks.length;
  const head = title && title.length <= 110 && !title.includes("; ") ? title : n ? `${n} ${plural(n, "фрагмент", "фрагмента", "фрагментов")}${kind ? ` (${kind})` : ""}` : kind;
  if (!n) return `<p class="small"><b>Передано модели:</b> ${esc(head)}</p>`;
  const texts = blockTexts(c.prompt, blocks);
  const items = blocks.map(b => `<li><span class="u-id">${esc(b)}</span><span class="u-text">${esc(texts.get(b) || "текст фрагмента есть в запросе к модели")}</span></li>`).join("");
  return `<details class="io unit"><summary><b>Передано модели:</b> ${esc(head)}</summary><ol class="unit-list">${items}</ol></details>`;
}

function callBody(c) {
  if (!c) return '<p class="muted small">Запись вызова не найдена в журнале.</p>';
  const unit = unitHtml(c);
  return `${unit}
    ${c.system ? `<details class="io"><summary>Системная инструкция модели</summary><pre>${esc(c.system)}</pre></details>` : ""}
    <details class="io"><summary>Запрос к модели <span class="muted small">${(c.prompt || "").length} ${plural((c.prompt || "").length, "знак", "знака", "знаков")}</span></summary><pre>${esc(c.prompt || "")}</pre></details>
    <details class="io"><summary>Ответ модели</summary><pre>${esc(pretty(c.parsed != null ? c.parsed : c.response))}</pre>${c.parsed != null && c.response ? `<details class="io raw"><summary>Сырой текст ответа</summary><pre>${esc(c.response)}</pre></details>` : ""}</details>`;
}

function traceStepHtml(rv, s, i) {
  const c = s.call && rv.callMap ? rv.callMap.get(s.call) : null;
  const title = TRACE_STEP[s.step] || s.step;
  return `<li class="t-step t-${esc(s.step)}">
    <span class="t-n">${i + 1}</span>
    <div class="t-body">
      <div class="t-title">${esc(title)}${callMeta(c) ? ` <span class="muted small">${esc(callMeta(c))}</span>` : ""}</div>
      ${s.text ? `<p class="t-text">${esc(s.text)}</p>` : ""}
      ${s.quote ? `<blockquote class="quote small">${esc(s.quote)}</blockquote>` : ""}
      ${Array.isArray(s.records) && s.records.length ? `<details class="io"><summary>Справочные записи: ${s.records.length}</summary><pre>${esc(s.records.join("\n\n"))}</pre></details>` : ""}
      ${s.call ? callBody(c) : ""}
      ${s.item != null ? `<details class="io"><summary>${s.step === "verify" ? "Ответ второй проверки" : "Ответ модели по замечанию"}</summary><pre>${esc(pretty(s.item))}</pre></details>` : ""}
    </div>
  </li>`;
}

async function fillTrace(rv, f, box) {
  if (box.dataset.ready) return;
  box.dataset.ready = "1";
  box.innerHTML = '<p class="muted small">Загрузка журнала</p>';
  await getTrace(rv);
  box.innerHTML = `<ol class="trace">${(f.trace || []).map((s, i) => traceStepHtml(rv, s, i)).join("")}</ol>`;
}

function rejectedHtml(rv) {
  const list = (rv.trace && rv.trace.rejected) || [];
  if (!list.length) return '<div class="empty">Вторая проверка ничего не сняла.</div>';
  return list.map(f => `<article class="finding rejected">
    <header class="f-head"><div class="f-meta"><span class="f-id">${esc(f.id || "")}</span><span>${esc(f.category || "")}</span>${f.location ? `<span class="muted">${esc(f.location)}</span>` : ""}</div></header>
    ${f.quote ? `<blockquote class="quote">${esc(f.quote)}</blockquote>` : ""}
    ${f.explanation ? `<div class="f-block"><h4>Довод модели</h4><p>${esc(f.explanation)}</p></div>` : ""}
    ${f.trace && f.trace.length ? `<details class="f-more" open><summary>Как нашли и сняли</summary><ol class="trace">${f.trace.map((s, i) => traceStepHtml(rv, s, i)).join("")}</ol></details>` : ""}
  </article>`).join("");
}

const STEP_STATE = {"выполнен": "готово", "неприменим": "пропущено", "не выполнен": "не выполнено"};

function stepState(s) {
  return STEP_STATE[s.state] || s.state || "";
}

function journalHtml(rv) {
  const calls = (rv.trace && rv.trace.calls) || [];
  const steps = rv.data.steps_status || [];
  const stepRows = steps.map(s => `<li><b>${esc(s.title || s.id)}</b><span class="muted">: ${esc(stepState(s))}${s.reason ? `, ${esc(s.reason)}` : ""}</span></li>`).join("");
  const byStage = new Map();
  for (const c of calls) { if (!byStage.has(c.stage)) byStage.set(c.stage, []); byStage.get(c.stage).push(c); }
  const groups = [...byStage.entries()].map(([st, cs]) => `<details class="io stage"><summary><b>${esc(STAGE_TITLE[st] || st)}</b> <span class="muted small">запросов: ${cs.length}, ${fmtSec(cs.reduce((a, c) => a + (c.seconds || 0), 0))}</span></summary>
    ${cs.map(c => `<details class="io call"><summary>${esc(c.title || c.id)} <span class="muted small">${esc(callMeta(c, true))}</span></summary>${callBody(c)}</details>`).join("")}</details>`).join("");
  return `${stepRows ? `<h4>Шаги</h4><ul class="j-steps">${stepRows}</ul>` : ""}
    <h4>Запросы к модели${calls.length ? `: ${calls.length}` : ""}</h4>
    ${calls.length ? groups : '<p class="muted small">Модель не вызывалась.</p>'}`;
}

function findingHtml(rv, f) {
  const dec = (rv.data.decisions || {})[f.id];
  const details = [
    f.confidence_pct != null ? `<p class="small"><b>Уверенность:</b> ${esc(f.confidence_pct)} %${f.confidence_basis ? ` ${esc(basisTitle(f.confidence_basis))}` : ""}</p>` : "",
    evidenceHtml(f.evidence),
    normHtml(rv.data, f.norm_ids),
  ].join("");
  return `<article class="finding ${STATUS_CLS[f.status] || ""}" id="f-${esc(f.id)}">
    <header class="f-head">
      <div class="f-meta"><span class="f-id">${esc(f.id)}</span><span>${esc(f.category || "")}</span>${f.location ? `<span class="muted">${esc(f.location)}${f.also_in && f.also_in.length ? `, ещё мест: ${f.also_in.length}` : ""}</span>` : ""}</div>
      <div class="f-tags"><span class="tag ${STATUS_CLS[f.status] || ""}"><i></i>${esc(statusTitle(f.status))}</span>${f.severity ? `<span class="tag plain">${esc(SEV[f.severity] || f.severity)}</span>` : ""}</div>
    </header>
    ${f.quote ? `<blockquote class="quote">${esc(f.quote)}</blockquote>` : ""}
    ${f.explanation ? `<div class="f-block"><h4>Почему</h4><p>${esc(f.explanation)}</p></div>` : ""}
    ${f.fix ? `<div class="f-block"><h4>Как исправить</h4><p>${esc(f.fix)}</p><button type="button" class="mini" data-copy="${esc(f.fix)}">${ICON_COPY}Скопировать</button></div>` : ""}
    ${f.replacement ? `<div class="f-block repl"><h4>Правка в Word</h4><p><del>${esc(f.quote)}</del></p><p><ins>${esc(f.replacement)}</ins></p></div>` : ""}
    ${f.trace && f.trace.length ? `<details class="f-more f-trace" data-trace="${esc(f.id)}"><summary>Как нашли</summary><div class="t-box"></div></details>` : ""}
    ${details ? `<details class="f-more"><summary>Основание</summary>${details}</details>` : ""}
    <footer class="f-foot">
      <div class="decide" role="group" aria-label="Решение закупщика">${DECISIONS.map(([k, t]) => `<button type="button" data-dec="${k}" data-fid="${esc(f.id)}" aria-pressed="${dec && dec.decision === k}">${t}</button>`).join("")}</div>
    </footer>
  </article>`;
}

function renderList(rv) {
  if (rv.section === "rejected") {
    document.getElementById("r-count").textContent = "";
    const box = document.getElementById("r-list");
    box.innerHTML = '<p class="muted">Загрузка журнала</p>';
    getTrace(rv).then(() => { if (rv.section === "rejected") box.innerHTML = rejectedHtml(rv); });
    return;
  }
  const list = filtered(rv);
  const shown = list.slice(0, rv.limit);
  const box = document.getElementById("r-list");
  const total = rv.data.findings.filter(f => sectionOf(f) === rv.section).length;
  document.getElementById("r-count").textContent = shown.length === total ? "" : `Показано ${shown.length} из ${list.length}${list.length !== total ? ` (в разделе ${total})` : ""}`;
  if (!list.length) {
    const any = rv.group !== "all" || rv.statuses.size || rv.q;
    box.innerHTML = `<div class="empty">${any ? `Под фильтр ничего не попало. <button type="button" class="link-btn inline" id="r-reset">Сбросить фильтры</button>` : rv.section === "main" ? "В основном списке замечаний нет." : "В этом разделе пусто."}</div>`;
  } else {
    box.innerHTML = shown.map(f => findingHtml(rv, f)).join("") + (list.length > shown.length ? `<button type="button" class="btn-secondary more" id="r-more">Показать ещё ${Math.min(PAGE, list.length - shown.length)}</button>` : "");
  }
}

function renderTabs(rv) {
  const d = rv.data;
  const inSec = d.findings.filter(f => sectionOf(f) === rv.section);
  const groups = (state.meta ? state.meta.groups.map(g => g.title) : ["Логика", "Стандарты", "Риски"]);
  const checked = new Set(d.check_groups || groups);
  document.getElementById("r-tabs").innerHTML = [["all", "Все", inSec.length]].concat(groups.map(g => [g, g, inSec.filter(f => f.group === g).length])).map(([k, t, n]) =>
    `<button type="button" role="tab" data-tab="${esc(k)}" aria-selected="${rv.group === k}" ${k !== "all" && !checked.has(k) ? 'title="группа не проверялась"' : ""}>${esc(t)} <span class="n">${k !== "all" && !checked.has(k) ? "не проверялась" : n}</span></button>`).join("");
  const secs = (state.meta && state.meta.sections) || {main: "основной список", llm_review: "на проверку", unchecked: "не проверено"};
  const secCounts = Object.keys(secs).map(k => d.findings.filter(f => sectionOf(f) === k).length);
  const secList = Object.entries(secs).filter(([k], i) => secCounts[i] || k === rv.section).map(([k, t], _, arr) => [k, t, d.findings.filter(f => sectionOf(f) === k).length]);
  const nRej = d.trace_rejected != null ? d.trace_rejected : null;
  if (d.trace_calls) secList.push(["rejected", "снято второй проверкой", nRej]);
  const secBox = document.getElementById("r-sections");
  secBox.hidden = secList.length < 2;
  secBox.innerHTML = secList.map(([k, t, n]) => `<button type="button" data-sec="${esc(k)}" aria-pressed="${rv.section === k}">${esc(t[0].toUpperCase() + t.slice(1))}${n != null ? ` <span class="n">${n}</span>` : ""}</button>`).join("");
  document.querySelector(".tabs-row").hidden = document.querySelector(".filters").hidden = rv.section === "rejected";
  const chips = STATUS_ORDER.map(s => [s, inSec.filter(f => f.status === s).length]).filter(([s, n]) => n || rv.statuses.has(s));
  document.getElementById("r-status").innerHTML = chips.length < 2 && !rv.statuses.size ? "" : chips.map(([s, n]) =>
    `<button type="button" class="dot-btn ${STATUS_CLS[s]}" data-st="${esc(s)}" aria-pressed="${rv.statuses.has(s)}"><i></i>${esc(statusTitle(s))} <span class="n">${n}</span></button>`).join("");
}

function renderRunHead(rv) {
  const d = rv.data;
  const run = d.run || {};
  const files = (d.document && d.document.files) || run.files || [d.file].filter(Boolean);
  document.getElementById("r-file").textContent = files[0] || d.file || "Документ";
  const meta = [];
  if (run.number) meta.push(`проверка № ${run.number}`);
  if (run.created_at) meta.push(fmtDate(run.created_at));
  if (run.user) meta.push(run.user);
  if (files.length > 1) meta.push(`приложений: ${files.length - 1}`);
  if (d.seconds != null) meta.push(`время ${fmtSec(d.seconds)}`);
  meta.push(!d.model ? "без модели" : `модель ${String(d.model).replace(/^openrouter\//, "")}`);
  document.getElementById("r-meta").textContent = meta.join(", ");

  const links = d.links || {};
  const btn = (href, text, cls, open) => href ? `<a class="${cls}" href="${esc(href)}" ${open ? 'target="_blank" rel="noopener"' : "download"}>${open ? "" : ICON_DL}${esc(text)}</a>` : "";
  const rev = d.revisions && d.revisions.main;
  document.getElementById("r-actions").innerHTML = [
    btn(links.revisions_docx, "Правки в Word", "btn-dark"),
    btn(links.comments_docx, "Комментарии в Word", "btn-secondary"),
    btn(links.report_html, "Отчёт для печати", "btn-secondary", true),
    ...Object.keys(links).filter(k => /^attachment_\d+_(comments|revisions)_docx$/.test(k)).map(k => btn(links[k], k.includes("revisions") ? `Правки к приложению ${k.split("_")[1]}` : `Комментарии к приложению ${k.split("_")[1]}`, "btn-secondary")),
    `<a class="btn-secondary" href="#/">Новая проверка</a>`,
  ].join("");
  document.getElementById("r-rev-note").textContent = links.revisions_docx && rev ? `Правок в файле: ${rev.applied ? rev.applied.length : 0}${rev.skipped && Object.keys(rev.skipped).length ? `, пропущено: ${Object.keys(rev.skipped).length}` : ""}.` : "";

  const v = document.getElementById("r-verdict");
  v.className = `verdict ${VERDICT_CLS[d.verdict_code] || ""}`;
  document.getElementById("r-verdict-text").textContent = d.verdict || "";
  const inc = d.completeness && d.completeness.complete === false;
  const incBox = document.getElementById("r-incomplete");
  if ((inc || d.full_check === false || d.status === "incomplete") && !/^Проверка неполная/.test(d.verdict || "")) {
    const steps = ((d.completeness && d.completeness.incomplete_steps) || []).map(s => typeof s === "string" ? s : `${s.title || s.id}: ${stepState(s)}${s.reason ? ", " + s.reason : ""}`);
    const only = d.full_check === false ? onlyGroups(d.check_groups || []) : "";
    incBox.hidden = false;
    incBox.innerHTML = `<b>Проверка неполная.</b> ${only ? `Проверены ${esc(only)}. ` : ""}${steps.length ? `Не выполнено: ${esc(steps.join("; "))}.` : ""}`;
  }
  const pii = d.pii_masked && Object.values(d.pii_masked).reduce((a, b) => a + (typeof b === "number" ? b : 0), 0);
  const warn = (d.warnings || []).filter(Boolean);
  document.getElementById("r-mode").innerHTML = [
    pii ? `<div>Замаскировано персональных данных: ${pii}</div>` : "",
    warn.length ? `<div><details><summary>Предупреждения: ${warn.length}</summary><ul>${warn.map(w => `<li>${esc(typeof w === "string" ? w : JSON.stringify(w))}</li>`).join("")}</ul></details></div>` : "",
  ].join("");

  const jr = document.getElementById("r-journal");
  if (jr) {
    jr.querySelector("summary").textContent = "Журнал проверки";
    jr.addEventListener("toggle", async () => {
      if (!jr.open || jr.dataset.ready) return;
      jr.dataset.ready = "1";
      const body = document.getElementById("r-journal-body");
      body.innerHTML = '<p class="muted small">Загрузка журнала</p>';
      if (d.trace_calls) await getTrace(rv);
      body.innerHTML = journalHtml(rv);
    });
  }
  const main = d.findings.filter(f => sectionOf(f) === "main");
  const present = STATUS_ORDER.filter(s => main.some(f => f.status === s));
  const stats = document.getElementById("r-stats");
  stats.hidden = present.length < 2;
  stats.style.setProperty("--n", String(present.length + 1));
  stats.innerHTML = present.length < 2 ? "" : `<div class="stat"><span class="stat-n">${main.length}</span><span class="stat-l">всего</span></div>` +
    present.map(s => `<div class="stat ${STATUS_CLS[s]}"><span class="stat-n">${main.filter(f => f.status === s).length}</span><span class="stat-l"><i></i>${esc(statusTitle(s))}</span></div>`).join("");
}

async function postDecision(rv, fid, decision, btn) {
  const group = btn.parentElement;
  const prev = group.querySelector('[aria-pressed="true"]');
  group.querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", String(b === btn)));
  try {
    const r = await fetch("/api/feedback", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({run_id: rv.id, finding_id: fid, decision})});
    if (!r.ok) throw new Error(String(r.status));
    rv.data.decisions = rv.data.decisions || {};
    rv.data.decisions[fid] = {decision};
  } catch {
    group.querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", String(b === prev)));
    btn.closest(".finding").insertAdjacentHTML("beforeend", '<p class="err small">Решение не сохранилось, попробуйте ещё раз.</p>');
  }
}

async function renderRun(id) {
  setNav("check");
  view.innerHTML = '<div class="page"><p class="muted">Загрузка результата</p></div>';
  try { await getMeta(); } catch {}
  let data = state.lastRun && state.lastRun.id === id ? state.lastRun.data : null;
  if (!data) {
    const r = await fetch(`/api/runs/${encodeURIComponent(id)}${userQ()}`).catch(() => null);
    if (!r || !r.ok) {
      view.innerHTML = `<div class="page"><h1>Результат не найден</h1><p class="muted">Проверка ${esc(id)} не найдена или скрыта.</p><a class="btn-secondary" href="#/">Новая проверка</a></div>`;
      return;
    }
    data = await r.json();
  }
  data.findings = data.findings || [];
  const saved = runView && runView.id === id ? runView : null;
  const rv = runView = saved ? Object.assign(saved, {data}) : {id, data, group: "all", section: "main", statuses: new Set(), q: "", sort: "risk", limit: PAGE};
  view.innerHTML = "";
  view.appendChild(document.getElementById("tpl-run").content.cloneNode(true));
  renderRunHead(rv);
  renderTabs(rv);
  renderList(rv);
  const search = document.getElementById("r-q");
  search.value = rv.q;
  document.getElementById("r-sort").value = rv.sort;
  const rerender = () => { rv.limit = PAGE; renderTabs(rv); renderList(rv); };
  document.getElementById("r-tabs").addEventListener("click", e => { const b = e.target.closest("[data-tab]"); if (b) { rv.group = b.dataset.tab; rerender(); } });
  document.getElementById("r-sections").addEventListener("click", e => { const b = e.target.closest("[data-sec]"); if (b) { rv.section = b.dataset.sec; rv.group = "all"; rv.statuses.clear(); rerender(); } });
  document.getElementById("r-status").addEventListener("click", e => { const b = e.target.closest("[data-st]"); if (b) { const s = b.dataset.st; rv.statuses.has(s) ? rv.statuses.delete(s) : rv.statuses.add(s); rerender(); } });
  search.addEventListener("input", () => { rv.q = search.value; rv.limit = PAGE; renderList(rv); });
  document.getElementById("r-sort").addEventListener("change", e => { rv.sort = e.target.value; renderList(rv); });
  document.getElementById("r-list").addEventListener("toggle", e => {
    const t = e.target;
    if (t.matches && t.matches("details[data-trace]") && t.open) {
      const f = rv.data.findings.find(x => x.id === t.dataset.trace);
      if (f) fillTrace(rv, f, t.querySelector(".t-box"));
    }
  }, true);
  document.getElementById("r-list").addEventListener("click", e => {
    const more = e.target.closest("#r-more");
    if (more) { rv.limit += PAGE; renderList(rv); return; }
    if (e.target.closest("#r-reset")) { rv.group = "all"; rv.statuses.clear(); rv.q = ""; search.value = ""; rerender(); return; }
    const cp = e.target.closest("[data-copy]");
    if (cp) { navigator.clipboard && navigator.clipboard.writeText(cp.dataset.copy).then(() => { cp.lastChild.textContent = "Скопировано"; setTimeout(() => { cp.lastChild.textContent = "Скопировать"; }, 1500); }).catch(() => {}); return; }
    const dec = e.target.closest("[data-dec]");
    if (dec) postDecision(rv, dec.dataset.fid, dec.dataset.dec, dec);
  });
}


const HIST_PAGE = 30;
let histView = null;

function plural(n, one, few, many) {
  const a = Math.abs(n) % 100, b = a % 10;
  return a > 10 && a < 20 ? many : b === 1 ? one : b > 1 && b < 5 ? few : many;
}

function histCard(it) {
  const c = it.counts || {};
  const dec = it.decisions || {};
  const files = it.files || [];
  const groups = it.full_check === false ? onlyGroups(it.check_groups || []) : "";
  const mode = it.model ? "" : "без модели";
  const decN = dec["всего решений"] || 0;
  return `<a class="h-card ${VERDICT_CLS[it.verdict_code] || ""}" href="#/run/${esc(it.run_id)}">
    <div class="h-main">
      <div class="h-title"><span class="h-num">${it.number ? `№ ${esc(it.number)}` : ""}</span><b>${esc(files[0] || it.run_id)}</b>${files.length > 1 ? `<span class="muted small">+ приложений ${files.length - 1}</span>` : ""}</div>
      <p class="h-verdict">${esc(it.verdict || "")}</p>
      <p class="muted small">${[fmtDate(it.created_at), it.user, groups, mode, it.previous_file ? `сопоставлено с ${it.previous_file}` : ""].filter(Boolean).map(esc).join(", ")}</p>
    </div>
    <div class="h-nums">
      <div><span class="stat-n">${c.total ?? 0}</span><span class="stat-l">${plural(c.total ?? 0, "замечание", "замечания", "замечаний")}</span></div>
      <div class="s-error"><span class="stat-n">${c.errors ?? 0}</span><span class="stat-l"><i></i>${plural(c.errors ?? 0, "ошибка", "ошибки", "ошибок")}</span></div>
      <div><span class="stat-n">${c.high ?? 0}</span><span class="stat-l">высокий риск</span></div>
      <div><span class="stat-n">${decN}</span><span class="stat-l">${plural(decN, "решение", "решения", "решений")}${decN ? `: принято ${dec["принято"] || 0}, отклонено ${dec["отклонено"] || 0}` : ""}</span></div>
    </div>
  </a>`;
}

async function loadHistory(hv, append) {
  const qs = new URLSearchParams({limit: String(HIST_PAGE), offset: String(append ? hv.items.length : 0)});
  if (hv.user.trim()) qs.set("user", hv.user.trim());
  const box = document.getElementById("h-list");
  try {
    const r = await fetch(`/api/runs?${qs}`);
    if (!r.ok) throw new Error(String(r.status));
    const d = await r.json();
    hv.total = d.total || 0;
    hv.items = append ? hv.items.concat(d.items || []) : (d.items || []);
  } catch {
    box.innerHTML = '<div class="empty">Историю не удалось загрузить. Обновите страницу.</div>';
    return;
  }
  renderHistoryList(hv);
}

function renderHistoryList(hv) {
  const q = hv.q.trim().toLowerCase();
  const list = q ? hv.items.filter(it => [(it.files || []).join(" "), it.user, it.verdict, it.number].join(" ").toLowerCase().includes(q)) : hv.items;
  const box = document.getElementById("h-list");
  document.getElementById("h-count").textContent = hv.total ? `Проверок: ${hv.total}${q ? `, найдено на странице: ${list.length}` : ""}` : "";
  if (!list.length) {
    box.innerHTML = `<div class="empty">${hv.items.length ? "Под поиск ничего не попало." : hv.user.trim() ? "У этого проверяющего проверок нет." : 'Проверок ещё не было. <a href="#/">Проверить ТЗ</a>'}</div>`;
    return;
  }
  box.innerHTML = list.map(histCard).join("") + (hv.items.length < hv.total ? `<button type="button" class="btn-secondary more" id="h-more">Показать ещё</button>` : "");
}

async function renderHistory() {
  setNav("history");
  try { await getMeta(); } catch {}
  const hv = histView = {items: [], total: 0, q: "", user: ""};
  view.innerHTML = `<section class="result history">
    <div class="r-top"><div><h1>История проверок</h1></div>
      <a class="btn-dark" href="#/">Новая проверка</a></div>
    <div class="tools h-tools">
      <label class="search">${ICON_SEARCH}<input type="search" id="h-q" placeholder="Файл, проверяющий, вердикт" aria-label="Поиск"></label>
      <label class="text-field h-user"><input type="text" id="h-user" placeholder="Проверяющий" aria-label="Проверяющий" value=""></label>
    </div>
    <p class="muted small" id="h-count"></p>
    <div class="list" id="h-list"><p class="muted">Загрузка</p></div>
  </section>`;
  const qIn = document.getElementById("h-q");
  const uIn = document.getElementById("h-user");
  qIn.addEventListener("input", () => { hv.q = qIn.value; renderHistoryList(hv); });
  let t = null;
  uIn.addEventListener("input", () => { clearTimeout(t); t = setTimeout(() => { hv.user = uIn.value; loadHistory(hv, false); }, 350); });
  document.getElementById("h-list").addEventListener("click", e => { if (e.target.closest("#h-more")) loadHistory(hv, true); });
  loadHistory(hv, false);
}


const KIND_TITLE = {internal: "Внутренние стандарты заказчика", customer_practice: "Практика заказчика", corpus: "Законы и стандарты", edition: "Тексты редакций ГОСТ"};
const ICON_TRASH = '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path d="M5 7h14M10 7V5h4v2M7 7l1 12h8l1-12" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';
let normsView = null;

function normRow(d) {
  const status = (d.status || "").replace(/\s*\([^)]*\.json\)/g, "");
  const st = /^правила практики/.test(status) ? "" : status.charAt(0).toLowerCase() + status.slice(1);
  const named = d.customer && String(d.document || "").includes(d.customer);
  const meta = [d.edition, st, !named && d.customer && `заказчик: ${d.customer}`, d.blocks ? `пунктов ${d.blocks}` : "", d.uploaded_at && `загружен ${fmtDate(d.uploaded_at)}`].filter(Boolean).join(", ");
  const file = String(d.source || "").replace(/^загружен через интерфейс:\s*/, "файл ");
  const src = d.source && /^https?:/.test(d.source) ? `<a href="${esc(d.source)}" target="_blank" rel="noopener">источник</a>` : file ? esc(file) : "";
  return `<li class="n-row" data-id="${esc(d.id)}">
    <div class="n-main"><b>${esc(d.document || d.id)}</b>${d.title ? `<span class="muted"> ${esc(d.title)}</span>` : ""}
      <p class="muted small">${esc(meta)}${src ? `, ${src}` : ""}</p></div>
    <div class="n-act"><button type="button" class="mini" data-open="${esc(d.id)}">Текст</button>${d.deletable ? `<button type="button" class="mini danger" data-del="${esc(d.id)}" title="Удалить">${ICON_TRASH}Удалить</button>` : ""}</div>
  </li>`;
}

function renderNormDocs(nv) {
  const d = nv.data;
  const q = nv.filter.trim().toLowerCase();
  const docs = (d.documents || []).filter(x => !q || [x.document, x.title, x.status, x.edition, x.customer].join(" ").toLowerCase().includes(q));
  const order = ["internal", "customer_practice", "edition", "corpus"];
  document.getElementById("n-docs").innerHTML = order.map(k => {
    const list = docs.filter(x => x.kind === k);
    if (!list.length && k !== "internal") return "";
    return `<section class="n-group"><div class="n-group-head"><h3>${esc(KIND_TITLE[k])} <span class="n">${list.length}</span></h3></div>
      ${list.length ? `<ul class="n-list">${list.map(normRow).join("")}</ul>` : '<p class="muted small empty-inline">Ничего не загружено.</p>'}</section>`;
  }).join("");
}

async function loadNorms(nv) {
  const r = await fetch("/api/norms").catch(() => null);
  if (!r || !r.ok) { document.getElementById("n-docs").innerHTML = '<div class="empty">Список норм не загрузился.</div>'; return; }
  nv.data = await r.json();
  renderNormDocs(nv);
}

async function openNorm(id) {
  const dlg = document.getElementById("n-dialog");
  const body = document.getElementById("n-dialog-body");
  document.getElementById("n-dialog-title").textContent = "Загрузка";
  body.innerHTML = "";
  dlg.showModal();
  const r = await fetch(`/api/norms/${encodeURIComponent(id)}`).catch(() => null);
  if (!r || !r.ok) { body.innerHTML = '<p class="err">Текст не загрузился.</p>'; return; }
  const d = await r.json();
  document.getElementById("n-dialog-title").textContent = d.document || id;
  const blocks = d.blocks_list || (d.text ? [{type: "text", text: d.text}] : []);
  body.innerHTML = `<p class="muted small">${esc([d.title, d.edition, (d.status || "").replace(/\s*\([^)]*\.json\)/g, "")].filter(Boolean).join(", "))}</p>` +
    blocks.slice(0, 600).map(b => `<p class="${b.type === "table" ? "n-table" : ""}">${esc(b.text)}</p>`).join("") +
    (blocks.length > 600 ? `<p class="muted small">Показано 600 пунктов из ${blocks.length}.</p>` : "");
}

async function searchNorms(nv) {
  const q = document.getElementById("n-q").value.trim();
  const box = document.getElementById("n-search-res");
  if (!q) { box.innerHTML = ""; return; }
  box.innerHTML = '<p class="muted small">Поиск</p>';
  const r = await fetch(`/api/norms/search?${new URLSearchParams({q, limit: "8", internal_only: String(document.getElementById("n-internal").checked)})}`).catch(() => null);
  if (!r || !r.ok) { box.innerHTML = '<p class="err small">Поиск не сработал.</p>'; return; }
  const d = await r.json();
  box.innerHTML = (d.items || []).length ? `<ol class="n-hits">${d.items.map(it => `<li><div><b>${esc(it.document)}${it.number ? `, п. ${esc(it.number)}` : ""}</b>${it.internal ? ' <span class="tag plain">внутренний</span>' : ""}</div><p class="small">${esc(it.quote || it.text || "")}</p></li>`).join("")}</ol>` : '<p class="muted small">Ничего не нашлось.</p>';
}

async function uploadNorm(nv, form) {
  const err = document.getElementById("n-up-err");
  const btn = form.querySelector("button[type=submit]");
  err.hidden = true;
  const file = form.querySelector("input[type=file]").files[0];
  if (!file) { err.textContent = "Выберите файл стандарта."; err.hidden = false; return; }
  const fd = new FormData(form);
  btn.disabled = true;
  btn.textContent = "Загрузка";
  try {
    const r = await fetch("/api/norms", {method: "POST", body: fd});
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || d.detail || `ошибка ${r.status}`);
    form.reset();
    document.getElementById("n-up-name").textContent = "Выбрать файл";
    await loadNorms(nv);
  } catch (e) {
    err.textContent = String(e.message || e);
    err.hidden = false;
  } finally {
    btn.disabled = false;
    btn.textContent = "Загрузить";
  }
}

async function deleteNorm(nv, id, btn) {
  if (!confirm("Удалить стандарт из базы? Проверки перестанут на него ссылаться.")) return;
  btn.disabled = true;
  const r = await fetch(`/api/norms/${encodeURIComponent(id)}`, {method: "DELETE"}).catch(() => null);
  if (!r || !r.ok) { btn.disabled = false; alert("Не удалось удалить."); return; }
  await loadNorms(nv);
}

function profileCounts(p) {
  const bs = p.by_status || p.statuses || {};
  const pick = k => bs[k] ?? p[k] ?? 0;
  return {draft: pick("draft"), verified: pick("verified"), approved: pick("approved"), total: p.total ?? p.items_count ?? (pick("draft") + pick("verified") + pick("approved"))};
}

async function loadProfiles(nv) {
  const box = document.getElementById("g-list");
  const r = await fetch("/api/gost_profiles").catch(() => null);
  nv.profiles = r && r.ok ? await r.json() : [];
  if (!nv.profiles.length) {
    box.innerHTML = '<div class="empty">Чек-листы ещё не собраны.</div>';
    return;
  }
  box.innerHTML = nv.profiles.map(p => {
    const c = profileCounts(p);
    return `<details class="g-card" data-slug="${esc(p.slug)}">
      <summary><div class="g-head"><b>${esc(p.designation)}</b><span class="muted">${esc(p.title || "")}</span></div>
        <div class="g-counts muted small">пунктов ${c.total}, утверждено ${c.approved}, сверено ${c.verified}, черновиков ${c.draft}</div></summary>
      <div class="g-body"><p class="muted small">Загрузка</p></div>
    </details>`;
  }).join("");
}

function limitText(l) {
  if (!l) return "";
  if (typeof l !== "object") return String(l);
  const u = l.unit ? ` ${l.unit}` : "";
  const n = v => String(v).replace(".", ",").replace(/,0$/, "");
  if (l.op === "min" && l.min != null) return `не менее ${n(l.min)}${u}`;
  if (l.op === "max" && l.max != null) return `не более ${n(l.max)}${u}`;
  if (l.op === "range") return `от ${n(l.min)} до ${n(l.max)}${u}`;
  if (l.op === "eq") return `равно ${n(l.min ?? (l.values || [])[0] ?? "")}${u}`;
  if (l.op === "one_of") return `одно из: ${(l.values || []).join(", ")}${u}`;
  return (l.values || []).join(", ") + u;
}

function itemRow(it, canApprove) {
  const lim = limitText(it.limit);
  return `<li class="g-item">
    ${canApprove ? `<label class="g-check"><input type="checkbox" value="${esc(it.id)}" ${it.status === "approved" ? "disabled" : ""}></label>` : ""}
    <div class="g-item-body">
      <div class="g-item-head"><b>${esc(it.param || it.requirement || it.id)}</b><span class="muted small">${esc([it.kind_title || it.kind, it.clause && `п. ${it.clause}`, it.nature].filter(Boolean).join(", "))}</span>
        <span class="tag ${it.status === "approved" ? "s-rec" : it.status === "verified" ? "s-manual" : "plain"}"><i></i>${esc(it.status_title || it.status || "")}</span></div>
      ${lim ? `<p class="small"><b>${esc(lim)}</b>${it.condition ? ` <span class="muted">при условии: ${esc(it.condition)}</span>` : ""}</p>` : it.condition ? `<p class="small muted">При условии: ${esc(it.condition)}</p>` : ""}
      ${it.requirement && it.requirement !== it.param ? `<p class="small">${esc(it.requirement)}</p>` : ""}
      ${it.quote ? `<blockquote class="quote small">${esc(it.quote)}</blockquote>` : ""}
      ${it.verify_reason ? `<p class="muted small">Сверка: ${esc(it.verify_reason)}</p>` : ""}
      ${it.draft_reason ? `<p class="muted small">Почему черновик: ${esc(it.draft_reason)}</p>` : ""}
      ${it.approved_by ? `<p class="muted small">Утвердил ${esc(it.approved_by)}${it.approved_at ? `, ${esc(fmtDate(it.approved_at))}` : ""}</p>` : ""}
    </div></li>`;
}

async function fillProfile(nv, card) {
  if (card.dataset.ready) return;
  card.dataset.ready = "1";
  const body = card.querySelector(".g-body");
  const r = await fetch(`/api/gost_profiles/${encodeURIComponent(card.dataset.slug)}`).catch(() => null);
  if (!r || !r.ok) { body.innerHTML = '<p class="muted small">Пункты не загрузились.</p>'; return; }
  const p = await r.json();
  const items = p.items || [];
  const check = items.filter(it => it.status !== "draft");
  const drafts = (p.drafts || []).concat(items.filter(it => it.status === "draft"));
  body.innerHTML = `<p class="muted small">${esc([p.edition && (/^редакц/i.test(p.edition) ? p.edition : `редакция ${p.edition}`), p.built_at && `собран ${fmtDate(p.built_at)}`].filter(Boolean).join(", "))}</p>
    <ul class="g-items">${check.map(it => itemRow(it, true)).join("") || '<li class="muted small">Сверенных пунктов нет.</li>'}</ul>
    <div class="g-approve"><button type="button" class="btn-dark sm" data-approve="${esc(p.slug)}">Утвердить выбранные</button><span class="muted small">Замечания по неутверждённым пунктам требуют проверки.</span><p class="err small" hidden></p></div>
    ${drafts.length ? `<details class="io"><summary>Черновики: ${drafts.length}</summary><ul class="g-items">${drafts.map(it => itemRow(it, false)).join("")}</ul></details>` : ""}`;
}

async function approveItems(nv, slug, btn) {
  const card = btn.closest(".g-card");
  const ids = [...card.querySelectorAll(".g-check input:checked")].map(x => x.value);
  const err = btn.parentElement.querySelector(".err");
  err.hidden = true;
  const user = (state.user || "").trim();
  if (!ids.length) { err.textContent = "Отметьте пункты."; err.hidden = false; return; }
  if (!user) { err.textContent = "Укажите ФИО проверяющего на странице проверки."; err.hidden = false; return; }
  btn.disabled = true;
  const r = await fetch(`/api/gost_profiles/${encodeURIComponent(slug)}/approve`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({item_ids: ids, user})}).catch(() => null);
  btn.disabled = false;
  if (!r || !r.ok) { err.textContent = "Не удалось утвердить."; err.hidden = false; return; }
  delete card.dataset.ready;
  await fillProfile(nv, card);
  const st = await fetch("/api/gost_profiles").then(x => x.json()).catch(() => null);
  if (st) nv.profiles = st;
}

async function renderNorms() {
  setNav("norms");
  try { await getMeta(); } catch {}
  const nv = normsView = {data: null, profiles: [], filter: "", tab: (location.hash.split("/")[2] || "docs")};
  view.innerHTML = `<section class="result norms">
    <div class="r-top"><div><h1>Нормативная база</h1></div></div>
    <div class="segmented" role="tablist"><button type="button" data-ntab="docs" aria-pressed="${nv.tab === "docs"}">Документы</button><button type="button" data-ntab="gost" aria-pressed="${nv.tab === "gost"}">Чек-листы ГОСТ</button></div>
    <div id="n-tab-docs" ${nv.tab === "docs" ? "" : "hidden"}>
      <div class="n-tools">
        <form class="card n-upload" id="n-upload">
          <h3>Загрузить внутренний стандарт</h3>
          <label class="file-pick"><input type="file" name="file" accept=".docx,.pdf,.doc,.rtf,.odt,.txt"><span id="n-up-name">Выбрать файл</span></label>
          <label class="text-field"><span class="label">Название</span><input type="text" name="title" placeholder="например, СТО Лифты 07-2026"></label>
          <label class="text-field"><span class="label">Заказчик</span><input type="text" name="customer" placeholder="например, МФТИ"></label>
          <p class="err small" id="n-up-err" hidden></p>
          <button type="submit" class="btn-dark">Загрузить</button>
        </form>
        <div class="card n-search">
          <h3>Найти пункт нормы</h3>
          <form id="n-search-form" class="n-search-row"><label class="search">${ICON_SEARCH}<input type="search" id="n-q" placeholder="например, эксплуатационная документация на русском языке"></label><button type="submit" class="btn-secondary">Найти</button></form>
          <label class="check small"><input type="checkbox" id="n-internal"> только внутренние стандарты</label>
          <div id="n-search-res"></div>
        </div>
      </div>
      <label class="search n-filter">${ICON_SEARCH}<input type="search" id="n-filter" placeholder="Фильтр по названию, статусу, заказчику"></label>
      <div id="n-docs"><p class="muted">Загрузка</p></div>
    </div>
    <div id="n-tab-gost" ${nv.tab === "gost" ? "" : "hidden"}>
      <div id="g-list"><p class="muted">Загрузка</p></div>
    </div>
    <dialog id="n-dialog" class="n-dialog"><div class="n-dialog-head"><h3 id="n-dialog-title"></h3><button type="button" class="icon-btn" id="n-dialog-close" aria-label="Закрыть"><svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg></button></div><div id="n-dialog-body" class="n-dialog-body"></div></dialog>
  </section>`;
  view.querySelector(".segmented").addEventListener("click", e => {
    const b = e.target.closest("[data-ntab]");
    if (!b) return;
    nv.tab = b.dataset.ntab;
    view.querySelectorAll("[data-ntab]").forEach(x => x.setAttribute("aria-pressed", String(x === b)));
    document.getElementById("n-tab-docs").hidden = nv.tab !== "docs";
    document.getElementById("n-tab-gost").hidden = nv.tab !== "gost";
    history.replaceState(null, "", `#/norms/${nv.tab}`);
  });
  const up = document.getElementById("n-upload");
  up.querySelector("input[type=file]").addEventListener("change", e => { document.getElementById("n-up-name").textContent = e.target.files[0] ? e.target.files[0].name : "Выбрать файл"; });
  up.addEventListener("submit", e => { e.preventDefault(); uploadNorm(nv, up); });
  document.getElementById("n-search-form").addEventListener("submit", e => { e.preventDefault(); searchNorms(nv); });
  document.getElementById("n-filter").addEventListener("input", e => { nv.filter = e.target.value; if (nv.data) renderNormDocs(nv); });
  document.getElementById("n-docs").addEventListener("click", e => {
    const o = e.target.closest("[data-open]");
    if (o) return openNorm(o.dataset.open);
    const d = e.target.closest("[data-del]");
    if (d) deleteNorm(nv, d.dataset.del, d);
  });
  document.getElementById("n-dialog-close").addEventListener("click", () => document.getElementById("n-dialog").close());
  document.getElementById("g-list").addEventListener("toggle", e => { if (e.target.matches && e.target.matches(".g-card") && e.target.open) fillProfile(nv, e.target); }, true);
  document.getElementById("g-list").addEventListener("click", e => { const b = e.target.closest("[data-approve]"); if (b) approveItems(nv, b.dataset.approve, b); });
  loadNorms(nv);
  loadProfiles(nv);
}

function renderStub(route, title) {
  setNav(route);
  view.innerHTML = `<div class="page"><h1>${esc(title)}</h1><p class="muted">Экран собирается.</p></div>`;
}

function route() {
  const h = location.hash || "#/";
  if (h === "#/" || h === "#") return renderStart();
  if (!h.startsWith("#/job/")) stopJob();
  if (h.startsWith("#/job/")) return renderJob(decodeURIComponent(h.slice(6)));
  if (h.startsWith("#/run/")) return renderRun(decodeURIComponent(h.slice(6)));
  if (h.startsWith("#/history")) return renderHistory();
  if (h.startsWith("#/norms")) return renderNorms();
  return renderStart();
}

document.getElementById("menu-btn").addEventListener("click", () => {
  const nav = document.getElementById("nav");
  const open = !nav.classList.contains("open");
  nav.classList.toggle("open", open);
  document.getElementById("menu-btn").setAttribute("aria-expanded", String(open));
});
window.addEventListener("hashchange", route);
route();
