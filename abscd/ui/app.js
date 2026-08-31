/* Audiobook Bob frontend. Same event protocol as before:
   Python -> JS: window.onEvent({kind, ...})   (evaluate_js push)
   JS -> Python: window.pywebview.api.*        (js_api bridge)      */

"use strict";

const $ = (id) => document.getElementById(id);
const show = (id, visible) => { $(id).hidden = !visible; };

/* Quality tier labels shown in the pixel dropdown (measured MB/hour). */
const QUALITY_ITEMS = [
  { value: "good",   label: "Good — 48 kbps mono (~23 MB per hour)" },
  { value: "better", label: "Better — 64 kbps mono (~30 MB per hour)" },
  { value: "best",   label: "Best — 128 kbps stereo (~59 MB per hour)" },
];

const state = {
  screen: "setup",        // setup | ripping | assembling
  ripActive: false,
  discs: {},              // number -> {state, tracks}
  elapsedTimer: null,
  elapsedStart: 0,
  results: [],            // metadata lookup results
  carPos: 0,              // carousel position
  carSelected: null,      // selected result index (null = none)
  coverData: null,        // data URI of the chosen cover, for the asm scene
};

/* ---------------- custom pixel dropdowns ---------------- */

function makeDropdown(rootId, onChange) {
  const root = $(rootId), val = $(rootId + "-val"), list = $(rootId + "-list");
  const dd = { items: [], value: null };

  function render() {
    list.innerHTML = "";
    dd.items.forEach((item) => {
      const el = document.createElement("div");
      el.className = "opt" + (item.value === dd.value ? " sel" : "");
      el.textContent = item.label;
      el.addEventListener("mousedown", (e) => {   // mousedown beats focusout
        e.preventDefault();
        dd.set(item.value);
        close();
        if (onChange) onChange(item.value);
      });
      list.appendChild(el);
    });
  }
  function open() { if (dd.items.length) { render(); show(rootId + "-list", true); root.classList.add("open"); } }
  function close() { show(rootId + "-list", false); root.classList.remove("open"); }
  const isOpen = () => !list.hidden;

  dd.set = (value) => {
    const item = dd.items.find((i) => i.value === value);
    dd.value = item ? item.value : null;
    val.textContent = item ? item.label : "";
  };
  dd.setItems = (items, value) => { dd.items = items; dd.set(value); };

  root.addEventListener("click", (e) => {
    if (e.target.closest(".ddlist")) return;
    isOpen() ? close() : open();
  });
  root.addEventListener("blur", close);
  root.addEventListener("keydown", (e) => {
    const idx = dd.items.findIndex((i) => i.value === dd.value);
    if (e.key === "Enter" || e.key === " ") { isOpen() ? close() : open(); e.preventDefault(); }
    else if (e.key === "Escape") close();
    else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      const next = idx + (e.key === "ArrowDown" ? 1 : -1);
      if (next >= 0 && next < dd.items.length) {
        dd.set(dd.items[next].value);
        if (isOpen()) render();
        if (onChange) onChange(dd.value);
      }
      e.preventDefault();
    }
  });
  return dd;
}

let ddDrive, ddQuality;

/* ---------------- screens & status bar ---------------- */

const TITLES = { setup: "Audiobook Bob — Setup", ripping: "Audiobook Bob — Ripping",
                 assembling: "Audiobook Bob — Assembling" };

function toScreen(name) {
  state.screen = name;
  ["setup", "ripping", "assembling"].forEach((s) => show("screen-" + s, s === name));
  $("tbar-title").textContent = TITLES[name];
}

function setStatus(text) { $("st-main").textContent = text; }
function setMid(html) { $("st-mid").innerHTML = html; }

function startElapsed() {
  stopElapsed();
  state.elapsedStart = Date.now();
  show("st-elapsed", true);
  const tick = () => {
    const s = Math.floor((Date.now() - state.elapsedStart) / 1000);
    $("st-elapsed").textContent =
      "Elapsed " + Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
  };
  tick();
  state.elapsedTimer = setInterval(tick, 1000);
}
function stopElapsed() {
  if (state.elapsedTimer) clearInterval(state.elapsedTimer);
  state.elapsedTimer = null;
}

/* ---------------- progress ---------------- */

