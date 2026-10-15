"""
banks.py - bank profiles, bank auto-detection and statement-totals check.

Design: there is ONE extraction engine (extractor.py). A bank profile is a
small set of hints layered on top of it -- never a separate parser -- so a
fix made for one bank keeps helping every other bank:

  * header_hints   -- words that appear in that bank's column header; they
                      add weight when choosing which line is the header
                      (helps scanned statements where OCR garbles labels).
  * header_expect  -- a few labels that should be in the detected header;
                      if most are missing the UI warns that the layout
                      differs from the bank's usual format.

"verified" banks have been checked end-to-end against a real statement
(totals reconciled): SIB, SBI, Federal, CSB, Axis, Bank of Baroda. "beta" banks run the same generic engine with the
hints below, but have not been validated against a real sample yet.

Auto-detection reads only the PDF's text layer (no OCR), so it costs a
few milliseconds. Scanned PDFs return no detection and rely on the user's
selection.
"""

import re
import pdfplumber

BANKS = [
    # ---- verified against real statements ----
    {"id": "sib", "name": "South Indian Bank", "short": "SIB", "status": "verified",
     "ifsc": ["SIBL"], "names": ["south indian bank"],
     "header_hints": ["particulars", "withdrawals", "deposits", "chq"],
     "header_expect": ["particulars", "withdrawal", "deposit", "balance"]},
    {"id": "sbi", "name": "State Bank of India", "short": "SBI", "status": "verified",
     "ifsc": ["SBIN"], "names": ["state bank of india"],
     "header_hints": ["post", "value", "description", "debit", "credit"],
     "header_expect": ["date", "debit", "credit", "balance"]},
    {"id": "federal", "name": "The Federal Bank", "short": "Federal", "status": "verified",
     "ifsc": ["FDRL"], "names": ["federal bank"],
     "header_hints": ["particulars", "tran", "withdrawals", "deposits"],
     "header_expect": ["particulars", "withdrawal", "deposit", "balance"]},
    {"id": "csb", "name": "CSB Bank (Catholic Syrian)", "short": "CSB", "status": "verified",
     "ifsc": ["CSBK"], "names": ["csb bank", "catholic syrian bank"],
     "header_hints": ["trans", "details", "debits", "credits"],
     "header_expect": ["details", "debit", "credit", "balance"]},
    {"id": "axis", "name": "Axis Bank", "short": "Axis", "status": "verified",
     "ifsc": ["UTIB"], "names": ["axis bank"],
     # "Txn Date | Transaction | Withdrawals | Deposits | Balance | Other
     # Information"; no running balance per row; a Recurring Deposit table
     # may follow the savings table (left out as another account).
     "header_hints": ["txn", "transaction", "withdrawals", "deposits"],
     "header_expect": ["withdrawal", "deposit", "balance"]},
    {"id": "bob", "name": "Bank of Baroda", "short": "BoB", "status": "verified",
     "ifsc": ["BARB"], "names": ["bank of baroda"],
     # "DATE | NARRATION | CHQ.NO. | WITHDRAWAL (DR) | DEPOSIT (CR) | BALANCE"
     "header_hints": ["narration", "withdrawal", "deposit", "chq"],
     "header_expect": ["narration", "withdrawal", "deposit", "balance"]},
    # ---- beta: generic engine + hints, not yet validated on a real sample ----
    {"id": "hdfc", "name": "HDFC Bank", "short": "HDFC", "status": "beta",
     "ifsc": ["HDFC"], "names": ["hdfc bank"],
     "header_hints": ["narration", "withdrawal", "deposit", "closing"],
     "header_expect": ["narration", "withdrawal", "deposit", "balance"]},
    {"id": "icici", "name": "ICICI Bank", "short": "ICICI", "status": "beta",
     "ifsc": ["ICIC"], "names": ["icici bank"],
     "header_hints": ["transaction", "remarks", "withdrawal", "deposit"],
     "header_expect": ["withdrawal", "deposit", "balance"]},
    {"id": "kotak", "name": "Kotak Mahindra Bank", "short": "Kotak", "status": "beta",
     "ifsc": ["KKBK"], "names": ["kotak mahindra"],
     "header_hints": ["description", "withdrawal", "deposit", "ref"],
     "header_expect": ["withdrawal", "deposit", "balance"]},
    {"id": "canara", "name": "Canara Bank", "short": "Canara", "status": "beta",
     "ifsc": ["CNRB"], "names": ["canara bank"],
     "header_hints": ["particulars", "debit", "credit", "value"],
     "header_expect": ["debit", "credit", "balance"]},
    {"id": "union", "name": "Union Bank of India", "short": "Union", "status": "beta",
     "ifsc": ["UBIN"], "names": ["union bank of india"],
     "header_hints": ["remarks", "withdrawals", "deposits", "tran"],
     "header_expect": ["withdrawal", "deposit", "balance"]},
    {"id": "pnb", "name": "Punjab National Bank", "short": "PNB", "status": "beta",
     "ifsc": ["PUNB"], "names": ["punjab national bank"],
     "header_hints": ["narration", "withdrawal", "deposit", "cheque"],
     "header_expect": ["withdrawal", "deposit", "balance"]},
    {"id": "idfc", "name": "IDFC FIRST Bank", "short": "IDFC", "status": "beta",
     "ifsc": ["IDFB"], "names": ["idfc first", "idfc bank"],
     "header_hints": ["particulars", "debit", "credit", "value"],
     "header_expect": ["debit", "credit", "balance"]},
    {"id": "esaf", "name": "ESAF Small Finance Bank", "short": "ESAF", "status": "beta",
     "ifsc": ["ESMF"], "names": ["esaf small finance", "esaf bank"],
     "header_hints": ["particulars", "debit", "credit", "narration"],
     "header_expect": ["debit", "credit", "balance"]},
    {"id": "dhanlaxmi", "name": "Dhanlaxmi Bank", "short": "Dhanlaxmi", "status": "beta",
     "ifsc": ["DLXB"], "names": ["dhanlaxmi bank", "dhanalakshmi bank"],
     "header_hints": ["particulars", "withdrawals", "deposits", "chq"],
     "header_expect": ["withdrawal", "deposit", "balance"]},
    {"id": "kgb", "name": "Kerala Gramin Bank", "short": "KGB", "status": "beta",
     "ifsc": ["KLGB"], "names": ["kerala gramin bank"],
     "header_hints": ["particulars", "debit", "credit", "chq"],
     "header_expect": ["debit", "credit", "balance"]},
]

