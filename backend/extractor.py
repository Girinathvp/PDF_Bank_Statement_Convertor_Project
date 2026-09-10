"""
extractor.py
Core PDF -> table extraction logic, purpose-built for bank statements.

Pipeline per page:
  1. Get "words" (word + bounding box), either from the PDF's real text
     layer, or via Tesseract OCR if the page has zero extractable characters
     (scanned / vector-flattened text).
  2. Group words into visual lines (rows) by vertical position.
  3. Find the column-header line(s) (PARTICULARS/WITHDRAWALS/DEPOSITS/
     BALANCE/etc.) -- checking up to one adjacent line too, since some
     banks wrap a column label onto two lines (e.g. "Cheque" / "No/Reference"
     stacked, or "Post" "Date" / "Value" "Date" side by side) -- and derive
     column x-boundaries from the combined header words.
  4. Walk the lines below the header, assigning each word to its column by
     x-position. A line whose Date-column text is an actual date (or B/F)
     starts a new transaction row; any other line is a continuation and its
     text is appended into the same row's cells (so narration stays whole).
  5. Stop consuming lines once a footer marker is hit (page total, page
     number, disclaimer, etc.).
  6. Every non-Particulars column is reconciled: any word in it that
     doesn't actually belong there (not a date/numeric code/amount) is
     pushed back onto Particulars, in the correct reading-order position.

Multi-page robustness: column x-boundaries are locked in from the first
page that gets a confident header detection, and reused for every other
page, since OCR noise can make header detection drift on individual pages
of a long, all-OCR statement even though the underlying template is
identical throughout. If a page's own header detection is missing a
column entirely, the locked version of that column is inserted back in
using its known position.
"""

import re
import pdfplumber
import pandas as pd


# ---------------------------------------------------------------------------
# Page counting / TAT estimation
# ---------------------------------------------------------------------------

def get_page_count(pdf_path):
    with pdfplumber.open(pdf_path) as pdf:
        return len(pdf.pages)


def estimate_tat(num_pages, likely_ocr=False):
    base_overhead = 3
    per_page = 1.6 if likely_ocr else 0.5
    return round(base_overhead + per_page * num_pages)


# ---------------------------------------------------------------------------
# Column-header / footer vocabulary (kept generic across banks)
# ---------------------------------------------------------------------------

HEADER_KEYWORDS = [
    "date", "particulars", "narration", "description", "details",
    "chq", "cheque", "ref", "reference",
    "withdrawal", "debit", "deposit", "credit", "balance", "amount",
]

FOOTER_MARKERS = [
    "page total", "b/f total", "closing balance", "closing balance total",
    "this is a system", "does not require any signature",
    "visit us at", "customer care", "toll-free", "toll free",
    "generated statement", "statement summary",
    "dr count", "cr count", "total debits", "total credits",
]

DATE_RE = re.compile(r"^\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}$")
PARTIAL_DATE_RE = re.compile(r"^[-/.]\d{1,2}[-/.]\d{2,4}$")
CARRY_FORWARD_RE = re.compile(r"^(b/f|c/f|bf|cf|brought|forward)$", re.IGNORECASE)

# Per-page footer noise (e.g. "Page no. 1", "Page 3 of 45") that can end up
# glued onto the SAME visual line as the last real transaction on a page --
# OCR sometimes positions this text close enough vertically to merge with
# the last data row instead of forming its own separate line, so it can't
# be caught by the whole-line footer check below. Stripped out of
# Particulars text specifically (see _reconcile_columns) rather than
# dropping the whole row, since the row's actual amounts are legitimate.
PAGE_NUM_NOISE_RE = re.compile(r"\bpage\s*(no\.?)?\s*\d+(\s*of\s*\d+)?\b", re.IGNORECASE)


def _extract_leading_date(tokens):
    """
    Returns (date_str, remaining_tokens). Handles two OCR artifacts that
    otherwise cause the "is this a new transaction?" check to fail and
    cascade into many rows silently merging into one:
      1. A date split across two tokens by a stray space, e.g. '01' +
         '-04-2025' instead of '01-04-2025'.
      2. OCR dropping the leading day digits entirely, e.g. '-04-2025'
         instead of '01-04-2025' -- this is still recognized as "clearly a
         date, just missing a digit", so the row starts correctly even
         though the exact day is incomplete (visible/fixable in the
         output, rather than silently merged away).
    """
    if not tokens:
        return None, tokens
    t0 = tokens[0]
    if DATE_RE.match(t0) or CARRY_FORWARD_RE.match(t0):
        return t0, tokens[1:]
    if PARTIAL_DATE_RE.match(t0):
        return t0, tokens[1:]
    if len(tokens) >= 2:
        combined = t0 + tokens[1]
        if DATE_RE.match(combined):
            return combined, tokens[2:]
    return None, tokens


def _looks_like_date(text):
    text = (text or "").strip()
    if not text:
        return False
    date, _ = _extract_leading_date(text.split())
    return date is not None


def _is_full_date(text):
    """Stricter than _looks_like_date: True only for a complete date (or
    B/F), not a partial/day-digit-missing one. _looks_like_date stays
    lenient on purpose (a partial date still correctly marks the start of
    a new transaction row, so we don't want to reject those rows), but the
    low-confidence-cell refinement pass needs this stricter check --
    otherwise a partial date like '-04-2025' is treated as "already fine"
    and never gets re-OCR'd, even when isolating and re-reading that cell
    could recover the missing digit."""
    text = (text or "").strip()
    if not text:
        return False
    first_token = text.split()[0]
    return bool(DATE_RE.match(first_token)) or bool(CARRY_FORWARD_RE.match(first_token))


def _is_footer_text(joined_lower):
    return any(marker in joined_lower for marker in FOOTER_MARKERS)


# ---------------------------------------------------------------------------
# Word grouping into visual lines
# ---------------------------------------------------------------------------

def _group_words_into_lines(words, y_tol):
    if not words:
        return []
    ws = sorted(words, key=lambda w: w["top"])
    lines = []
    current = [ws[0]]
    current_top = ws[0]["top"]
    for w in ws[1:]:
        if abs(w["top"] - current_top) <= y_tol:
            current.append(w)
            current_top = sum(x["top"] for x in current) / len(current)
        else:
            lines.append(sorted(current, key=lambda x: x["x0"]))
            current = [w]
            current_top = w["top"]
    lines.append(sorted(current, key=lambda x: x["x0"]))
    return lines


def _line_avg_top(line):
    return sum(w["top"] for w in line) / len(line)


def _line_has_date_token(line):
    return any(DATE_RE.match(w["text"]) for w in line)


# ---------------------------------------------------------------------------
# Header detection: find the best line, then optionally extend into an
# adjacent wrapped line (e.g. "Cheque" / "No/Reference" stacked).
# ---------------------------------------------------------------------------

def _line_keyword_score(line):
    score = 0
    for w in line:
        t = w["text"].lower().strip(":.")
        if any(t == k or t.startswith(k) for k in HEADER_KEYWORDS):
            score += 1
    return score


