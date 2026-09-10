# Building the Windows 11 Desktop App

## Option A (recommended): automated cloud build — no Windows machine needed

A Windows `.exe` can only be built by actually compiling on Windows (Python
bundlers don't cross-compile). Rather than needing your own Windows PC,
this repo includes a GitHub Actions workflow
(`.github/workflows/build-windows-exe.yml`) that builds the real `.exe` on
GitHub's own Windows servers and hands you back a downloadable file.

### One-time setup
1. Create a free GitHub account if you don't have one: https://github.com/join
2. Create a new repository and push this project to it:
   ```bash
   cd bank_statement_converter
   git init
   git add .
   git commit -m "Initial commit"
   git branch -M main
   git remote add origin https://github.com/<your-username>/<your-repo>.git
   git push -u origin main
   ```
   (Replace `<your-username>/<your-repo>` with your actual GitHub repo URL —
   create the empty repo first at https://github.com/new)

### Running the build
The workflow runs automatically on every push to `main`. To trigger it
manually instead:
1. Go to your repo on GitHub → the **Actions** tab
2. Click **"Build Windows Desktop App"** in the left sidebar
3. Click **"Run workflow"** → **"Run workflow"** (green button)
4. Wait for the run to finish (typically 3-6 minutes) — a green checkmark
   means success

### Getting the .exe
1. Click on the finished workflow run
2. Scroll to **"Artifacts"** at the bottom of the page
3. Download **"BankStatementConverter-windows-exe"** — this is a zip
   containing the actual `.exe`
4. Unzip it, and `BankStatementConverter.exe` is ready to share with
   anyone — it has Tesseract OCR bundled in, so the receiver needs
   **nothing else installed**. They just double-click it and it runs.

### If the build fails
Check the failed step's log (click on it to expand). The two most likely
causes:
- **The Tesseract download step fails** — UB-Mannheim changed how they name
  release files. The workflow looks up the latest release automatically
  rather than a hardcoded version, but if their asset naming pattern
  changes, update the `-match` pattern in the workflow's Tesseract step to
  match their current filename (visible at
  https://github.com/UB-Mannheim/tesseract/releases).
- **A pip install step fails** — a pinned dependency version in
  `requirements.txt`/`requirements-windows.txt` may need bumping; check the
  error message for which package.

I built and syntax-validated this workflow, but I can't actually execute a
GitHub Actions Windows runner from here to watch it run end-to-end — so
treat the first run as a real test, not a guaranteed-working black box.

## Option B: build it yourself on a Windows machine

## 1. Prerequisites
- **Python 3.11** — https://www.python.org/downloads/release/python-3119/ (tick "Add python.exe to PATH")
- **Tesseract OCR** — https://github.com/UB-Mannheim/tesseract/wiki
- **WebView2 Runtime** — almost certainly already on Windows 11

## 2. Install dependencies
```powershell
cd "D:\Projects\PDF to Excel\bank_statement_converter\backend"
py -3.11 -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
pip install -r requirements-windows.txt
```

## 3. Test as a plain Python app first
```powershell
python desktop_app.py
```

## 4. Build the exe
```powershell
pyinstaller --noconfirm --onefile --windowed ^
  --name "BankStatementConverter" ^
  --add-data "..\frontend;frontend" ^
  desktop_app.py
```
Output: `backend\dist\BankStatementConverter.exe`

## 5. Zero-install single-click (bundle Tesseract)
```powershell
mkdir tesseract
xcopy "C:\Program Files\Tesseract-OCR\*" "tesseract\" /E /I
pyinstaller --noconfirm --onefile --windowed ^
  --name "BankStatementConverter" ^
  --add-data "..\frontend;frontend" ^
  --add-data "tesseract;tesseract" ^
  desktop_app.py
```

## Troubleshooting
- **"tesseract is not installed or it's not in your PATH"** — see step 1, or bundle per step 5.
- **Window opens blank/white** — install WebView2 Runtime.
- **Download button does nothing** — confirm you're on the latest `desktop_app.py`/`script.js` (native Save-As dialog).
