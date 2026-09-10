# PDF Bank Statement → Excel Converter

## How extraction works (bank-agnostic)

The extractor doesn't hardcode any single bank's layout. Per page:

1. Get words from the PDF's real text layer, or via Tesseract OCR if the
   page has zero extractable characters (scanned / vector-flattened text).
2. Group words into visual lines by vertical position.
3. **Find the column header — across up to two physical lines.** Some
   banks wrap a column label onto a second line (e.g. "Cheque" / "No/Reference"
   stacked). The header line with the highest keyword score is used as the
   anchor; if the very next line is close vertically and doesn't look like
   a transaction row, its words are folded into the header too, matched to
   existing columns by horizontal (x-range) overlap.
4. **Build column boundaries generically.** Adjacent header words merge
   into one column label only when at most one of the two is itself a
   recognized column keyword — this correctly joins two-word labels like
   "Post" + "Date" or "Chq" + ".No" into one column, while still keeping
   two genuine, distinct column keywords (like "Cheque No" and
   "Withdrawals") from merging into each other even if OCR noise narrows
   the visual gap between them. When a label is stacked across two physical
   lines and OCR read one line cleanly but garbled the other (e.g. "Cheque"
   misread as nonsense while "No/Reference" below it read fine), only the
   line that actually matches a recognized keyword is kept, instead of
   concatenating OCR noise into the final label.
5. **Support multiple date-like columns.** A statement may have both a
   Post Date and a Value Date column; every column whose label contains
   "date" gets the same date-reconciliation treatment, while the leftmost
   one is used as the anchor for "does this line start a new transaction?".
6. Walk the lines below the header: a line whose primary-date column holds
   an actual date (or B/F, or a partial/OCR-damaged date) starts a new
   transaction row; anything else is a continuation, merged into the
   current row so wrapped narration stays in one cell.
7. Every non-Particulars column is reconciled — stray words that land in
   the wrong column (common when a short header label sits inside a much
   wider real column) are pushed back to Particulars in reading order, and
   amount values with a Cr/Dr suffix OCR'd with no space before it (e.g.
   "6,55,298.37CR") are still recognized as valid amounts rather than
   treated as stray text. Page-footer noise ("Page no. 3") that ends up
   glued onto the same OCR line as the last real transaction is stripped
   out specifically, without dropping that transaction's real data. Stray
   single-character OCR artifacts (e.g. a faint table gridline misread as
   a "|" character) are filtered out before they can glue onto and corrupt
   an adjacent column.
8. Column x-boundaries **and label strings** are locked in from the first
   page with a confident header detection and reused on later pages, so
   alignment stays consistent across a long document even where OCR noise
   makes a later page's own header detection less reliable — including the
   case where a single-word header reads fine structurally (still scores as
   a column) but the text itself is misread (e.g. "Debit" OCR'd as
   "Psion"), which would otherwise silently break that page's data from
   lining up with the rest of the document when pages are merged. If a
   page's header is missing a column entirely, the locked version is
   inserted back by position.

This has been validated against two structurally different real bank
statements (a 6-column single-date-column layout, and a 7-column
dual-date-column layout with a wrapped two-line header) without any
per-bank special-casing in the code.

## Known limitation: OCR reading accuracy

For statements with no real text layer, transaction *structure* (rows,
columns, narration boundaries) is handled generically as above. On top of
that, every date and amount/Balance cell is validated against its expected
format; a cell that looks wrong or was read with low confidence gets
individually re-cropped from the source page image and re-OCR'd in
isolation, with a whitelist matched to what that column can legitimately
contain. This catches things a generic OCR pass alone misses — e.g. a
leading day-digit or a Balance's lakhs-group digits that the full-page pass
failed to detect as any glyph at all (not just misread it), or a phantom
duplicate "echo" reading of the same text.

This can't guarantee 100% accuracy on a badly degraded scan — a small
number of cells can still resist every automated recovery attempt. Rather
than silently leaving a wrong-looking value in the sheet, any row containing
such a cell is marked in a **"Needs Review"** column and highlighted in
amber, so it's easy to find and manually verify rather than trust blindly.
Minor narration misreads (e.g. "UPI/CR" read as "BIC R") aren't corrected
by this mechanism, since free-text narration has no fixed format to
validate against — only the structured date/amount columns are.

## Web app: local run
```bash
cd backend
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
python3 app.py
```
Open http://localhost:5000. Requires Tesseract OCR installed system-wide.

## Windows desktop app
See `BUILD_WINDOWS_EXE.md`.

## Deploying the web app for a limited audience (cloud)
Optional HTTP Basic Auth (`APP_USERNAME`/`APP_PASSWORD` env vars), a
`robots.txt`, and automatic deletion of uploaded/generated files after
`JOB_TTL_SECONDS` are built in. See the Dockerfile for a Render/Railway/
Fly.io-ready container.