def _find_header_lines(lines, max_gap=12):
    """
    Returns (header_line_groups, data_start_idx) where header_line_groups is
    a list of physical lines (each a list of words) that together make up
    the column header, and data_start_idx is the index of the first line
    after the header. Returns (None, None) if no confident header found.
    """
    best_idx, best_score = None, 0
    for idx, line in enumerate(lines):
        score = _line_keyword_score(line)
        if score > best_score:
            best_score, best_idx = score, idx
    if best_idx is None or best_score < 3:
        return None, None

    header_lines = [lines[best_idx]]
    header_top = _line_avg_top(lines[best_idx])
    data_start = best_idx + 1

    # Extend into the line immediately after, if it's close vertically and
    # doesn't look like an actual transaction row (which would mean we've
    # run past the header into real data).
    if best_idx + 1 < len(lines):
        nxt = lines[best_idx + 1]
        if abs(_line_avg_top(nxt) - header_top) <= max_gap and not _line_has_date_token(nxt):
            header_lines.append(nxt)
            data_start = best_idx + 2

    return header_lines, data_start


# ---------------------------------------------------------------------------
# Column boundary construction from (possibly multi-line) header words
# ---------------------------------------------------------------------------

_PROTECTED_STANDALONE_HEADERS = {
    "particulars", "narration", "description", "details",
    "withdrawals", "withdrawal", "debit", "debits",
    "deposits", "deposit", "credit", "credits",
    "balance", "amount",
}


def _merge_words_horizontal(line_words, merge_gap, max_words=2):
    """Merge adjacent word fragments on the SAME line into one column label,
    e.g. 'CHQ' + '.NO.' or 'Post' + 'Date'. Capped at 2 words per column so
    a third word (start of the next real column) never gets absorbed.
    Never merges a genuine standalone column keyword (WITHDRAWALS, BALANCE,
    etc.) into a preceding label -- those always start their own column."""
    ordered = sorted(line_words, key=lambda w: w["x0"])
    merged = []
    counts = []
    for w in ordered:
        is_protected = w["text"].strip().lower().strip(".:—-") in _PROTECTED_STANDALONE_HEADERS
        if (merged and not is_protected and counts[-1] < max_words
                and (w["x0"] - merged[-1]["x1"]) < merge_gap):
            merged[-1] = dict(merged[-1])
            merged[-1]["text"] += " " + w["text"]
            merged[-1]["x1"] = max(merged[-1]["x1"], w["x1"])
            counts[-1] += 1
        else:
            merged.append(dict(w))
            counts.append(1)
    return merged


def _x_overlap_fraction(a, b):
    inter = min(a["x1"], b["x1"]) - max(a["x0"], b["x0"])
    if inter <= 0:
        return 0.0
    shorter = min(a["x1"] - a["x0"], b["x1"] - b["x0"])
    return inter / shorter if shorter > 0 else 0.0


def _text_has_header_keyword(text):
    """Whether any token in this (possibly multi-word) header fragment
    matches a recognized column keyword. Used to prefer a correctly-OCR'd
    fragment over a garbled one when merging a label across two stacked
    header lines (see _merge_header_word_groups)."""
    for tok in text.lower().replace("/", " ").split():
        tok = tok.strip(":.")
        if any(tok == k or tok.startswith(k) for k in HEADER_KEYWORDS):
            return True
    return False


def _merge_header_word_groups(line_word_groups, merge_gap, overlap_thresh=0.4):
    """
    line_word_groups: list of physical lines' word lists (1 or 2 lines).
    Phase 1: horizontally merge fragments within each line.
    Phase 2: merge column-candidates ACROSS lines when their x-ranges
    substantially overlap (a label wrapped/stacked across two lines, e.g.
    "Cheque" above "No/Reference"). When one of the two overlapping
    fragments is a recognizable header keyword and the other isn't (a
    common OCR failure mode: one line reads cleanly, the other comes out
    as garbage), keep only the recognizable one instead of concatenating
    OCR noise into the column label.
    """
    per_line_cols = [_merge_words_horizontal(lw, merge_gap) for lw in line_word_groups]
    if not per_line_cols:
        return []
    result = list(per_line_cols[0])
    for line_cols in per_line_cols[1:]:
        used = [False] * len(result)
        leftover = []
        for col in line_cols:
            matched = False
            for i, existing in enumerate(result):
                if not used[i] and _x_overlap_fraction(existing, col) >= overlap_thresh:
                    result[i] = dict(existing)
                    existing_has_kw = _text_has_header_keyword(existing["text"])
                    col_has_kw = _text_has_header_keyword(col["text"])
                    if col_has_kw and not existing_has_kw:
                        result[i]["text"] = col["text"]
                    elif existing_has_kw and not col_has_kw:
                        pass  # keep existing text, drop the non-matching fragment
                    else:
                        result[i]["text"] = existing["text"] + " " + col["text"]
                    result[i]["x0"] = min(existing["x0"], col["x0"])
                    result[i]["x1"] = max(existing["x1"], col["x1"])
                    used[i] = True
                    matched = True
                    break
            if not matched:
                leftover.append(col)
        result = result + leftover
    return sorted(result, key=lambda w: w["x0"])


def _build_columns(header_line_groups, page_width, merge_gap):
    merged = _merge_header_word_groups(header_line_groups, merge_gap)

    has_date_col = any("date" in w["text"].lower() for w in merged)
    if not has_date_col and merged and merged[0]["x0"] > page_width * 0.05:
        merged.insert(0, {"text": "DATE", "x0": 0.0, "x1": merged[0]["x0"]})

    columns = []
    for i, w in enumerate(merged):
        start = 0.0 if i == 0 else (merged[i - 1]["x0"] + w["x0"]) / 2
        end = page_width + 1000 if i == len(merged) - 1 else (w["x0"] + merged[i + 1]["x0"]) / 2
        label = " ".join(w["text"].strip().rstrip(".:—-").split())
        columns.append({"label": label or f"Col{i+1}", "x_start": start, "x_end": end})
    return columns


_REF_COLUMN_KEYWORDS = ("chq", "cheque", "ref", "reference", "no/reference")
_AMOUNT_COLUMN_KEYWORDS = ("withdrawal", "debit", "deposit", "credit", "balance", "amount")


def _ensure_reference_column(columns, locked_columns):
    """
    If this page's own header detection is missing a Chq No / Reference
    column but we have one locked in from an earlier page, insert it here
    using the locked column's x-boundaries.
    """
    if locked_columns is None:
        return columns
    has_ref = any(any(k in c["label"].lower() for k in _REF_COLUMN_KEYWORDS) for c in columns)
    if has_ref:
        return columns
    locked_ref = next(
        (c for c in locked_columns if any(k in c["label"].lower() for k in _REF_COLUMN_KEYWORDS)), None
    )
    if locked_ref is None:
        return columns

    insert_idx = next(
        (i for i, c in enumerate(columns) if any(k in c["label"].lower() for k in _AMOUNT_COLUMN_KEYWORDS)),
        len(columns),
    )
    if insert_idx == 0:
        return columns

    columns = [dict(c) for c in columns]
    new_col = {"label": locked_ref["label"], "x_start": locked_ref["x_start"], "x_end": locked_ref["x_end"]}
    columns[insert_idx - 1]["x_end"] = new_col["x_start"]
    if insert_idx < len(columns):
        columns[insert_idx]["x_start"] = new_col["x_end"]
    columns.insert(insert_idx, new_col)
    return columns


def _assign_line_to_columns(line, columns):
    row = {c["label"]: "" for c in columns}
    row_words = {c["label"]: [] for c in columns}
    for w in line:
        for c in columns:
            if c["x_start"] <= w["x0"] < c["x_end"]:
                row[c["label"]] = (row[c["label"]] + " " + w["text"]).strip()
                row_words[c["label"]].append(w)
                break
        else:
            last = columns[-1]["label"]
            row[last] = (row[last] + " " + w["text"]).strip()
            row_words[last].append(w)
    return row, row_words


