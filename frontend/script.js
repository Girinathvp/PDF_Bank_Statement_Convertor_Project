// ---------------------------------------------------------------------------
// Elements
// ---------------------------------------------------------------------------
const $ = (id) => document.getElementById(id);
const bankGrid = $("bankGrid");
const betaGrid = $("betaGrid");
const betaWrap = $("betaWrap");
const bankSelectedName = $("bankSelectedName");
const detectNotice = $("detectNotice");
const dropzone = $("dropzone");
const fileInput = $("fileInput");
const fileInfo = $("fileInfo");
const fileName = $("fileName");
const fileMeta = $("fileMeta");
const convertBtn = $("convertBtn");
const mergeToggle = $("mergeToggle");
const progressSection = $("progressSection");
const etaText = $("etaText");
const progressSub = $("progressSub");
const pctText = $("pctText");
const ledger = $("ledger");
const stagesEl = $("stages");
const tipsRoll = $("tipsRoll");
const resultSection = $("resultSection");
const resultTime = $("resultTime");
const figures = $("figures");
const checksEl = $("checks");
const warningsEl = $("warnings");
const downloadLink = $("downloadLink");
const againBtn = $("againBtn");
const errorBox = $("errorBox");

// Used only if /api/banks can't be reached.
const FALLBACK_BANKS = [
  { id: "sib", name: "South Indian Bank", short: "SIB", status: "verified" },
  { id: "sbi", name: "State Bank of India", short: "SBI", status: "verified" },
  { id: "federal", name: "Federal Bank", short: "Federal", status: "verified" },
  { id: "csb", name: "CSB Bank", short: "CSB", status: "verified" },
  { id: "other", name: "Other bank (auto-detect layout)", short: "Other", status: "generic" },
];

const TIPS = [
  "Amber rows in the Excel file are the ones worth a second look.",
  "Totals are checked against the figures printed on the statement.",
  "Scanned statements take longer: each page is read with OCR.",
  "Wrong bank picked? The layout is still detected, and you'll see a warning.",
  "Bank not listed? “Other bank” works out the columns from the PDF itself.",
  "Turn off “one sheet” to keep each PDF page on its own Excel tab.",
  "Narration that wraps across pages is joined back to its transaction.",
];

const STAGE_ORDER = ["reading", "extracting", "writing", "validating", "done"];
const STAGE_TEXT = {
  reading: "Reading the PDF",
  extracting: "Extracting transactions",
  writing: "Building the Excel file",
  validating: "Checking totals against the statement",
};

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let banks = [];
let selectedBank = "other";
let userPickedBank = false;
let upload = null;          // response of /api/upload
let currentJobId = null;
let pollTimer = null;
let tickTimer = null;
let tipsTimer = null;
let etaModel = null;        // { serverEta, at, shown }
let lastPagesDone = 0;

// ---------------------------------------------------------------------------
// Banks
// ---------------------------------------------------------------------------
async function loadBanks() {
  try {
    const res = await fetch("/api/banks");
    const data = await res.json();
    banks = data.banks && data.banks.length ? data.banks : FALLBACK_BANKS;
  } catch {
    banks = FALLBACK_BANKS;
  }
  renderBanks();
}

function bankById(id) {
  return banks.find((b) => b.id === id) || banks.find((b) => b.id === "other");
}

function bankButton(b) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "bank" + (b.id === "other" ? " bank--other" : "");
  btn.setAttribute("role", "radio");
  btn.dataset.id = b.id;
  const full = b.id === "other" ? "Layout read from the PDF" : b.name;
  const short = b.id === "other" ? "Other bank" : b.short;
  btn.innerHTML = `<span class="bank-short"></span><span class="bank-full"></span>`;
  btn.querySelector(".bank-short").textContent = short;
  btn.querySelector(".bank-full").textContent = full;
  if (b.status === "verified") {
    const flag = document.createElement("span");
    flag.className = "bank-flag";
    flag.textContent = "Verified";
    btn.appendChild(flag);
  }
  btn.addEventListener("click", () => selectBank(b.id, true));
  return btn;
}