OTHER_BANK = {"id": "other", "name": "Other bank (auto-detect layout)", "short": "Other",
              "status": "generic", "ifsc": [], "names": [], "header_hints": [], "header_expect": []}

_BY_ID = {b["id"]: b for b in BANKS}
_BY_ID[OTHER_BANK["id"]] = OTHER_BANK


def get_profile(bank_id):
    return _BY_ID.get((bank_id or "").lower(), OTHER_BANK)


def public_bank_list():
    """What the UI needs to render the selector."""
    keys = ("id", "name", "short", "status")
    return [{k: b[k] for k in keys} for b in BANKS] + [{k: OTHER_BANK[k] for k in keys}]


# ---------------------------------------------------------------------------
# Auto-detection (text layer only)
# ---------------------------------------------------------------------------

# "IFS Code: CSBK0000220", "IFSC : SIBL0000123", "IFSC Code - FDRL0001234".
# Only an IFSC printed next to its label counts: statements are full of
# OTHER banks' IFSC prefixes inside UPI narration ("/SBIN/", "/FDRL/").
_IFSC_LABELLED_RE = re.compile(r"\bIFS(?:C)?\s*(?:Code)?\s*[:\-]?\s*([A-Z]{4})0[A-Z0-9]{6}\b", re.IGNORECASE)


def _text_flags(pdf_path, idxs=None):
    """Which pages have a real text layer, via pdfium (milliseconds). Asking
    pdfplumber instead makes pdfminer parse the whole page drawing, which on
    scanned/vector-drawn statements (SIB, SBI) costs seconds per page."""
    from extractor import text_layer_flags
    idxs = None if idxs is None else list(idxs)
    flags = text_layer_flags(pdf_path, pages=idxs)
    if flags is None:
        return None
    return flags if idxs is None else [flags[i] for i in idxs if 0 <= i < len(flags)]


def detect_bank(pdf_path, max_pages=1):
    """
    Returns (bank_id or None, is_scanned, reason). Never runs OCR.
    """
    flags = _text_flags(pdf_path, range(max_pages))
    if flags is not None and not any(flags):
        return None, True, "scanned"
    try:
        with pdfplumber.open(pdf_path) as pdf:
            pages = pdf.pages[:max_pages]
            text = "\n".join((p.extract_text() or "") for p in pages)
    except Exception:
        return None, False, "unreadable"
    if not text.strip():
        return None, True, "scanned"

    for m in _IFSC_LABELLED_RE.finditer(text):
        prefix = m.group(1).upper()
        for b in BANKS:
            if prefix in b["ifsc"]:
                return b["id"], False, f"IFSC {m.group(0).split()[-1]}"

    lower = text.lower()
    best, best_hits = None, 0
    for b in BANKS:
        hits = sum(lower.count(n) for n in b["names"])
        if hits > best_hits:
            best, best_hits = b["id"], hits
    if best:
        return best, False, "bank name on page 1"
    return None, False, "no match"


def likely_scanned(pdf_path, sample_pages=3):
    flags = _text_flags(pdf_path, range(sample_pages))
    return bool(flags) and not any(flags)