_NUMERIC_CODE_RE = re.compile(r"^[\d/\-\.]{3,}$")
_AMOUNT_RE = re.compile(r"^(?:[\d,]+(?:\.\d{2})?(?:\s*(?:cr|dr))?|(?:cr|dr))$", re.IGNORECASE)
_STRAY_PUNCT_TOKEN_RE = re.compile(r"(?:^|(?<=\s))[.,](?=\s|$)")


def _strip_stray_punctuation(text):
    """
    Remove isolated punctuation-only tokens (a lone ',' or '.' surrounded
    by whitespace) from OCR'd amount text, e.g. '16,22,934.37 , CR' ->
    '16,22,934.37 CR'. These are almost always OCR noise -- a decimal tick
    or artifact detected as its own separate "word" slightly offset from
    the digits around it -- rather than meaningful content. Left in place,
    they make an otherwise-correct reading fail the amount-format check
    and get needlessly flagged for manual review.
    """
    cleaned = _STRAY_PUNCT_TOKEN_RE.sub("", text)
    return re.sub(r"\s+", " ", cleaned).strip()


_COMMA_BEFORE_DECIMAL_RE = re.compile(r",(\.)")
_TRAILING_TWO_DIGIT_COMMA_RE = re.compile(r"(\d),(\d{2})(\s*(?:cr|dr)?)$", re.IGNORECASE)


def _normalize_amount_grouping(text):
    """
    Fix two specific, verified OCR misreads in amount/Balance values:

    1. A comma immediately followed by a decimal point, e.g.
       '8,84,474,.37 CR' -- the comma is spurious (almost certainly a
       genuine decimal point that got detected twice, once correctly and
       once misread as a comma right before it); drop it.

    2. A trailing comma-separated group of exactly 2 digits at the very
       end of the number, e.g. '8,85,974,37 CR'. Valid Indian-style digit
       grouping ALWAYS ends in a 3-digit group (or has no comma at all,
       for numbers under 1000) -- a 2-digit final group is not a legal
       grouping. Combined with every balance in this class of statement
       carrying exactly 2 decimal digits, this is reliably a misread
       decimal point rather than a thousands separator; replace that
       comma with '.'.

    Verified directly against real failing rows from the source document
    (cross-checked against the running Balance) before landing this fix --
    e.g. '8,85,974,37 CR' -> '8,85,974.37 CR' now correctly matches the
    balance implied by the surrounding transactions.
    """
    if not text:
        return text
    text = _COMMA_BEFORE_DECIMAL_RE.sub(r"\1", text)
    text = _TRAILING_TWO_DIGIT_COMMA_RE.sub(r"\1.\2\3", text)
    return text


def _looks_like_amount(text):
    return bool(_AMOUNT_RE.match((text or "").strip()))


def _has_cr_dr_suffix(text):
    """
    Whether an amount string carries a Cr/Dr indicator. Deliberately NOT
    using a \\b word-boundary regex here: digits and letters are both "word"
    characters in regex, so \\b never matches between them -- meaning a very
    common real-world OCR reading like '4,70,283.37CR' (no space before the
    suffix) would incorrectly fail a \\b(cr|dr)\\b check even though it's a
    complete, correct value. A plain substring check is what's actually
    needed here.
    """
    return "cr" in (text or "").lower() or "dr" in (text or "").lower()


def _reocr_cell(crop_image, kind):
    """Re-run Tesseract on a tightly-cropped single cell, using a character
    whitelist matched to what that column can legitimately contain.
    Isolating just the cell (rather than the whole page) and constraining
    the character set both meaningfully improve accuracy on cells the
    full-page pass got wrong or low-confidence on. Uses psm 6 (uniform
    block of text) rather than psm 7 (strictly one line) because an amount
    cell can legitimately wrap across two visual lines (the number, then a
    Cr/Dr suffix stacked below it) -- psm 7 fails outright on that case."""
    import pytesseract

    # Upscale the crop before re-OCR'ing. These crops are often only ~15-30px
    # tall (a single table cell at page-render resolution), and Tesseract's
    # accuracy on genuinely small/thin text improves meaningfully once it's
    # enlarged -- this is a standard, well-established OCR preprocessing step.
    try:
        from PIL import Image as _PILImage
        target_h = 90
        if crop_image.height and crop_image.height < target_h:
            factor = target_h / crop_image.height
            new_size = (max(1, int(crop_image.width * factor)), target_h)
            crop_image = crop_image.resize(new_size, _PILImage.LANCZOS)
    except Exception:
        pass

    whitelist = "0123456789-/." if kind == "date" else "0123456789,.CRDcrd "
    config = f"--psm 6 -c tessedit_char_whitelist={whitelist}"
    try:
        text = pytesseract.image_to_string(crop_image, config=config)
    except Exception:
        return ""
    return " ".join(text.split())


_NEEDS_REVIEW_KEY = "_needs_review"


def _attempt_blind_date_recovery(row, label, columns, all_line_words, ocr_context):
    """
    Called when a date column has ZERO detected words on this line -- the
    full-page OCR pass missed the glyphs entirely rather than misreading
    them, so there's no existing word bounding box to crop from at all.
    Constructs a search region from the column's own x-boundaries and this
    line's overall vertical extent (derived from whatever OTHER words on
    the line WERE detected, e.g. the narration), then attempts a re-OCR
    there. Returns True if the cell still needs manual review after this
    attempt (i.e. recovery failed or wasn't attempted).
    """
    col_def = next((c for c in columns if c["label"] == label), None)
    if col_def is None:
        return True

    image = ocr_context["image"]
    scale = ocr_context["scale"]
    top = min(w["top"] for w in all_line_words)
    bottom = max(w["bottom"] for w in all_line_words)
    x0 = max(0.0, col_def["x_start"])
    # Column boundaries can be very wide (e.g. the last column extends to
    # page width); cap the search width to a sane date-cell size so the
    # crop doesn't balloon into unrelated content.
    x1 = min(col_def["x_end"], x0 + 90.0)

    margin_pt = 2.0
    px0 = max(0, int((x0 - margin_pt) / scale))
    py0 = max(0, int((top - margin_pt) / scale))
    px1 = int((x1 + margin_pt) / scale)
    py1 = int((bottom + margin_pt) / scale)
    if px1 <= px0 or py1 <= py0:
        return True

    try:
        crop = image.crop((px0, py0, px1, py1))
    except Exception:
        return True

    refined = _reocr_cell(crop, "date")
    if refined and _is_full_date(refined):
        row[label] = refined
        return False
    return True


