# hwk-fee-schedule-monitor

Weekly monitor for the 53 German Handwerkskammern: finds each chamber's
**Gebührenverzeichnis** (fee schedule) and tracks the date it was last
updated — so you get notified when a chamber revises its fees.

Built as a companion to [MeisterKompass](https://meisterkompass.eu), but kept
in its own repo: different cadence (weekly vs. daily), different confidence
level (generic heuristics across 53 sites vs. MeisterKompass's hand-verified
per-chamber scrapers), no shared code.

---

## What it does

```
53 chamber websites ──▶ find Gebührenverzeichnis link ──▶ resolve "last updated"
                                                              │
                                                              ▼
                                          data/fee_schedule_status.json (current state)
                                          data/fee_schedule_changes.json (change log)
```

1. **Find the link.** Scans each chamber's homepage for a link whose text or
   URL contains something like "Gebührenverzeichnis", "Gebührenordnung",
   "Kostenordnung", etc. If it's not on the homepage, shallow-crawls a
   handful of likely nav pages ("Über uns", "Formulare", "Satzungen", ...)
   and tries again there.
2. **Resolve "last updated"**, in priority order:
   - PDF metadata (`/ModDate` on the PDF itself) — most reliable, most common
     since most chambers just link a PDF.
   - "Stand: DD.MM.YYYY" / "gültig ab ..." text on the page.
   - HTTP `Last-Modified` response header, as a last resort.
3. **Diff against last week's run.** Any chamber whose resolved URL or date
   changed gets logged to `data/fee_schedule_changes.json`.
4. **Notify.** The weekly GitHub Action opens a GitHub Issue listing what
   changed, so you get pinged via GitHub's own notifications (if you're
   watching the repo).

## ⚠️ Accuracy — read this before trusting the output

This is a **generic, best-effort crawler**, not a set of hand-verified
per-chamber scrapers. 53 different websites are too heterogeneous for that.
Expect a real chunk of the 53 chambers to come back with
`"error": "Gebührenverzeichnis-Link nicht gefunden"` or
`"error": "Datum konnte nicht bestimmt werden"` on the first few runs.

Treat this the same way you'd treat a new course scraper: run it, look at
what came back, and fix the chambers it got wrong — usually by adding a
direct override rather than fighting the generic heuristic:

```python
# scrapers/fee_schedule_monitor.py
CHAMBER_OVERRIDES: dict[str, dict] = {
    "hwk-example": {"fee_schedule_url": "https://www.hwk-example.de/gebuehren.pdf"},
}
```

Chamber slugs and websites live in `data/chambers_national.json`.

## Usage

```bash
pip install -r requirements.txt

# One chamber, verbose, nothing written — good for checking a fix
python -m scrapers.fee_schedule_monitor --chamber hwk-koblenz --verbose --dry-run

# Everything, nothing written
python -m scrapers.fee_schedule_monitor --dry-run

# Full run — writes data/fee_schedule_status.json (+ changes.json if anything moved)
python -m scrapers.fee_schedule_monitor

# Slower / more polite crawl (default is 1s between requests)
python -m scrapers.fee_schedule_monitor --delay 2.0
```

### CLI flags

| Flag         | Effect                                                        |
|--------------|----------------------------------------------------------------|
| `--chamber`  | Only check one chamber (slug from `data/chambers_national.json`) |
| `--dry-run`  | Crawl and log, write nothing                                   |
| `--delay`    | Seconds between requests (politeness), default `1.0`           |
| `--verbose`, `-v` | Debug-level logging                                        |

## Output files

- **`data/chambers_national.json`** — static list of all 53 chambers (slug,
  name, website), sourced from the [ZDH address list](https://www.zdh.de/ueber-uns/organisationen-des-handwerks/handwerkskammern/adressen-der-handwerkskammern/).
- **`data/fee_schedule_status.json`** — current state per chamber: resolved
  URL, resolved date, detection method (`pdf_metadata` / `page_text` /
  `http_header`), timestamp, and any error.
- **`data/fee_schedule_changes.json`** — append-only log of every detected
  change (previous date/URL → new date/URL, with a timestamp).
- **`data/.fee_schedule_has_changes`** — marker file the Action checks to
  decide whether to open an issue; present only when the latest run found a
  change, deleted otherwise.

## CI

`.github/workflows/fee_schedule_monitor.yml`:

- Runs every **Monday 05:00 UTC** (+ manual trigger via "Run workflow").
- Commits the updated JSON files if anything changed.
- Opens a GitHub Issue (label `gebuehren-update`) when a chamber's fee
  schedule date or URL moved since the last run.

First-run setup:
- Create the `gebuehren-update` label in the repo (Issues → Labels), or
  remove the `--label` argument from the `gh issue create` step in the
  workflow so it doesn't fail on a missing label.
- The workflow needs `issues: write` and `contents: write` permissions
  (already set in the YAML) — no extra secrets required.

Want email or Slack instead of/in addition to a GitHub Issue? Add a step
after "Open issue if changes were detected" that reads
`data/.fee_schedule_has_changes` and `curl`s a Slack webhook or sends mail —
the marker file already has the human-readable summary.

## Roadmap / known gaps

- [ ] Spot-check all 53 chambers' resolved URLs after the first live run;
      fill in `CHAMBER_OVERRIDES` for the ones the generic heuristic misses.
- [ ] Some chambers may gate their Gebührenverzeichnis behind a search form
      or JS-rendered page (same class of problem as HWK Kassel's BBZ Mitte
      in MeisterKompass) — those will need a raw-HTML or network-capture
      approach rather than BeautifulSoup.
- [ ] Consider Slack/email notification as an alternative to GitHub Issues.
