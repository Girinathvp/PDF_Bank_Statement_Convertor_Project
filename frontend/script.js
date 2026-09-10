const dropzone = document.getElementById("dropzone");
const fileInput = document.getElementById("fileInput");
const fileInfo = document.getElementById("fileInfo");
const convertBtn = document.getElementById("convertBtn");
const mergeToggle = document.getElementById("mergeToggle");
const progressSection = document.getElementById("progressSection");
const progressFill = document.getElementById("progressFill");
const progressLabel = document.getElementById("progressLabel");
const resultSection = document.getElementById("resultSection");
const downloadLink = document.getElementById("downloadLink");
const errorBox = document.getElementById("errorBox");

let currentJobId = null;
let pollTimer = null;

dropzone.addEventListener("click", () => fileInput.click());
dropzone.addEventListener("dragover", (e) => { e.preventDefault(); dropzone.classList.add("dragover"); });
dropzone.addEventListener("dragleave", () => dropzone.classList.remove("dragover"));
dropzone.addEventListener("drop", (e) => {
  e.preventDefault();
  dropzone.classList.remove("dragover");
  if (e.dataTransfer.files.length) {
    fileInput.files = e.dataTransfer.files;
    handleFileSelected(e.dataTransfer.files[0]);
  }
});
fileInput.addEventListener("change", () => {
  if (fileInput.files.length) handleFileSelected(fileInput.files[0]);
});

async function handleFileSelected(file) {
  resetUI();
  if (file.type !== "application/pdf") {
    showError("Please select a PDF file.");
    return;
  }
  fileInfo.textContent = `Uploading "${file.name}"…`;
  fileInfo.classList.remove("hidden");

  const formData = new FormData();
  formData.append("file", file);

  try {
    const res = await fetch("/api/upload", { method: "POST", body: formData });
    const data = await res.json();
    if (!res.ok) { showError(data.error || "Upload failed."); return; }
    currentJobId = data.job_id;
    fileInfo.textContent = `"${file.name}" — ${data.num_pages} page(s). Estimated time: ~${data.estimated_seconds}s`;
    convertBtn.disabled = false;
  } catch (err) {
    showError("Could not reach the server. Please try again.");
  }
}

convertBtn.addEventListener("click", async () => {
  if (!currentJobId) return;
  convertBtn.disabled = true;
  progressSection.classList.remove("hidden");
  resultSection.classList.add("hidden");
  errorBox.classList.add("hidden");
  progressFill.style.width = "0%";
  progressLabel.textContent = "Starting conversion…";

  try {
    const res = await fetch(`/api/process/${currentJobId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ merge_pages: mergeToggle.checked }),
    });
    const data = await res.json();
    if (!res.ok) { showError(data.error || "Could not start processing."); convertBtn.disabled = false; return; }
    pollStatus();
  } catch (err) {
    showError("Could not reach the server. Please try again.");
    convertBtn.disabled = false;
  }
});

function pollStatus() {
  pollTimer = setInterval(async () => {
    try {
      const res = await fetch(`/api/status/${currentJobId}`);
      const data = await res.json();

      if (data.status === "processing" || data.status === "uploaded") {
        const pct = data.total_pages ? Math.round((data.pages_done / data.total_pages) * 100) : 0;
        progressFill.style.width = `${pct}%`;
        progressLabel.textContent = `Processing page ${data.pages_done} of ${data.total_pages} (est. ~${data.estimated_seconds}s total)`;
      } else if (data.status === "done") {
        clearInterval(pollTimer);
        progressFill.style.width = "100%";
        progressLabel.textContent = "Done!";
        resultSection.classList.remove("hidden");
        convertBtn.disabled = false;

        if (window.pywebview) {
          downloadLink.onclick = async (e) => {
            e.preventDefault();
            downloadLink.textContent = "Saving…";
            try {
              const result = await window.pywebview.api.save_excel(currentJobId);
              if (result.ok) {
                downloadLink.textContent = "Saved! Click to save again";
              } else if (result.cancelled) {
                downloadLink.textContent = "Download Excel file";
              } else {
                showError(result.error || "Could not save the file.");
                downloadLink.textContent = "Download Excel file";
              }
            } catch (err) {
              showError("Could not save the file.");
              downloadLink.textContent = "Download Excel file";
            }
          };
        } else {
          downloadLink.href = `/api/download/${currentJobId}`;
        }
      } else if (data.status === "error") {
        clearInterval(pollTimer);
        showError(data.error || "Conversion failed.");
        convertBtn.disabled = false;
      }
    } catch (err) {
      clearInterval(pollTimer);
      showError("Lost connection while checking progress.");
      convertBtn.disabled = false;
    }
  }, 1000);
}

function resetUI() {
  currentJobId = null;
  convertBtn.disabled = true;
  progressSection.classList.add("hidden");
  resultSection.classList.add("hidden");
  errorBox.classList.add("hidden");
  if (pollTimer) clearInterval(pollTimer);
  downloadLink.onclick = null;
  downloadLink.removeAttribute("href");
  downloadLink.textContent = "Download Excel file";
}

function showError(msg) {
  errorBox.textContent = msg;
  errorBox.classList.remove("hidden");
}