def _refine_low_confidence_cells(row, row_words, columns, date_labels, ocr_context, conf_threshold=45):
    """
    Amount and date cells are re-OCR'd individually (cropped straight from
    the source page image at production resolution) when either:
      - the lowest per-word OCR confidence contributing to that cell is
        below threshold (Tesseract itself flagged the reading as
        unreliable), or
      - the assembled text doesn't match the strict expected format for
        that column (a full date, or a clean amount).
    This is what catches cases like a full-page OCR pass misreading a
    perfectly sharp "18,49,596.37" as "18487" with confidence 0 --
    re-cropping just that cell and reading it in isolation, with a
    whitelist restricted to digits/punctuation, resolves it correctly.
    Only replaces the cell when the re-OCR result itself looks valid, so a
    failed re-attempt never makes an already-correct cell worse.

    Sets row[_NEEDS_REVIEW_KEY] = True when a cell was flagged as suspect
    and even this refinement pass couldn't produce a confidently-valid
    replacement. This is a real, expected outcome on some cells of a
    heavily-degraded scan -- rather than silently leaving a wrong-looking
    value in the sheet with no indication, the row is marked so it's easy
    to find and manually verify in the output (see write_excel).
    """
    if not ocr_context or ocr_context.get("image") is None:
        return row
    image = ocr_context["image"]
    scale = ocr_context["scale"]

    targets = [(label, "date") for label in date_labels]
    for c in columns:
        if any(k in c["label"].lower() for k in _AMOUNT_COLUMN_KEYWORDS):
            targets.append((c["label"], "amount"))

    all_line_words = [w for ws in row_words.values() for w in ws]
    needs_review = False
    for label, kind in targets:
        ws = row_words.get(label) or []
        if not ws:
            if kind == "date" and all_line_words and ocr_context and ocr_context.get("image") is not None:
                needs_review = _attempt_blind_date_recovery(
                    row, label, columns, all_line_words, ocr_context
                ) or needs_review
            elif kind == "date":
                needs_review = True
            continue
        text = row.get(label, "")
        if kind == "amount" and text:
            text = _strip_stray_punctuation(text)
            text = _normalize_amount_grouping(text)
            row[label] = text
        min_conf = min(w.get("conf", 100) for w in ws)
        looks_valid = _is_full_date(text) if kind == "date" else _looks_like_amount(text)

        # A genuine Balance reading in these statements always carries a
        # Cr/Dr suffix. Tesseract can produce a numerically-wrong reading
        # that still happens to be syntactically valid (e.g. "18,478.37"
        # instead of the real "18,47,096.37 CR") at moderate confidence, so
        # a missing suffix on this specific column is treated as a strong
        # signal to re-check even when the format otherwise looks fine.
        if "balance" in label.lower() and text and not _has_cr_dr_suffix(text):
            looks_valid = False

        if min_conf >= conf_threshold and looks_valid:
            continue

        x0 = min(w["x0"] for w in ws)
        x1 = max(w["x1"] for w in ws)
        top = min(w["top"] for w in ws)
        bottom = max(w["bottom"] for w in ws)

        # A word's own bounding box can occasionally be badly mismeasured
        # by the full-page OCR pass -- a sliver a fraction of a point tall,
        # nowhere near real text height. That degenerate box is often
        # exactly WHY the reading was garbled/low-confidence in the first
        # place, so cropping around that same broken box can't help.
        # Fall back to this whole line's vertical extent (derived from
        # whatever other words on the line have sane bounding boxes).
        MIN_SANE_HEIGHT = 5.0
        if (bottom - top) < MIN_SANE_HEIGHT and all_line_words:
            sane_words = [w for w in all_line_words if (w["bottom"] - w["top"]) >= MIN_SANE_HEIGHT]
            if sane_words:
                top = min(w["top"] for w in sane_words)
                bottom = max(w["bottom"] for w in sane_words)

        margin_pt = 2.0

        # If the assembled text isn't valid, the missing piece is very
        # likely digits the full-page OCR pass failed to detect as any
        # glyph at all -- meaning the word's own bounding box never
        # included those pixels. Re-cropping tightly around that same
        # bounding box can never recover them, so widen the crop leftward
        # before re-OCR'ing. Applies to both date cells (missing leading
        # day-digit) and amount/Balance cells (missing leading
        # lakhs/thousands digits -- e.g. "151,283," instead of the real
        # "5,51,283.37 CR").
        #
        # The widen amount is calibrated from the token's OWN average
        # character width (rather than reaching out to the column's
        # heuristic x-boundary, which is only a midpoint estimate and can
        # actually overlap the previous column's real rendered text --
        # verified directly: doing that pulled in leftover digits from a
        # neighbouring cell). A minimum floor handles the case where the
        # original reading was itself very short (e.g. just ",,"), which
        # would otherwise produce too small a widen amount to matter.
        if not looks_valid:
            best_text = row.get(label, "") or (ws[0]["text"] if ws else "")
            char_w = (x1 - x0) / max(len(best_text), 1)
            min_widen = 12.0 if kind == "date" else 25.0
            widen = max(char_w * 2.5, min_widen)
            col_def = next((c for c in columns if c["label"] == label), None)
            # Never cross this column's own left boundary -- doing so risks
            # grabbing the previous column's real rendered content (verified
            # directly: an earlier, less-bounded version of this widening
            # pulled a neighbouring cell's digits into the crop and made
            # the re-OCR result worse, not better).
            floor = col_def["x_start"] if col_def is not None else 0.0
            x0 = max(floor, x0 - widen)
            # NOTE: a symmetric rightward widen for date cells was tried
            # and reverted -- verified directly that it regressed already-
            # correct dates (e.g. a clean '01-04-2025' reading became the
            # corrupted '01-04-21') while still not reliably recovering the
            # cases it targeted. Left-only widening is the validated,
            # net-positive version.
            if kind == "amount":
                line_h = bottom - top
                top = max(0.0, top - line_h * 0.6)
                bottom = bottom + line_h * 0.6

        px0 = max(0, int((x0 - margin_pt) / scale))
        py0 = max(0, int((top - margin_pt) / scale))
        px1 = int((x1 + margin_pt) / scale)
        py1 = int((bottom + margin_pt) / scale)
        if px1 <= px0 or py1 <= py0:
            needs_review = True
            continue

        try:
            crop = image.crop((px0, py0, px1, py1))
        except Exception:
            needs_review = True
            continue

        refined = _reocr_cell(crop, kind)
        final_valid = looks_valid  # cell may already have been fine; refinement is a bonus, not a requirement
        if refined:
            refined_valid = _is_full_date(refined) if kind == "date" else _looks_like_amount(refined)
            if refined_valid and kind == "date" and text:
                # Only trust the recovered leading digit(s); the trailing
                # -MM-YYYY was already read with reasonable confidence in
                # the original pass, so require it to match exactly -- a
                # re-OCR guess that changes the month/year too is more
                # likely a misread than a genuine correction, and is
                # rejected rather than risking a silently wrong date.
                orig_suffix = re.sub(r"^\d*", "", text.split()[0])
                new_suffix = re.sub(r"^\d*", "", refined.split()[0])
                if orig_suffix and orig_suffix != new_suffix:
                    refined_valid = False
            if refined_valid:
                row[label] = refined
                final_valid = True
        if not final_valid:
            # This cell was flagged as suspect (low confidence or wrong
            # format) and re-OCR either failed outright or didn't produce
            # something we can confidently trust -- the original value is
            # left in place (never overwritten with an unconfirmed guess),
            # but the row is marked for the person to manually double-check.
            needs_review = True

    row[_NEEDS_REVIEW_KEY] = needs_review
    return row


def _particulars_label(columns):
    return next(
        (c["label"] for c in columns
         if any(k in c["label"].lower() for k in ("particular", "narration", "description", "detail"))),
        None,
    )