function setBar(fillId, labelId, done, total) {
  const pct = total > 0 ? Math.max(0, Math.min(100, 100 * done / total)) : 0;
  $(fillId).style.width = pct.toFixed(1) + "%";
  $(labelId).textContent = pct.toFixed(0) + "%";
}

/* Assembling scene: pre-floored integers, pure calc() in CSS (no round/mod). */
function setAsmProgress(p) {
  p = Math.max(0, Math.min(1, p));
  const scene = $("asm-anim");
  const row = Math.floor(p * 64);
  scene.style.setProperty("--row", row);
  scene.style.setProperty("--flip", row % 2);
  scene.style.setProperty("--revpx", Math.floor(p * 60));
  scene.style.setProperty("--edgeon", row >= 64 ? 0 : 1);
}

/* ---------------- disc list ---------------- */

function renderDiscs() {
  const box = $("disc-list");
  box.innerHTML = "";
  const nums = Object.keys(state.discs).map(Number).sort((a, b) => a - b);
  nums.forEach((n) => {
    const d = state.discs[n];
    const row = document.createElement("div");
    row.className = "drow";
    if (d.state === "done") {
      row.innerHTML = '<span class="chk">&#10003;</span><span class="num">Disc ' + n +
        "</span><span>" + d.tracks + (d.tracks === 1 ? " chapter" : " chapters") + "</span>";
    } else {
      row.innerHTML = '<span class="chk">&nbsp;</span><span class="num">Disc ' + n +
        "</span><span>ripping&#8230;</span>";
    }
    box.appendChild(row);
  });
  const done = nums.filter((n) => state.discs[n].state === "done").length;
  setMid('<span class="ok">' + done + (done === 1 ? " disc" : " discs") + " done</span>");
  return done;
}

/* ---------------- events pushed from Python ---------------- */

window.onEvent = function (ev) {
  switch (ev.kind) {
    case "stage":
      setStatus(ev.text);
      break;
    case "log":
      setStatus(ev.text);
      break;
    case "progress":
      if (state.screen === "ripping") setBar("rip-fill", "rip-label", ev.done, ev.total);
      else if (state.screen === "assembling") {
        setBar("asm-fill", "asm-label", ev.done, ev.total);
        setAsmProgress(ev.total > 0 ? ev.done / ev.total : 0);
      }
      break;
    case "disc_status":
      state.discs[ev.number] = { state: ev.state, tracks: ev.tracks || 0 };
      if (ev.state === "ripping") $("rip-discnum").textContent = ev.number;
      renderDiscs();
      break;
    case "ask_disc":
      $("rip-discnum").textContent = ev.next_number;
      $("next-btn").textContent = "Rip Disc " + ev.next_number;
      $("next-btn").disabled = false;
      $("assemble-btn").disabled = false;
      setBar("rip-fill", "rip-label", 0, 1);
      setStatus("Disc ejected. Insert Disc " + ev.next_number + ", or assemble the book.");
      break;
    case "assembling": {
      const done = renderDiscs();
      $("asm-title").textContent = $("rip-title").textContent;
      $("asm-disccount").textContent = ev.discs || done;
      $("asm-path").textContent = ev.path || "";
      if (ev.has_cover && state.coverData) {
        document.getElementById("cover-art")
          .setAttribute("href", state.coverData);
      }
      setBar("asm-fill", "asm-label", 0, 1);
      setAsmProgress(0);
      setMid('<span class="ok">All discs ripped</span>');
      toScreen("assembling");
      startElapsed();
      break;
    }
    case "finished":
      state.ripActive = false;
      stopElapsed();
      setBar("asm-fill", "asm-label", 1, 1);
      setAsmProgress(1);
      $("asm-path").textContent = ev.path;
      setStatus("Done! Your audiobook is ready (" + ev.size_mb + " MB).");
      show("asm-cancel-btn", false);
      show("asm-again-btn", true);
      break;
    case "aborted":
      state.ripActive = false;
      stopElapsed();
      setStatus(ev.text);
      if (state.screen === "ripping") {
        $("next-btn").disabled = true;
        $("assemble-btn").disabled = true;
        show("cancel-rip-btn", false);
        show("confirm-rip", false);
        show("again-btn", true);
      } else if (state.screen === "assembling") {
        show("asm-cancel-btn", false);
        show("confirm-asm", false);
        show("asm-again-btn", true);
      }
      break;
  }
};

/* ---------------- metadata lookup carousel ---------------- */

