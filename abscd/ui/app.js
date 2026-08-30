/* Frontend logic. All communication with Python goes through
   window.pywebview.api (JS -> Python) and window.onEvent (Python -> JS). */

"use strict";

const $ = (id) => document.getElementById(id);

let logLines = [];
let discs = {};       // number -> {state, tracks}
let ripActive = false;

/* ---------- helpers ---------- */

function show(id, visible) { $(id).hidden = !visible; }

function setStage(text) { $("stage").textContent = text; }

function setProgress(done, total) {
  const pct = total > 1 ? (100 * done / total) : 0;
  $("bar").style.width = pct.toFixed(1) + "%";
  $("percent").textContent = total > 1 ? pct.toFixed(1) + " %" : "";
}

function addLog(text) {
  logLines.push(text);
  $("log").textContent = logLines.slice(-12).join("\n");
}

function renderDiscs() {
  const ul = $("disc-list");
  ul.innerHTML = "";
  Object.keys(discs).map(Number).sort((a, b) => a - b).forEach((n) => {
    const d = discs[n];
    const li = document.createElement("li");
    let text = "Disc " + n + ": ";
    if (d.state === "ripping") text += "ripping…";
    else if (d.state === "done") text += "ripped — " + d.tracks + " chapters";
    else text += d.state;
    li.textContent = text;
    ul.appendChild(li);
  });
}

/* ---------- events pushed from Python ---------- */

window.onEvent = function (ev) {
  switch (ev.kind) {
    case "stage":
      setStage(ev.text);
      break;
    case "progress":
      setProgress(ev.done, ev.total);
      break;
    case "log":
      addLog(ev.text);
      break;
    case "disc_status":
      discs[ev.number] = { state: ev.state, tracks: ev.tracks || 0 };
      renderDiscs();
      break;
    case "ask_disc":
      setStage("Disc ejected. Insert Disc " + ev.next_number +
               ", or assemble the book.");
      setProgress(0, 1);
      $("next-btn").textContent = "Rip Disc " + ev.next_number;
      show("disc-buttons", true);
      break;
    case "assembling":
      show("disc-buttons", false);
      break;
    case "finished":
      ripActive = false;
      setStage("Done! Your audiobook is ready.");
      setProgress(1, 1);
      addLog("Saved: " + ev.path);
      show("disc-buttons", false);
      show("cancel-btn", false);
      show("again-btn", true);
      break;
    case "aborted":
      ripActive = false;
      setStage(ev.text);
      show("disc-buttons", false);
      show("cancel-btn", false);
      show("again-btn", true);
      break;
  }
};

/* ---------- user actions (JS -> Python) ---------- */

function startRip() {
  const author = $("author").value.trim();
  const title = $("title").value.trim();
  const year = $("year").value.trim();
  const drive = $("drive").value;
  window.pywebview.api.start_job(author, title, year, drive).then((res) => {
    if (!res.ok) {
      $("form-error").textContent = res.error;
      show("form-error", true);
      return;
    }
    show("form-error", false);
    logLines = [];
    discs = {};
    ripActive = true;
    $("log").textContent = "";
    renderDiscs();
    $("book-heading").textContent = title + " — " + author + " (" + year + ")";
    show("screen-form", false);
    show("screen-progress", true);
    show("again-btn", false);
    show("cancel-btn", true);
    show("disc-buttons", false);
  });
}

function backToForm() {
  ["author", "title", "year"].forEach((id) => { $(id).value = ""; });
  show("screen-progress", false);
  show("screen-form", true);
  $("author").focus();
}

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

function init() {
  window.pywebview.api.get_init().then((info) => {
    const sel = $("drive");
    sel.innerHTML = "";
    info.drives.forEach((d) => {
      const opt = document.createElement("option");
      opt.value = d;
      opt.textContent = d + ":";
      sel.appendChild(opt);
    });
    if (info.drives.length === 0) {
      $("form-error").textContent = "No optical drive was found on this computer.";
      show("form-error", true);
    }
    applyOutputStatus(info);
  });

  $("change-output-btn").addEventListener("click", () => {
    window.pywebview.api.choose_output_folder().then(applyOutputStatus);
  });

  $("start-btn").addEventListener("click", startRip);
  $("next-btn").addEventListener("click", () => {
    show("disc-buttons", false);
    window.pywebview.api.answer("next");
  });
  $("assemble-btn").addEventListener("click", () => {
    show("disc-buttons", false);
    window.pywebview.api.answer("assemble");
  });
  $("cancel-btn").addEventListener("click", () => {
    if (!ripActive) { backToForm(); return; }
    show("confirm-cancel", true);
  });
  $("confirm-yes").addEventListener("click", () => {
    show("confirm-cancel", false);
    window.pywebview.api.answer("cancel");
  });
  $("confirm-no").addEventListener("click", () => show("confirm-cancel", false));
  $("again-btn").addEventListener("click", backToForm);

  document.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !$("screen-form").hidden
        && !$("start-btn").disabled) startRip();
  });
}

window.addEventListener("pywebviewready", init);