def _final_validate_row(row, columns, date_labels):
    """
    Authoritative needs-review check, run once per fully-assembled
    transaction row after all its continuation lines have been merged in.
    This replaces whatever provisional flag accumulated during per-line
    refinement (see _refine_low_confidence_cells), because a per-line
    check can be a false positive: e.g. a Balance's Cr/Dr suffix can
    legitimately wrap onto a separate continuation line in this document's
    OCR'd layout, so the very first physical line correctly looks
    "incomplete" on its own even though the fully-merged result is fine.
    Only what actually ends up in the final cell is what should determine
    whether a person needs to double-check it.
    """
    particulars_label = _particulars_label(columns)
    particulars_text = (row.get(particulars_label, "") or "") if particulars_label else ""
    is_opening_balance_row = bool(
        re.search(r"\b(brought forward|opening balance|b/f)\b", particulars_text, re.IGNORECASE)
    )

    for date_label in date_labels:
        val = row.get(date_label, "")
        if is_opening_balance_row and not val:
            continue  # genuinely no date in the source statement for this row
        if not val or not _is_full_date(val):
            return True
    for c in columns:
        label_lower = c["label"].lower()
        if not any(k in label_lower for k in _AMOUNT_COLUMN_KEYWORDS):
            continue
        val = row.get(c["label"], "")
        if not val:
            continue
        if not _looks_like_amount(val):
            return True
        if "balance" in label_lower and not _has_cr_dr_suffix(val):
            return True
    return False


def _date_column_labels(columns):
    """A statement may have more than one date column (e.g. Post Date /
    Value Date). Every column whose label contains 'date' is reconciled
    the same way -- keep only a leading date/B-F token, push overflow to
    Particulars."""
    return [c["label"] for c in columns if "date" in c["label"].lower()]


def _reconcile_columns(row, columns, date_labels):
    """
    Every column except Particulars should only ever hold content that
    actually belongs to it. When a line's text physically overlaps a
    neighbouring column's x-range, stray words land in the wrong cell; this
    pushes them back onto Particulars, in the correct reading-order
    position, instead of corrupting that column's own value.
    """
    particulars_label = _particulars_label(columns)
    if particulars_label is None:
        return row

    for date_label in date_labels:
        date_text = row.get(date_label, "")
        if not date_text:
            continue
        tokens = date_text.split()
        date_val, overflow = _extract_leading_date(tokens)
        if date_val is not None:
            row[date_label] = date_val
        else:
            overflow = tokens
            row[date_label] = ""
        if overflow:
            row[particulars_label] = f"{' '.join(overflow)} {row[particulars_label]}".strip()

    for c in columns:
        label_lower = c["label"].lower()
        if not any(k in label_lower for k in _REF_COLUMN_KEYWORDS):
            continue
        text = row.get(c["label"], "")
        if not text:
            continue
        tokens = text.split()
        numeric_tokens = [t for t in tokens if _NUMERIC_CODE_RE.match(t)]
        stray_tokens = [t for t in tokens if not _NUMERIC_CODE_RE.match(t)]
        if stray_tokens:
            row[particulars_label] = f"{row[particulars_label]} {' '.join(stray_tokens)}".strip()
            row[c["label"]] = " ".join(numeric_tokens)

    for c in columns:
        label_lower = c["label"].lower()
        if not any(k in label_lower for k in _AMOUNT_COLUMN_KEYWORDS):
            continue
        text = row.get(c["label"], "")
        if not text:
            continue
        tokens = text.split()
        keep, stray = [], []
        for t in tokens:
            if t in (",", "."):
                # A lone comma/period token is virtually always OCR noise
                # (a misread decimal tick or artifact) rather than
                # meaningful content -- drop it rather than keeping it as
                # a spurious fragment in the amount value.
                continue
            if _AMOUNT_RE.match(t) or t.lower() in ("cr", "dr"):
                keep.append(t)
            else:
                stray.append(t)
        if stray:
            row[particulars_label] = f"{row[particulars_label]} {' '.join(stray)}".strip()

        numeric_fragments = [t for t in keep if t.lower() not in ("cr", "dr")]
        suffix_fragments = [t for t in keep if t.lower() in ("cr", "dr")]
        if len(numeric_fragments) > 1:
            # A real amount/Balance cell holds exactly one number. Multiple
            # separate numeric-looking fragments here are almost always one
            # number that OCR split into two words at a rendering gap (e.g.
            # '21,38,' + '767.37' instead of '21,38,767.37') -- concatenate
            # them directly rather than space-joining, which would otherwise
            # leave a malformed value with a stray space in the middle.
            row[c["label"]] = "".join(numeric_fragments) + ("".join(f" {s}" for s in suffix_fragments))
        else:
            row[c["label"]] = " ".join(keep)
        row[c["label"]] = _normalize_amount_grouping(row[c["label"]])

    # Strip page-footer noise that can end up glued onto the last real
    # transaction line on a page (see PAGE_NUM_NOISE_RE comment above).
    if row.get(particulars_label):
        cleaned = PAGE_NUM_NOISE_RE.sub("", row[particulars_label])
        row[particulars_label] = re.sub(r"\s+", " ", cleaned).strip()

    return row


# ---------------------------------------------------------------------------
# Structured extraction for one page
# ---------------------------------------------------------------------------

def _relabel_columns_from_locked(columns, locked_columns):
    """
    A single-word OCR misread in a header (e.g. 'Debit' read as 'Psion')
    can slip through when it happens on its own line with no competing
    correct-text fragment to prefer against (unlike the two-line
    'Cheque'/'No/Reference' case _merge_header_word_groups already
    handles). Left uncorrected, that page's rows would be keyed under a
    garbled label that doesn't match the canonical header used when
    merging pages, silently dropping that column's data for the page.

    Once column semantics are locked in from an earlier confidently-parsed
    page, and this page detects the same NUMBER of columns in the same
    left-to-right order, reuse the locked label strings by position --
    trusting that a single statement's column layout doesn't change
    page-to-page -- while keeping this page's own x-boundaries (OCR word
    positions can shift slightly page-to-page even when the layout is the
    same).
    """
    if locked_columns is None or len(columns) != len(locked_columns):
        return columns
    relabeled = []
    for local_col, locked_col in zip(columns, locked_columns):
        new_col = dict(local_col)
        new_col["label"] = locked_col["label"]
        relabeled.append(new_col)
    return relabeled


def _extract_structured(words, page_width, y_tol, merge_gap, locked_columns=None, ocr_context=None):
    lines = _group_words_into_lines(words, y_tol=y_tol)
    header_line_groups, data_start = _find_header_lines(lines)

    if header_line_groups is not None:
        columns = _build_columns(header_line_groups, page_width, merge_gap)
        columns = _ensure_reference_column(columns, locked_columns)
        columns = _relabel_columns_from_locked(columns, locked_columns)
    elif locked_columns is not None:
        columns = locked_columns
        date_labels = _date_column_labels(columns) or [columns[0]["label"]]
        primary_date_label = date_labels[0]
        data_start = None
        for idx, line in enumerate(lines):
            row, _ = _assign_line_to_columns(line, columns)
            if _looks_like_date(row.get(primary_date_label, "")):
                data_start = idx
                break
        if data_start is None:
            return None, None, None
    else:
        return None, None, None

    date_labels = _date_column_labels(columns) or [columns[0]["label"]]
    primary_date_label = date_labels[0]
    rows = []
    current = None
    for line in lines[data_start:]:
        joined_lower = " ".join(w["text"] for w in line).lower()
        if _is_footer_text(joined_lower):
            break

        row, row_words = _assign_line_to_columns(line, columns)
        row = _refine_low_confidence_cells(row, row_words, columns, date_labels, ocr_context)
        row = _reconcile_columns(row, columns, date_labels)
        date_val = row.get(primary_date_label, "")

        if _looks_like_date(date_val):
            if current is not None:
                current[_NEEDS_REVIEW_KEY] = _final_validate_row(current, columns, date_labels)
                rows.append(current)
            current = row
        else:
            if current is None:
                current = row
            else:
                current[_NEEDS_REVIEW_KEY] = bool(current.get(_NEEDS_REVIEW_KEY)) or bool(row.get(_NEEDS_REVIEW_KEY))
                for label, val in row.items():
                    if label == _NEEDS_REVIEW_KEY:
                        continue
                    if not val:
                        continue
                    current[label] = f"{current[label]} {val}".strip() if current[label] else val

    if current is not None:
        current[_NEEDS_REVIEW_KEY] = _final_validate_row(current, columns, date_labels)
        rows.append(current)

    header_labels = [c["label"] for c in columns]
    return header_labels, rows, columns