function renderBanks() {
  bankGrid.innerHTML = "";
  betaGrid.innerHTML = "";
  banks.filter((b) => b.status !== "beta").forEach((b) => bankGrid.appendChild(bankButton(b)));
  banks.filter((b) => b.status === "beta").forEach((b) => betaGrid.appendChild(bankButton(b)));
  if (!betaGrid.children.length) betaWrap.classList.add("hidden");
  selectBank(selectedBank, false);
}

function selectBank(id, byUser) {
  selectedBank = id;
  if (byUser) userPickedBank = true;
  document.querySelectorAll(".bank").forEach((el) => {
    const on = el.dataset.id === id;
    el.setAttribute("aria-checked", on ? "true" : "false");
    el.tabIndex = on ? 0 : -1;
  });
  const b = bankById(id);
  bankSelectedName.textContent = b.id === "other" ? "Other bank" : b.name;
  if (b.status === "beta") betaWrap.open = true;
  updateDetectNotice();
}

// Arrow keys move between bank options (radio-group behaviour).
document.addEventListener("keydown", (e) => {
  const el = document.activeElement;
  if (!el || !el.classList || !el.classList.contains("bank")) return;
  if (!["ArrowRight", "ArrowDown", "ArrowLeft", "ArrowUp"].includes(e.key)) return;
  e.preventDefault();
  const all = [...document.querySelectorAll(".bank")].filter((b) => b.offsetParent !== null);
  const i = all.indexOf(el);
  const next = all[(i + (e.key === "ArrowRight" || e.key === "ArrowDown" ? 1 : -1) + all.length) % all.length];
  next.focus();
  selectBank(next.dataset.id, true);
});

function updateDetectNotice() {
  detectNotice.className = "notice hidden";
  detectNotice.innerHTML = "";
  if (!upload) return;

  if (upload.is_scanned && !upload.detected_bank) {
    detectNotice.className = "notice";
    detectNotice.textContent =
      "This is a scanned statement, so the bank can't be read automatically. Make sure the right bank is selected.";
    return;
  }
  if (!upload.detected_bank) return;

  const det = bankById(upload.detected_bank);
  if (selectedBank === upload.detected_bank) {
    detectNotice.className = "notice ok";
    detectNotice.textContent = `Matches the statement: ${det.name} (found ${upload.detect_reason}).`;
  } else {
    detectNotice.className = "notice";
    const span = document.createElement("span");
    span.textContent = `This statement looks like ${det.name} (found ${upload.detect_reason}).`;
    const sw = document.createElement("button");
    sw.type = "button";
    sw.className = "link-btn";
    sw.textContent = `Use ${det.short}`;
    sw.addEventListener("click", () => selectBank(det.id, true));
    detectNotice.append(span, sw);
  }
}

// ---------------------------------------------------------------------------
// Upload
// ---------------------------------------------------------------------------
dropzone.addEventListener("click", () => fileInput.click());
dropzone.addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); }
});
dropzone.addEventListener("dragover", (e) => { e.preventDefault(); dropzone.classList.add("dragover"); });
dropzone.addEventListener("dragleave", () => dropzone.classList.remove("dragover"));
dropzone.addEventListener("drop", (e) => {
  e.preventDefault();
  dropzone.classList.remove("dragover");
  if (e.dataTransfer.files.length) handleFileSelected(e.dataTransfer.files[0]);
});
fileInput.addEventListener("change", () => {
  if (fileInput.files.length) handleFileSelected(fileInput.files[0]);
});

function fmtSeconds(s) {
  s = Math.max(0, Math.round(s));
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60), r = s % 60;
  return r ? `${m} min ${r} s` : `${m} min`;
}