function lookupStatus(text) {
  show("cover-placeholder", false);
  show("carousel", false);
  $("lookup-status").innerHTML = text;
  show("lookup-status", true);
}

function renderCarousel() {
  const r = state.results[state.carPos];
  if (!r) return;
  show("cover-placeholder", false);
  show("lookup-status", false);
  show("carousel", true);

  if (r.thumb) {
    $("car-img").src = r.thumb;
    show("car-img", true);
    show("car-noimg", false);
  } else {
    show("car-img", false);
    show("car-noimg", true);
  }
  $("car-title").textContent = r.title;
  $("car-author").textContent = r.author;
  $("car-year").textContent =
    [r.year, r.edition, r.source].filter(Boolean).join(" — ") || " ";

  const selectedHere = state.carSelected === state.carPos;
  $("car-coverbox").classList.toggle("selected", selectedHere);
  $("car-use").textContent = selectedHere ? "✓ Selected" : "Use this one";
  $("car-use").disabled = selectedHere;
  $("car-none").disabled = state.carSelected === null;

  const dots = $("car-dots");
  dots.innerHTML = "";
  state.results.forEach((_, i) => {
    const d = document.createElement("span");
    d.className = "dot" + (i === state.carPos ? " on" : "")
      + (i === state.carSelected ? " sel" : "");
    d.addEventListener("click", () => { state.carPos = i; renderCarousel(); });
    dots.appendChild(d);
  });
}

function carStep(delta) {
  const n = state.results.length;
  if (!n) return;
  state.carPos = (state.carPos + delta + n) % n;
  renderCarousel();
}

function findBooks() {
  $("find-btn").disabled = true;
  lookupStatus("Searching…");
  window.pywebview.api.search_books($("title").value, $("author").value)
    .then((res) => {
      $("find-btn").disabled = !$("title").value.trim();
      if (!res.ok) { lookupStatus(res.error); return; }
      if (!res.results.length) {
        lookupStatus("No matches found — your typed details will be used.");
        return;
      }
      state.results = res.results;
      state.carPos = 0;
      state.carSelected = null;
      state.coverData = null;
      renderCarousel();
    });
}

function useResult() {
  const idx = state.carPos;
  window.pywebview.api.select_result(idx).then((res) => {
    if (!res.ok) { lookupStatus(res.error); return; }
    $("title").value = res.title || $("title").value;
    $("author").value = res.author || $("author").value;
    // Year recorded is deliberately untouched: provider dates describe an
    // edition's release, not the recording being ripped.
    state.carSelected = idx;
    state.coverData = res.cover_data || null;
    renderCarousel();
  });
}

function clearResult() {
  window.pywebview.api.clear_selection();
  state.carSelected = null;
  state.coverData = null;
  renderCarousel();
}

function resetLookup() {
  state.results = [];
  state.carPos = 0;
  state.carSelected = null;
  state.coverData = null;
  window.pywebview.api.clear_selection();
  show("carousel", false);
  show("lookup-status", false);
  show("cover-placeholder", true);
}

/* ---------------- user actions ---------------- */

function applyOutputStatus(info) {
  $("output-path").textContent = info.output_root;
  if (info.output_ok) {
    show("output-error", false);
  } else {
    $("output-error").textContent =
      info.output_error + " Choose a new folder before ripping.";
    show("output-error", true);
  }
  $("start-btn").disabled = !info.output_ok;
}

function startRip() {
  const author = $("author").value.trim();
  const title = $("title").value.trim();
  const year = $("year").value.trim();
  window.pywebview.api.start_job(author, title, year, ddDrive.value || "").then((res) => {
    if (!res.ok) {
      $("form-error").textContent = res.error;
      show("form-error", true);
      return;
    }
    show("form-error", false);
    state.discs = {};
    state.ripActive = true;
    $("rip-title").textContent = title;
    $("rip-discnum").textContent = "1";
    $("next-btn").disabled = true;
    $("assemble-btn").disabled = true;
    show("cancel-rip-btn", true);
    show("again-btn", false);
    show("confirm-rip", false);
    setBar("rip-fill", "rip-label", 0, 1);
    renderDiscs();
    setMid('<span class="ok">0 discs done</span>');
    toScreen("ripping");
    startElapsed();
  });
}