# ---------------------------------------------------------------------------
# Word sourcing: real text layer vs. OCR
# ---------------------------------------------------------------------------

def _words_from_text_layer(page):
    words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
    return [
        {"text": w["text"], "x0": w["x0"], "x1": w["x1"], "top": w["top"], "bottom": w["bottom"],
         "conf": 100.0}
        for w in words
    ]


def _resolve_tesseract_cmd():
    import os as _os
    import sys as _sys

    env_override = _os.environ.get("TESSERACT_CMD")
    if env_override and _os.path.exists(env_override):
        return env_override

    if _os.name == "nt":
        if getattr(_sys, "frozen", False):
            base_dir = getattr(_sys, "_MEIPASS", _os.path.dirname(_sys.executable))
        else:
            base_dir = _os.path.dirname(_os.path.abspath(__file__))
        bundled = _os.path.join(base_dir, "tesseract", "tesseract.exe")
        if _os.path.exists(bundled):
            return bundled

        default_win_path = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
        if _os.path.exists(default_win_path):
            return default_win_path

    return None


_GRIDLINE_ARTIFACT_RE = re.compile(r"^[|¦‖!]+$")


def _suppress_duplicate_words(words, top_tol=8, x_overlap_thresh=0.5):
    """
    Tesseract occasionally produces a duplicate 'echo' detection of the same
    physical text: a clean, higher-confidence reading, plus a second,
    lower-confidence, garbled reading sitting just a few points above/below
    it with heavily overlapping x-range (e.g. a real '19-04-2025' plus a
    phantom '-04-' a few points below it covering the same characters).
    Left in place, both land in the same line-group (their vertical gap is
    well within normal same-line tolerance) and corrupt the row -- visible
    as duplicated trailing digits or scrambled word order.

    This keeps the higher-confidence word of any such overlapping pair and
    drops the lower-confidence one. Processed in confidence-descending
    order so the "real" reading is always kept and only genuine echoes
    (which by definition score lower) are removed.
    """
    if not words:
        return words
    ordered = sorted(words, key=lambda w: w.get("conf", 0), reverse=True)
    kept = []
    for w in ordered:
        is_echo = False
        for k in kept:
            if abs(w["top"] - k["top"]) > top_tol:
                continue
            inter = min(w["x1"], k["x1"]) - max(w["x0"], k["x0"])
            if inter <= 0:
                continue
            shorter = min(w["x1"] - w["x0"], k["x1"] - k["x0"])
            overlap = inter / shorter if shorter > 0 else 0.0
            if overlap >= x_overlap_thresh:
                is_echo = True
                break
        if not is_echo:
            kept.append(w)
    return sorted(kept, key=lambda w: w["top"])


def _words_from_ocr(page, dpi=250):
    import pytesseract

    tesseract_cmd = _resolve_tesseract_cmd()
    if tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd

    pil_image = page.to_image(resolution=dpi).original
    data = pytesseract.image_to_data(pil_image, output_type=pytesseract.Output.DICT)

    scale = 72.0 / dpi
    words = []
    n = len(data["text"])
    for i in range(n):
        text = data["text"][i].strip()
        if not text:
            continue
        if _GRIDLINE_ARTIFACT_RE.match(text):
            # A real (often faint) vertical table rule between two columns
            # can get misread by OCR as a stray '|'-like character. It
            # carries no data, and left in place it pollutes whichever
            # column it lands in (e.g. gluing onto the next column's
            # narration text). Drop it here rather than trying to route it.
            continue
        conf_raw = data.get("conf", ["-1"] * n)[i]
        try:
            conf = float(conf_raw)
        except (ValueError, TypeError):
            conf = -1.0
        if conf < 0:
            continue
        left = data["left"][i] * scale
        top = data["top"][i] * scale
        width = data["width"][i] * scale
        height = data["height"][i] * scale
        words.append(
            {"text": text, "x0": left, "x1": left + width, "top": top, "bottom": top + height,
             "conf": conf}
        )
    words = _suppress_duplicate_words(words)
    return words, pil_image, scale


# ---------------------------------------------------------------------------
# Per-page orchestration
# ---------------------------------------------------------------------------

def extract_page(page, locked_columns=None):
    has_chars = len(page.chars) > 0
    ocr_context = None

    if has_chars:
        words = _words_from_text_layer(page)
        method = "text"
        y_tol, merge_gap = 3, 6
    else:
        words, ocr_image, ocr_scale = _words_from_ocr(page)
        method = "ocr"
        y_tol, merge_gap = 6, 15
        ocr_context = {"image": ocr_image, "scale": ocr_scale}

    header, rows, columns = _extract_structured(
        words, page.width, y_tol=y_tol, merge_gap=merge_gap, locked_columns=locked_columns,
        ocr_context=ocr_context,
    )
    if header is None:
        return {"type": "none"}, method, locked_columns

    return {"type": "structured", "header": header, "rows": rows}, method, columns


def process_pdf(pdf_path, progress_callback=None):
    results = {}
    methods = {}
    locked_columns = None
    with pdfplumber.open(pdf_path) as pdf:
        total = len(pdf.pages)
        for i, page in enumerate(pdf.pages):
            page_result, method, columns_used = extract_page(page, locked_columns=locked_columns)
            if locked_columns is None and columns_used is not None:
                locked_columns = columns_used
            results[i + 1] = page_result
            methods[i + 1] = method
            if progress_callback:
                progress_callback(i + 1, total)
    return results, methods


def _process_single_page(args):
    pdf_path, page_index, locked_columns = args
    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[page_index]
        page_result, method, columns_used = extract_page(page, locked_columns=locked_columns)
    return page_index, page_result, method


def process_pdf_parallel(pdf_path, progress_callback=None, max_workers=None):
    import os
    from concurrent.futures import ProcessPoolExecutor, as_completed

    with pdfplumber.open(pdf_path) as pdf:
        total = len(pdf.pages)
        first_page = pdf.pages[0]
        first_result, first_method, locked_columns = extract_page(first_page)

    results = {1: first_result}
    methods = {1: first_method}
    done = 1
    if progress_callback:
        progress_callback(done, total)

    if total <= 1:
        return results, methods

    if max_workers is None:
        max_workers = max(1, (os.cpu_count() or 2) - 1)

    tasks = [(pdf_path, idx, locked_columns) for idx in range(1, total)]
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_process_single_page, t) for t in tasks]
        for future in as_completed(futures):
            page_index, page_result, method = future.result()
            results[page_index + 1] = page_result
            methods[page_index + 1] = method
            done += 1
            if progress_callback:
                progress_callback(done, total)

    return results, methods


# ---------------------------------------------------------------------------
# Excel output
# ---------------------------------------------------------------------------

def _canonical_header(results):
    candidates = [
        r["header"] for r in results.values()
        if r.get("type") == "structured" and r.get("header")
    ]
    if not candidates:
        return None
    return max(candidates, key=len)


def _find_column_label(header, keywords):
    if not header:
        return None
    return next((h for h in header if any(k in h.lower() for k in keywords)), None)