def header_matches_profile(header, profile):
    """True/False whether the detected header looks like this bank's usual
    layout; None when there is nothing to compare against."""
    expect = profile.get("header_expect") or []
    if not expect or not header:
        return None
    joined = " ".join(header).lower()
    hits = sum(1 for e in expect if e in joined)
    return hits >= max(1, (len(expect) + 1) // 2)


# ---------------------------------------------------------------------------
# Printed statement totals -> cross-check against what was extracted
# ---------------------------------------------------------------------------

# 1-2 decimals: Federal prints its GRAND TOTAL as "583085.0".
_AMT_TOKEN_RE = re.compile(r"^-?[\d,]+\.\d{1,2}(?:\s*\(?(?:cr|dr)\)?)?$", re.IGNORECASE)

_SUMMARY_LABELS = [
    ("opening", re.compile(r"opening\s+balance|brought\s+forward", re.I)),
    ("debits", re.compile(r"total\s+(?:debit|withdrawal)s?(?:\s+amount)?", re.I)),
    ("credits", re.compile(r"total\s+(?:credit|deposit)s?(?:\s+amount)?", re.I)),
    ("closing", re.compile(r"closing\s+balance", re.I)),
]


def _to_float(tok):
    t = tok.replace(",", "").strip()
    neg = t.startswith("-") or bool(re.search(r"dr\)?$", t, re.I))
    t = re.sub(r"[^\d.]", "", t)
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def _amounts_in(line_text):
    return [t for t in line_text.split() if _AMT_TOKEN_RE.match(t)]


def read_printed_summary(pdf_path, ocr_lines=None):
    """
    Look for a printed summary on the first and last page (text layer only):
      * a label row with a value row under it (CSB: "Opening Balance | Total
        Debit Amount | Total Credit Amount | Closing Balance" / "513.80 | ...")
      * labels with values on the same line ("Closing Balance : 96,794.00 Cr")
      * "GRAND TOTAL <debits> <credits>" (Federal)
      * the same from OCR'd text (`ocr_lines`) for scanned statements, where
        the summary may start on one page and continue on the next (SBI)
    Returns a dict with any of opening/debits/credits/closing.
    """
    found = {}
    texts = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            idxs = sorted({0, len(pdf.pages) - 1})
            flags = _text_flags(pdf_path, idxs)
            flags = dict(zip(idxs, flags)) if flags is not None else None
            # Scanned pages have no text to read here (and asking pdfminer
            # would cost seconds per page), so skip them.
            idxs = [i for i in idxs if flags is None or flags.get(i)]
            texts = [pdf.pages[i].extract_text() or "" for i in idxs]
    except Exception:
        texts = []
    if ocr_lines:
        texts.append("\n".join(ocr_lines))
    for text in texts:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        for i, ln in enumerate(lines):
            labels = []
            for key, rx in _SUMMARY_LABELS:
                m = rx.search(ln)
                if m:
                    labels.append((m.start(), key))
            labels.sort()
            keys = [k for _, k in labels]
            if keys:
                amts = _amounts_in(ln)
                pairs = []
                if len(amts) == len(keys):
                    pairs = zip(keys, amts)
                elif not amts:
                    # Figures on a following line -- possibly after a page
                    # footer when the summary spans two pages (SBI).
                    for nxt in lines[i + 1:i + 4]:
                        if len(_amounts_in(nxt)) == len(keys):
                            pairs = zip(keys, _amounts_in(nxt))
                            break
                for k, a in pairs:
                    v = _to_float(a)
                    if v is not None and k not in found:
                        found[k] = v
            if re.match(r"^grand\s+total\b", ln, re.I):
                amts = _amounts_in(ln)
                if len(amts) >= 2:
                    found.setdefault("debits", _to_float(amts[0]))
                    found.setdefault("credits", _to_float(amts[1]))
                if len(amts) >= 3:
                    found.setdefault("closing", _to_float(amts[2]))
    return found


def compare_totals(printed, computed, tolerance=0.01):
    """
    printed/computed: dicts with any of opening/debits/credits/closing.
    Returns a list of checks: {label, printed, extracted, ok}.
    """
    names = {"debits": "Total debits", "credits": "Total credits",
             "closing": "Closing balance", "opening": "Opening balance"}
    checks = []
    for key in ("debits", "credits", "closing"):
        if key in printed and computed.get(key) is not None:
            p, c = printed[key], computed[key]
            # Closing balance sign conventions differ (Cr suffix vs plain)
            ok = abs(abs(p) - abs(c)) <= tolerance if key == "closing" else abs(p - c) <= tolerance
            checks.append({"label": names[key], "printed": round(p, 2), "extracted": round(c, 2), "ok": ok})
    return checks