function backToSetup() {
  ["author", "title", "year"].forEach((id) => { $(id).value = ""; });
  resetLookup();
  $("find-btn").disabled = true;
  stopElapsed();
  show("st-elapsed", false);
  setStatus("Ready");
  $("st-mid").textContent = "No disc in drive";
  show("again-btn", false);
  show("asm-again-btn", false);
  show("cancel-rip-btn", true);
  show("asm-cancel-btn", true);
  toScreen("setup");
  window.pywebview.api.get_init().then((info) => {
    ddDrive.setItems(info.drives.map((d) => ({ value: d, label: d + ":" })),
                     info.drives[0] || null);
    applyOutputStatus(info);
  });
  $("author").focus();
}

/* The monitor scales by --ui-scale; that would leave the pixel scenes at a
   non-integer number of device pixels per scene pixel (uneven nearest-neighbor
   columns). Compensate per scene: pick the largest whole device-pixel scale
   that fits and zoom the svg so scene px x total zoom x devicePixelRatio is an
   integer. Scenes render slightly smaller but perfectly even. */
function snapScenes(uiScale) {
  const dpr = window.devicePixelRatio || 1;
  ["rip-anim", "asm-anim"].forEach((id) => {
    const svg = $(id);
    if (!svg) return;
    const devicePerScenePx = 3 * uiScale * dpr;   // scenes sit at 3x in CSS
    const k = Math.max(1, Math.floor(devicePerScenePx));
    svg.style.zoom = k / devicePerScenePx;
  });
}

function init() {
  ddDrive = makeDropdown("dd-drive", null);
  ddQuality = makeDropdown("dd-quality", (tier) => {
    window.pywebview.api.set_audio_quality(tier);
  });

  window.pywebview.api.get_init().then((info) => {
    ddDrive.setItems(info.drives.map((d) => ({ value: d, label: d + ":" })),
                     info.drives[0] || null);
    ddQuality.setItems(QUALITY_ITEMS, info.audio_quality);
    applyOutputStatus(info);
    snapScenes(info.ui_scale || 1);
    if (info.drives.length === 0) {
      $("form-error").textContent = "No optical drive was found on this computer.";
      show("form-error", true);
    }
  });

  /* title bar */
  $("btn-min").addEventListener("click", () => window.pywebview.api.minimize());
  $("btn-close").addEventListener("click", () => window.pywebview.api.close_window());

  /* screen 1 */
  $("change-output-btn").addEventListener("click", () => {
    window.pywebview.api.choose_output_folder().then(applyOutputStatus);
  });
  $("start-btn").addEventListener("click", startRip);
  $("title").addEventListener("input", () => {
    $("find-btn").disabled = !$("title").value.trim();
  });
  $("find-btn").addEventListener("click", findBooks);
  $("car-prev").addEventListener("click", () => carStep(-1));
  $("car-next").addEventListener("click", () => carStep(1));
  $("car-use").addEventListener("click", useResult);
  $("car-img").addEventListener("click", useResult);
  $("car-none").addEventListener("click", clearResult);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && state.screen === "setup" && !$("start-btn").disabled
        && !e.target.closest(".dd")) startRip();
  });

  /* screen 2 */
  $("next-btn").addEventListener("click", () => {
    $("next-btn").disabled = true;
    $("assemble-btn").disabled = true;
    window.pywebview.api.answer("next");
  });
  $("assemble-btn").addEventListener("click", () => {
    $("next-btn").disabled = true;
    $("assemble-btn").disabled = true;
    window.pywebview.api.answer("assemble");
  });
  $("cancel-rip-btn").addEventListener("click", () => {
    if (!state.ripActive) { backToSetup(); return; }
    show("confirm-rip", true);
  });
  $("confirm-rip-yes").addEventListener("click", () => {
    show("confirm-rip", false);
    window.pywebview.api.answer("cancel");
  });
  $("confirm-rip-no").addEventListener("click", () => show("confirm-rip", false));
  $("again-btn").addEventListener("click", backToSetup);

  /* screen 3 */
  $("asm-cancel-btn").addEventListener("click", () => {
    if (!state.ripActive) { backToSetup(); return; }
    show("confirm-asm", true);
  });
  $("confirm-asm-yes").addEventListener("click", () => {
    show("confirm-asm", false);
    window.pywebview.api.answer("cancel");
  });
  $("confirm-asm-no").addEventListener("click", () => show("confirm-asm", false));
  $("asm-again-btn").addEventListener("click", backToSetup);
}

window.addEventListener("pywebviewready", init);