_SINGLE_SIGNED_AMOUNT_RE = re.compile(r"^([\d,]+\.\d{2})\s*(cr|dr)?$", re.IGNORECASE)
_SINGLE_PLAIN_AMOUNT_RE = re.compile(r"^[\d,]+\.\d{2}$")


def _parse_signed_balance(text):
    """
    Parse a Balance cell into a signed float (positive = Cr, negative =
    Dr), but ONLY when it's a single, cleanly-formatted amount -- returns
    None for anything merged, truncated, or otherwise not matching that
    exact shape (e.g. '151,283,' or two values concatenated together),
    rather than guessing at a partial/ambiguous reading.
    """
    if not text:
        return None
    m = _SINGLE_SIGNED_AMOUNT_RE.match(text.strip())
    if not m:
        return None
    try:
        v = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return -v if (m.group(2) or "").lower() == "dr" else v


def _parse_clean_amount(text):
    """
    Parse a Debit/Credit cell as a plain float, but ONLY when it's a
    single clean amount -- returns None (not 0) for a merged/malformed
    cell like '4,800.00 1,500.00', so the caller can tell "genuinely
    blank" apart from "can't be trusted", rather than silently treating
    an unparseable value as zero.
    """
    if not text:
        return 0.0
    if not _SINGLE_PLAIN_AMOUNT_RE.match(text.strip()):
        return None
    try:
        return float(text.strip().replace(",", ""))
    except ValueError:
        return None


def _format_signed_balance(value):
    """Format a corrected balance value as 'X,XX,XXX.37 Cr' / '... Dr',
    using standard Indian-style digit grouping (3 digits, then groups of
    2) for the corrected replacement text."""
    is_dr = value < 0
    v = round(abs(value), 2)
    int_part = int(v)
    frac_part = round((v - int_part) * 100)
    s = str(int_part)
    if len(s) > 3:
        last3 = s[-3:]
        rest = s[:-3]
        groups = []
        while len(rest) > 2:
            groups.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            groups.insert(0, rest)
        s = ",".join(groups + [last3])
    return f"{s}.{frac_part:02d} {'Dr' if is_dr else 'Cr'}"


def _format_amount(value):
    """Format a corrected Debit/Credit value using standard Indian-style
    digit grouping, no Cr/Dr suffix (unlike Balance, these columns are
    unsigned -- the column itself indicates direction)."""
    v = round(abs(value), 2)
    int_part = int(v)
    frac_part = round((v - int_part) * 100)
    s = str(int_part)
    if len(s) > 3:
        last3 = s[-3:]
        rest = s[:-3]
        groups = []
        while len(rest) > 2:
            groups.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            groups.insert(0, rest)
        s = ",".join(groups + [last3])
    return f"{s}.{frac_part:02d}"


def _correct_balances_via_running_total(all_rows_in_order, debit_label, credit_label, balance_label, tolerance=1.0):
    """
    Walks the full transaction sequence in document order, cross-validating
    (and correcting, when confidently possible) each row's Balance against
    the running total implied by the previous reliable balance plus this
    row's own Debit/Credit.

    Why derive Balance from Debit/Credit rather than trust each Balance
    reading directly: cross-checking the actual extracted data from this
    document showed Balance readings are the most failure-prone column by
    a wide margin (a missing leading digit is far more common there than
    in Debit/Credit), while Debit/Credit individually parse cleanly in all
    but one row across the whole 954-row document. Given that reliability
    gap, the running total is a stronger source of truth than the OCR'd
    Balance text itself whenever the two disagree.

    Only replaces a Balance when this row's own Debit/Credit are each
    individually clean (not merged/malformed) and a reliable previous
    balance is available -- if either condition fails, the original
    Balance is left untouched and the chain resets, so a bad row can't
    silently corrupt every balance after it; the next row with its own
    cleanly-parseable Balance re-establishes trust from there.

    Symmetrically: when this row's OWN Balance is trustworthy (parses
    cleanly) and the previous balance is trustworthy, but exactly one of
    Debit/Credit failed clean parsing (e.g. a truncated '400.' where the
    real value is '2,400.00'), the bad one is solved for algebraically
    from the two trustworthy balances -- since a wrong Debit/Credit
    reading silently undercounts totals (unlike a wrong Balance, which
    stays visible), this is worth correcting even though Balance itself
    doesn't need fixing in this case.
    """
    if balance_label is None:
        return all_rows_in_order

    prev_balance = None
    for row in all_rows_in_order:
        own_balance_text = row.get(balance_label, "")
        own_balance = _parse_signed_balance(own_balance_text)
        debit = _parse_clean_amount(row.get(debit_label, "")) if debit_label else 0.0
        credit = _parse_clean_amount(row.get(credit_label, "")) if credit_label else 0.0

        if prev_balance is not None and debit is not None and credit is not None:
            implied = prev_balance - debit + credit
            if own_balance is not None and abs(implied - own_balance) <= tolerance:
                prev_balance = own_balance  # matches -- trust it as-is
            else:
                # Either no usable Balance reading here, or it disagrees
                # with the running total -- the derived value is more
                # trustworthy given the reliability gap described above.
                row[balance_label] = _format_signed_balance(implied)
                prev_balance = implied
        elif prev_balance is not None and own_balance is not None and (debit is None) != (credit is None):
            # Exactly one of Debit/Credit is untrustworthy, but both
            # balances bracketing this transaction ARE trustworthy --
            # solve for the missing one directly. A wrong Debit/Credit
            # silently undercounts totals (unlike a wrong Balance, which
            # stays visibly wrong), so this is worth fixing even when
            # Balance itself needs no correction here.
            if debit is None and debit_label:
                implied_debit = prev_balance - own_balance + (credit or 0.0)
                if implied_debit > tolerance:
                    row[debit_label] = _format_amount(implied_debit)
            elif credit is None and credit_label:
                implied_credit = own_balance - prev_balance + (debit or 0.0)
                if implied_credit > tolerance:
                    row[credit_label] = _format_amount(implied_credit)
            prev_balance = own_balance
        else:
            # Chain not currently trustworthy (start of document, or the
            # previous row broke it) -- fall back to this row's own
            # reading if it's usable, to re-establish the chain for
            # subsequent rows; otherwise leave it as-is (already flagged
            # by the earlier validation pass) and stay in "unreliable"
            # state until a row with its own clean Balance appears.
            prev_balance = own_balance

    return all_rows_in_order


def _row_to_ordered_list(row, header):
    out = []
    row_keys_lower = {k.lower(): k for k in row.keys()}
    for label in header:
        key = row_keys_lower.get(label.lower())
        if key is None:
            key = next(
                (k for k in row.keys() if label.lower() in k.lower() or k.lower() in label.lower()),
                None,
            )
        out.append(row.get(key, "") if key else "")
    return out


_SPLIT_AMOUNT_RE = re.compile(r"^[\d,]+\.\d{2}$")
_SPLIT_BALANCE_TOKEN_RE = re.compile(r"^[\d,]+\.\d{2}(cr|dr)?$", re.IGNORECASE)
_NARRATION_START_KEYWORDS = ("UPI", "IMPS", "NEFT", "RTGS", "DEP ", "WDL ", "CLG ", "Transfer")