async function handleFileSelected(file) {
  resetRun();
  upload = null;
  updateDetectNotice();
  const isPdf = file.type === "application/pdf" || /\.pdf$/i.test(file.name);
  if (!isPdf) { showError("That file isn't a PDF. Choose the statement PDF downloaded from your bank."); return; }

  fileInfo.classList.remove("hidden");
  fileName.textContent = file.name;
  fileMeta.textContent = "Uploading…";

  const formData = new FormData();
  formData.append("file", file);
  try {
    const res = await fetch("/api/upload", { method: "POST", body: formData });
    const data = await res.json();
    if (!res.ok) { fileMeta.textContent = ""; showError(data.error || "Upload failed. Try the file again."); return; }
    upload = data;
    currentJobId = data.job_id;
    const kind = data.is_scanned ? "Scanned statement (OCR)" : "Digital statement";
    fileMeta.textContent = `${data.num_pages} page${data.num_pages === 1 ? "" : "s"}, ${kind.toLowerCase()}, about ${fmtSeconds(data.estimated_seconds)} to convert`;
    if (data.detected_bank && !userPickedBank) selectBank(data.detected_bank, false);
    else updateDetectNotice();
    convertBtn.disabled = false;
  } catch {
    fileMeta.textContent = "";
    showError("Couldn't reach the converter. Check that the app is still running and try again.");
  }
}