def _split_description(desc, n):
    """
    Best-effort split of a merged narration into n pieces, using common
    transaction-type keywords (UPI/IMPS/NEFT/...) as likely boundaries
    between the two (or more) original narrations. Falls back to an even
    word-count split if fewer than n-1 marker positions are found. This is
    inherently approximate -- narration text isn't used in any financial
    total, so an imperfect split here is a cosmetic issue, not a data
    integrity one (unlike the amount/balance split, which must be exact).
    """
    if n <= 1 or not desc:
        return [desc] * max(n, 1)
    positions = sorted({
        m.start()
        for kw in _NARRATION_START_KEYWORDS
        for m in re.finditer(re.escape(kw), desc)
    })
    # Prefer split points that leave a reasonably-sized first segment, to
    # avoid picking a keyword that's still part of transaction 1's own
    # narration (e.g. both 'DEP' and 'UPI' commonly appear together right
    # at the start of a single narration, well before the true boundary).
    MIN_SEGMENT_LEN = 20
    split_points = [p for p in positions if p >= MIN_SEGMENT_LEN][: n - 1]
    if len(split_points) < n - 1:
        parts = desc.split()
        chunk = max(1, len(parts) // n)
        pieces = [" ".join(parts[i * chunk:(i + 1) * chunk]) for i in range(n)]
        pieces[-1] = " ".join(parts[(n - 1) * chunk:])  # last piece takes any remainder
        return pieces
    bounds = [0] + split_points + [len(desc)]
    return [desc[bounds[i]:bounds[i + 1]].strip() for i in range(n)]


def _split_merged_transaction_rows(rows, debit_label, credit_label, balance_label, date_labels, particulars_label):
    """
    Detects a row where Credit (or Debit) contains multiple space-separated
    valid amounts AND Balance contains the same count of valid amounts --
    the signature of two or more distinct transactions having been merged
    into a single row during extraction (typically because OCR failed to
    detect a valid date on the second transaction's own line, so it got
    treated as a continuation instead of a new row -- see the date-column
    recovery logic in _refine_low_confidence_cells for the underlying
    cause). Splits such a row into one row per amount, so each
    transaction's Credit/Debit is individually summable in Excel rather
    than silently excluded from totals as an unparseable merged string.

    Each split row is marked for review regardless of its individual
    validity, since the narration split (see _split_description) and date
    assignment are best-effort and worth a quick visual confirmation even
    though the financial figures themselves are exact.
    """
    if not (debit_label and credit_label and balance_label and particulars_label):
        return rows

    out = []
    for row in rows:
        credit_text = row.get(credit_label, "") or ""
        debit_text = row.get(debit_label, "") or ""

        amount_label, amount_tokens = None, []
        for label, text in ((credit_label, credit_text), (debit_label, debit_text)):
            tokens = text.split()
            if len(tokens) >= 2 and all(_SPLIT_AMOUNT_RE.match(t) for t in tokens):
                amount_label, amount_tokens = label, tokens
                break

        balance_tokens = (row.get(balance_label, "") or "").split()
        valid_balance_tokens = [t for t in balance_tokens if _SPLIT_BALANCE_TOKEN_RE.match(t)]

        if not amount_label or len(valid_balance_tokens) != len(amount_tokens) or len(amount_tokens) < 2:
            out.append(row)
            continue

        n = len(amount_tokens)
        other_label = debit_label if amount_label == credit_label else credit_label
        descs = _split_description(row.get(particulars_label, "") or "", n)
        date_tokens_by_label = {
            dl: re.findall(r"\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}", row.get(dl, "") or "")
            for dl in date_labels
        }

        for i in range(n):
            new_row = dict(row)
            new_row[amount_label] = amount_tokens[i]
            new_row[balance_label] = valid_balance_tokens[i]
            new_row[particulars_label] = descs[i] if i < len(descs) else ""
            if other_label:
                new_row[other_label] = ""
            for dl in date_labels:
                toks = date_tokens_by_label[dl]
                if toks:
                    new_row[dl] = toks[i] if i < len(toks) else toks[-1]
            new_row[_NEEDS_REVIEW_KEY] = True
            out.append(new_row)

    return out


def write_excel(results, output_path, merge_pages=True):
    from openpyxl.styles import PatternFill, Font

    review_fill = PatternFill(start_color="FFF3CD", end_color="FFF3CD", fill_type="solid")
    header_font = Font(bold=True)
    review_col_name = "Needs Review"

    header = _canonical_header(results)

    debit_label = _find_column_label(header, ("debit", "withdrawal"))
    credit_label = _find_column_label(header, ("credit", "deposit"))
    balance_label = _find_column_label(header, ("balance",))
    particulars_label = _find_column_label(header, ("particular", "narration", "description", "detail"))
    date_labels_for_split = [h for h in (header or []) if "date" in h.lower()] or ([header[0]] if header else [])

    for page_num, res in results.items():
        if res.get("type") == "structured":
            res["rows"] = _split_merged_transaction_rows(
                res["rows"], debit_label, credit_label, balance_label, date_labels_for_split, particulars_label
            )

    all_rows_in_doc_order = [
        row
        for page_num in sorted(results.keys())
        for row in (results[page_num]["rows"] if results[page_num].get("type") == "structured" else [])
    ]
    if debit_label is not None and credit_label is not None:
        _correct_balances_via_running_total(all_rows_in_doc_order, debit_label, credit_label, balance_label)

    def row_flag(row):
        return bool(row.get(_NEEDS_REVIEW_KEY, False))

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        if merge_pages:
            all_rows = []
            flags = []
            for page_num in sorted(results.keys()):
                res = results[page_num]
                if res.get("type") != "structured":
                    continue
                for row in res["rows"]:
                    ordered = _row_to_ordered_list(row, header) if header else [
                        v for k, v in row.items() if k != _NEEDS_REVIEW_KEY
                    ]
                    all_rows.append(ordered)
                    flags.append(row_flag(row))

            full_header = (list(header) if header else []) + [review_col_name]
            data_rows = [ordered + [("Yes" if flag else "")] for ordered, flag in zip(all_rows, flags)]
            df = pd.DataFrame(data_rows, columns=full_header)
            sheet_name = "Statement"
            df.to_excel(writer, sheet_name=sheet_name, index=False)
            _style_review_column(writer.sheets[sheet_name], flags, header_font, review_fill)

        else:
            used_names = set()
            for page_num in sorted(results.keys()):
                res = results[page_num]
                if res.get("type") != "structured" or not res["rows"]:
                    continue
                page_header = res["header"]
                flags = [row_flag(r) for r in res["rows"]]
                data_rows = [
                    _row_to_ordered_list(r, page_header) + [("Yes" if flag else "")]
                    for r, flag in zip(res["rows"], flags)
                ]
                full_page_header = list(page_header) + [review_col_name]
                df = pd.DataFrame(data_rows, columns=full_page_header)

                sheet_name = f"Page_{page_num}"[:31]
                base, suffix = sheet_name, 1
                while sheet_name in used_names:
                    sheet_name = f"{base[:28]}_{suffix}"
                    suffix += 1
                used_names.add(sheet_name)
                df.to_excel(writer, sheet_name=sheet_name, index=False)
                _style_review_column(writer.sheets[sheet_name], flags, header_font, review_fill)


def _style_review_column(worksheet, flags, header_font, review_fill):
    """Bold the header row and highlight every flagged row so cells that
    couldn't be confidently OCR'd/repaired are easy to find and manually
    verify, rather than sitting silently indistinguishable from correct
    data."""
    for cell in worksheet[1]:
        cell.font = header_font
    for i, flagged in enumerate(flags, start=2):  # row 1 is the header
        if not flagged:
            continue
        for cell in worksheet[i]:
            cell.fill = review_fill