// ---------------------------------------------------------------------------
// Convert + progress
// ---------------------------------------------------------------------------
convertBtn.addEventListener("click", async () => {
  if (!currentJobId) return;
  convertBtn.disabled = true;
  convertBtn.textContent = "Converting…";
  resultSection.classList.add("hidden");
  errorBox.classList.add("hidden");
  startProgressUI(upload ? upload.num_pages : 0, upload ? upload.estimated_seconds : null);

  try {
    const res = await fetch(`/api/process/${currentJobId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ merge_pages: mergeToggle.checked, bank: selectedBank }),
    });
    const data = await res.json();
    if (!res.ok) { failRun(data.error || "Couldn't start the conversion."); return; }
    pollTimer = setInterval(pollStatus, 1000);
    pollStatus();
  } catch {
    failRun("Couldn't reach the converter. Check that the app is still running and try again.");
  }
});

function buildLedger(totalPages) {
  ledger.innerHTML = "";
  ledger.classList.remove("complete");
  const n = Math.max(1, Math.min(totalPages || 1, 60));
  for (let i = 0; i < n; i++) {
    const t = document.createElement("div");
    t.className = "tick";
    ledger.appendChild(t);
  }
}

function paintLedger(done, total, finished) {
  const ticks = ledger.children;
  const n = ticks.length;
  const frac = total ? done / total : 0;
  const filled = finished ? n : Math.floor(frac * n);
  for (let i = 0; i < n; i++) {
    ticks[i].className = "tick" + (i < filled ? " on" : (i === filled && !finished ? " now" : ""));
  }
  if (finished) ledger.classList.add("complete");
  const pct = finished ? 100 : Math.min(99, Math.round(frac * 100));
  pctText.textContent = `${pct}%`;
  ledger.setAttribute("aria-valuenow", String(pct));
}

function paintStages(stage) {
  const idx = STAGE_ORDER.indexOf(stage);
  [...stagesEl.children].forEach((li) => {
    const i = STAGE_ORDER.indexOf(li.dataset.stage);
    li.className = i < idx || stage === "done" ? "done" : i === idx ? "active" : "";
  });
}

function startProgressUI(totalPages, estimate) {
  progressSection.classList.remove("hidden");
  buildLedger(totalPages);
  paintLedger(0, totalPages, false);
  paintStages("reading");
  etaText.textContent = estimate ? `About ${fmtSeconds(estimate)} left` : "Estimating…";
  progressSub.textContent = "Reading the PDF";
  lastPagesDone = 0;
  etaModel = estimate ? { serverEta: estimate, at: performance.now(), shown: estimate } : null;
  startTips();
  clearInterval(tickTimer);
  tickTimer = setInterval(tickEta, 250);
}

// Smooth countdown between polls; never jumps up for small revisions.
function tickEta() {
  if (!etaModel) return;
  const projected = etaModel.serverEta - (performance.now() - etaModel.at) / 1000;
  let shown = Math.min(etaModel.shown, projected);
  if (projected > etaModel.shown * 1.25 + 2) shown = projected; // real slowdown: be honest
  etaModel.shown = Math.max(1, shown);
  etaText.textContent = etaModel.shown <= 1.5 ? "Almost done" : `About ${fmtSeconds(etaModel.shown)} left`;
}

async function pollStatus() {
  try {
    const res = await fetch(`/api/status/${currentJobId}`);
    const data = await res.json();
    if (!res.ok) { failRun(data.error || "Lost track of this conversion."); return; }

    if (data.status === "processing" || data.status === "uploaded") {
      const stage = data.stage || "reading";
      paintStages(stage);
      paintLedger(data.pages_done, data.total_pages, false);
      if (data.eta_seconds != null) {
        const prevShown = etaModel ? etaModel.shown : data.eta_seconds;
        etaModel = { serverEta: data.eta_seconds, at: performance.now(), shown: prevShown };
        tickEta();
      }
      const detail = stage === "extracting"
        ? `Page ${data.pages_done} of ${data.total_pages}`
        : STAGE_TEXT[stage] || "Working";
      progressSub.textContent = `${detail}, ${fmtSeconds(data.elapsed_seconds || 0)} elapsed`;
      lastPagesDone = data.pages_done;
    } else if (data.status === "done") {
      stopTimers();
      paintStages("done");
      paintLedger(data.total_pages, data.total_pages, true);
      etaText.textContent = "Done";
      progressSub.textContent = `${data.total_pages} pages in ${fmtSeconds(data.elapsed_seconds || 0)}`;
      convertBtn.textContent = "Convert again";
      convertBtn.disabled = false;
      showResult(data.result || {});
    } else if (data.status === "error") {
      failRun(data.error || "The conversion stopped with an error.");
    }
  } catch {
    failRun("Lost connection to the converter while checking progress.");
  }
}

// ---------------------------------------------------------------------------
// Rolling tips
// ---------------------------------------------------------------------------
function startTips() {
  clearInterval(tipsTimer);
  const start = Math.floor(Math.random() * TIPS.length);
  const order = TIPS.map((_, i) => TIPS[(start + i) % TIPS.length]);
  tipsRoll.innerHTML = "";
  [...order, order[0]].forEach((t) => {
    const d = document.createElement("div");
    d.textContent = t;
    tipsRoll.appendChild(d);
  });
  let i = 0;
  tipsRoll.style.transition = "none";
  tipsRoll.style.transform = "translateY(0)";
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  tipsTimer = setInterval(() => {
    i += 1;
    tipsRoll.style.transition = reduce ? "none" : "";
    tipsRoll.style.transform = `translateY(-${i * 1.55}em)`;
    if (i === order.length) {
      setTimeout(() => {
        tipsRoll.style.transition = "none";
        tipsRoll.style.transform = "translateY(0)";
        i = 0;
      }, reduce ? 0 : 650);
    }
  }, 4500);
}

// ---------------------------------------------------------------------------
// Result
// ---------------------------------------------------------------------------
const inr = (v) => (v == null ? "–" : "₹ " + Number(v).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 }));

function figure(label, value, warn) {
  const d = document.createElement("div");
  d.className = "fig";
  d.innerHTML = `<div class="fig-label"></div><div class="fig-value${warn ? " warn" : ""}"></div>`;
  d.querySelector(".fig-label").textContent = label;
  d.querySelector(".fig-value").textContent = value;
  return d;
}

function showResult(r) {
  resultSection.classList.remove("hidden");
  resultTime.textContent = r.bank ? `${r.bank.name}${r.ocr_pages ? `, ${r.ocr_pages} pages read with OCR` : ""}` : "";

  figures.innerHTML = "";
  figures.append(
    figure("Transactions", (r.rows || 0).toLocaleString("en-IN")),
    figure("Total debits", inr(r.debits)),
    figure("Total credits", inr(r.credits)),
    figure("Closing balance", inr(r.closing)),
  );

  checksEl.innerHTML = "";
  const checks = r.checks || [];
  if (!checks.length) {
    const li = document.createElement("li");
    li.className = "none";
    li.textContent = "No printed totals were found on this statement to compare against. Spot-check the first and last rows.";
    checksEl.appendChild(li);
  }
  checks.forEach((c) => {
    const li = document.createElement("li");
    li.className = c.ok ? "ok" : "bad";
    const diff = c.extracted - c.printed;
    li.innerHTML = `<span class="mark"></span><span class="what"></span><span class="amt"></span>`;
    li.querySelector(".mark").textContent = c.ok ? "✓" : "✕";
    li.querySelector(".what").textContent = c.ok
      ? `${c.label} match the statement`.replace("balance match", "balance matches")
      : `${c.label} differ from the statement`.replace("balance differ", "balance differs");
    li.querySelector(".amt").textContent = c.ok ? inr(c.printed) : `${inr(c.extracted)} vs ${inr(c.printed)} (off by ${inr(Math.abs(diff))})`;
    checksEl.appendChild(li);
  });

  const review = r.needs_review || 0;
  const rv = document.createElement("li");
  rv.className = review ? "flag" : "ok";
  rv.innerHTML = `<span class="mark"></span><span class="what"></span><span class="amt"></span>`;
  rv.querySelector(".mark").textContent = review ? "!" : "✓";
  rv.querySelector(".what").textContent = review
    ? `${review} row${review === 1 ? "" : "s"} highlighted in amber for a manual check`
    : "Every row was read with confidence";
  checksEl.appendChild(rv);

  warningsEl.innerHTML = "";
  (r.warnings || []).forEach((w) => {
    const li = document.createElement("li");
    li.textContent = w;
    warningsEl.appendChild(li);
  });

  const name = (upload && upload.download_name) || "converted_statement.xlsx";
  downloadLink.textContent = "Download Excel";
  if (window.pywebview) {
    downloadLink.removeAttribute("href");
    downloadLink.onclick = async (e) => {
      e.preventDefault();
      downloadLink.textContent = "Saving…";
      try {
        const out = await window.pywebview.api.save_excel(currentJobId, name);
        if (out.ok) downloadLink.textContent = "Saved. Save another copy";
        else if (out.cancelled) downloadLink.textContent = "Download Excel";
        else { showError(out.error || "Couldn't save the file."); downloadLink.textContent = "Download Excel"; }
      } catch {
        showError("Couldn't save the file.");
        downloadLink.textContent = "Download Excel";
      }
    };
  } else {
    downloadLink.onclick = null;
    downloadLink.href = `/api/download/${currentJobId}`;
    downloadLink.setAttribute("download", name);
  }
  resultSection.scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
}

againBtn.addEventListener("click", () => {
  resetRun();
  upload = null;
  currentJobId = null;
  fileInput.value = "";
  fileInfo.classList.add("hidden");
  convertBtn.disabled = true;
  updateDetectNotice();
  window.scrollTo({ top: 0 });
  dropzone.focus();
});

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function stopTimers() {
  clearInterval(pollTimer);
  clearInterval(tickTimer);
  clearInterval(tipsTimer);
  pollTimer = tickTimer = tipsTimer = null;
}

function failRun(msg) {
  stopTimers();
  progressSection.classList.add("hidden");
  convertBtn.textContent = "Convert to Excel";
  convertBtn.disabled = !currentJobId;
  showError(msg);
}

function resetRun() {
  stopTimers();
  convertBtn.disabled = true;
  convertBtn.textContent = "Convert to Excel";
  progressSection.classList.add("hidden");
  resultSection.classList.add("hidden");
  errorBox.classList.add("hidden");
  downloadLink.onclick = null;
  downloadLink.removeAttribute("href");
}

function showError(msg) {
  errorBox.textContent = msg;
  errorBox.classList.remove("hidden");
}

loadBanks();
