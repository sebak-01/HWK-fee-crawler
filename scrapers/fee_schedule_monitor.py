"""
scrapers/fee_schedule_monitor.py

Weekly monitor for all 53 German Handwerkskammern: finds each chamber's
Gebührenverzeichnis (fee schedule for the Meisterprüfung and other services)
and tracks the date it was last updated, so MeisterKompass — or you — gets
notified when a chamber revises its fees.

IMPORTANT — read before trusting the output:
This is a GENERIC, best-effort crawler. Unlike scrapers/hwk_*.py (which have
hand-verified selectors per chamber, built one at a time against confirmed
markup), 53 different chamber websites are too heterogeneous for that here.
This module instead uses a handful of heuristics:

  1. Find the Gebührenverzeichnis link by scanning the homepage (and a
     shallow set of likely nav pages: "Über uns", "Formulare", "Berufliche
     Bildung", "Meister", "Satzungen", ...) for link text/href containing
     keywords like "gebührenverzeichnis", "gebührenordnung", "kostenordnung".
  2. Once found, determine "last updated" by extracting all available date
     signals (PDF metadata, PDF body text, page text, HTTP header) and
     picking the *most recent* date — because PDF metadata often carries a
     stale "D:" date from the original document creation.
  3. Compare against the previous run's data/fee_schedule_status.json and
     flag any chamber whose resolved URL or resolved date changed.

Expect this to work well for many chambers and to misfire or miss on some —
treat a first run like a new course scraper: run --dry-run, spot-check a
handful of chambers, and add per-chamber overrides in CHAMBER_OVERRIDES
below as you find pages this can't handle generically.

Usage:
    python -m scrapers.fee_schedule_monitor                    # full run, writes JSON
    python -m scrapers.fee_schedule_monitor --dry-run           # scrape + log, write nothing
    python -m scrapers.fee_schedule_monitor --chamber hwk-aachen --verbose
    python -m scrapers.fee_schedule_monitor --delay 2.0         # more polite crawling
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
CHAMBERS_JSON = REPO_ROOT / "data" / "chambers_national.json"
STATUS_JSON = DATA_DIR / "fee_schedule_status.json"
CHANGES_JSON = DATA_DIR / "fee_schedule_changes.json"
CHANGE_MARKER = DATA_DIR / ".fee_schedule_has_changes"

USER_AGENT = (
    "Mozilla/5.0 (compatible; MeisterKompassFeeMonitor/1.0; "
    "+https://meisterkompass.eu/about.html)"
)
REQUEST_TIMEOUT = 20
MAX_NAV_PAGES = 12          # shallow-crawl cap per chamber before giving up
MAX_CANDIDATE_LINKS = 6     # how many fee-schedule-looking links to actually open

# Keywords used to *find* the Gebührenverzeichnis link (href or link text).
FEE_LINK_KEYWORDS = [
    "gebuehrenverzeichnis", "gebührenverzeichnis",
    "gebuehrenordnung", "gebührenordnung",
    "kostenordnung", "kostenverzeichnis",
    "gebuehrensatzung", "gebührensatzung",
    "gebuehrentarif", "gebührentarif",
]
# Broader keywords for pages worth a shallow follow if the fee link isn't
# on the homepage (nav labels, not the fee schedule itself).
NAV_FOLLOW_KEYWORDS = [
    "gebühr", "gebuehr", "satzung", "formular", "merkblatt", "recht",
    "über uns", "ueber uns", "meister", "berufliche bildung", "kammer",
    "service", "downloads", "dokumente",
]

DATE_PATTERNS = [
    # "Stand: 03.06.2026" / "Stand 03/2026" / "Stand: Juni 2026"
    re.compile(r"Stand[:\s]*?(\d{1,2}\.\d{1,2}\.\d{2,4})"),
    re.compile(r"gültig\s+ab[:\s]*?(\d{1,2}\.\d{1,2}\.\d{2,4})", re.IGNORECASE),
    re.compile(r"g[üu]ltig\s+ab\s+dem[:\s]*?(\d{1,2}\.\d{1,2}\.\d{2,4})", re.IGNORECASE),
]

MONTHS_DE = {
    "januar": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4, "mai": 5,
    "juni": 6, "juli": 7, "august": 8, "september": 9, "oktober": 10,
    "november": 11, "dezember": 12,
}
DATE_MONTHNAME_RE = re.compile(
    r"Stand[:\s]*?(\d{1,2})\.?\s*("
    r"januar|februar|märz|maerz|april|mai|juni|juli|august|september|oktober|november|dezember"
    r")\s*(\d{4})",
    re.IGNORECASE,
)

# Per-chamber overrides, keyed by slug (see data/chambers_national.json).
# Fill this in as you discover chambers the generic heuristics can't handle —
# e.g. {"hwk-example": {"fee_schedule_url": "https://.../gebuehren.pdf"}}
CHAMBER_OVERRIDES: dict[str, dict] = {}


@dataclass
class ChamberResult:
    slug: str
    name: str
    website: str
    fee_schedule_url: str | None = None
    last_updated: str | None = None          # ISO date, if determined
    detection_method: str | None = None      # "pdf_metadata" | "page_text" | "http_header" | None
    checked_at: str = ""
    error: str | None = None


def load_chambers(only_slug: str | None = None) -> list[dict]:
    chambers = json.loads(CHAMBERS_JSON.read_text(encoding="utf-8"))
    if only_slug:
        chambers = [c for c in chambers if c["slug"] == only_slug]
        if not chambers:
            raise SystemExit(f"Unknown chamber slug: {only_slug!r}")
    return chambers


class Crawler:
    def __init__(self, delay: float = 1.0, verbose: bool = False):
        self.delay = delay
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "de-DE,de;q=0.9"})
        self.verbose = verbose

    def get(self, url: str, **kwargs) -> requests.Response | None:
        try:
            time.sleep(self.delay)
            r = self.session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True, **kwargs)
            r.raise_for_status()
            return r
        except requests.RequestException as exc:
            logger.debug("GET failed %s: %s", url, exc)
            return None

    # ------------------------------------------------------------------
    # Step 1: locate the Gebührenverzeichnis link
    # ------------------------------------------------------------------

    def find_fee_schedule_urls(self, website: str) -> list[str]:
        home = self.get(website)
        if home is None:
            return []

        soup = BeautifulSoup(home.text, "html.parser")
        direct = self._scan_for_fee_links(soup, home.url)
        if direct:
            return direct

        nav_urls = self._collect_nav_candidates(soup, home.url)
        for nav_url in nav_urls[:MAX_NAV_PAGES]:
            page = self.get(nav_url)
            if page is None:
                continue
            if "html" not in (page.headers.get("Content-Type") or "").lower():
                continue
            nav_soup = BeautifulSoup(page.text, "html.parser")
            found = self._scan_for_fee_links(nav_soup, page.url)
            if found:
                return found
        return []

    def _scan_for_fee_links(self, soup: BeautifulSoup, base_url: str) -> list[str]:
        hits: list[str] = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            text = a.get_text(" ", strip=True).lower()
            haystack = f"{href.lower()} {text}"
            if any(kw in haystack for kw in FEE_LINK_KEYWORDS):
                full = urljoin(base_url, href)
                if full not in hits:
                    hits.append(full)
        # Prefer PDFs (usually the authoritative fee schedule itself).
        hits.sort(key=lambda u: 0 if u.lower().endswith(".pdf") else 1)
        return hits[:MAX_CANDIDATE_LINKS]

    def _collect_nav_candidates(self, soup: BeautifulSoup, base_url: str) -> list[str]:
        candidates: list[str] = []
        base_host = urlparse(base_url).netloc
        for a in soup.find_all("a", href=True):
            href = a["href"]
            text = a.get_text(" ", strip=True).lower()
            if not any(kw in text for kw in NAV_FOLLOW_KEYWORDS):
                continue
            full = urljoin(base_url, href)
            if urlparse(full).netloc != base_host:
                continue  # stay on the chamber's own site
            if full not in candidates:
                candidates.append(full)
        return candidates

    # ------------------------------------------------------------------
    # Step 2: determine "last updated" for a found URL
    # ------------------------------------------------------------------

    def resolve_last_updated(self, url: str) -> tuple[str | None, str | None]:
        """Returns (iso_date_or_None, method_or_None). Picks newest date across all sources."""
        resp = self.get(url)
        if resp is None:
            return None, None

        content_type = (resp.headers.get("Content-Type") or "").lower()
        is_pdf = url.lower().endswith(".pdf") or "application/pdf" in content_type

        candidates: list[tuple[str, str]] = []

        if is_pdf:
            meta_date = self._pdf_mod_date(resp.content)
            if meta_date:
                candidates.append((meta_date, "pdf_metadata"))
            pdf_text = self._pdf_extract_text(resp.content)
            if pdf_text:
                text_date = self._text_stand_date(pdf_text)
                if text_date:
                    candidates.append((text_date, "pdf_text"))
        else:
            text_date = self._text_stand_date(resp.text)
            if text_date:
                candidates.append((text_date, "page_text"))

        header_date = self._http_last_modified(resp)
        if header_date:
            candidates.append((header_date, "http_header"))

        if candidates:
            candidates.sort(reverse=True)
            return candidates[0]

        return None, None

    def _pdf_mod_date(self, content: bytes) -> str | None:
        try:
            from pypdf import PdfReader  # optional dependency; see requirements.txt
        except ImportError:
            logger.warning("pypdf not installed — cannot read PDF metadata. `pip install pypdf`.")
            return None
        try:
            reader = PdfReader(io.BytesIO(content))
            meta = reader.metadata or {}
            raw = meta.get("/ModDate") or meta.get("/CreationDate")
            if not raw:
                return None
            # PDF date format: "D:20260603120000+02'00'"
            m = re.match(r"D:(\d{4})(\d{2})(\d{2})", raw)
            if m:
                y, mo, d = (int(x) for x in m.groups())
                return date(y, mo, d).isoformat()
        except Exception as exc:
            logger.debug("Could not parse PDF metadata: %s", exc)
        return None

    def _pdf_extract_text(self, content: bytes) -> str | None:
        try:
            from pypdf import PdfReader
        except ImportError:
            return None
        try:
            reader = PdfReader(io.BytesIO(content))
            parts: list[str] = []
            for page in reader.pages[:3]:
                page_text = page.extract_text()
                if page_text:
                    parts.append(page_text)
            return "\n".join(parts)
        except Exception as exc:
            logger.debug("Could not extract PDF text: %s", exc)
        return None

    def _text_stand_date(self, text: str) -> str | None:
        window = text[:20000]  # "Stand:" usually appears near the top/footer of the first screen
        for pat in DATE_PATTERNS:
            m = pat.search(window)
            if m:
                try:
                    d, mth, y = m.group(1).split(".")
                    y = "20" + y if len(y) == 2 else y
                    return date(int(y), int(mth), int(d)).isoformat()
                except ValueError:
                    continue
        m = DATE_MONTHNAME_RE.search(window)
        if m:
            day, month_name, year = m.groups()
            month = MONTHS_DE.get(month_name.lower())
            if month:
                try:
                    return date(int(year), month, int(day)).isoformat()
                except ValueError:
                    pass
        return None

    def _http_last_modified(self, resp: requests.Response) -> str | None:
        raw = resp.headers.get("Last-Modified")
        if not raw:
            return None
        try:
            dt = datetime.strptime(raw, "%a, %d %b %Y %H:%M:%S %Z")
            return dt.date().isoformat()
        except ValueError:
            return None

    # ------------------------------------------------------------------

    def check_chamber(self, chamber: dict) -> ChamberResult:
        slug, name, website = chamber["slug"], chamber["name"], chamber["website"]
        result = ChamberResult(slug=slug, name=name, website=website,
                                checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z")
        try:
            override = CHAMBER_OVERRIDES.get(slug, {})
            if override.get("fee_schedule_url"):
                candidate_urls = [override["fee_schedule_url"]]
            else:
                candidate_urls = self.find_fee_schedule_urls(website)

            if not candidate_urls:
                result.error = "Gebührenverzeichnis-Link nicht gefunden"
                return result

            best_date: str | None = None
            best_url: str | None = None
            best_method: str | None = None
            for url in candidate_urls[:MAX_CANDIDATE_LINKS]:
                date_val, method = self.resolve_last_updated(url)
                if date_val and (best_date is None or date_val > best_date):
                    best_date = date_val
                    best_url = url
                    best_method = method

            result.fee_schedule_url = best_url or candidate_urls[0]
            result.last_updated = best_date
            result.detection_method = best_method
            if not best_date:
                result.error = "Datum konnte nicht bestimmt werden"
        except Exception as exc:  # noqa: BLE001 — keep the run going for other chambers
            logger.exception("Unhandled error checking %s", slug)
            result.error = f"Unerwarteter Fehler: {exc}"
        return result


# ----------------------------------------------------------------------
# Diffing against the previous run
# ----------------------------------------------------------------------

def _load_previous_status() -> dict[str, dict]:
    if not STATUS_JSON.exists():
        return {}
    data = json.loads(STATUS_JSON.read_text(encoding="utf-8"))
    return {r["slug"]: r for r in data}


def diff_results(previous: dict[str, dict], current: list[ChamberResult]) -> list[dict]:
    changes = []
    for r in current:
        prev = previous.get(r.slug)
        if prev is None:
            continue  # first time we've seen this chamber — not a "change"
        if prev.get("last_updated") != r.last_updated or prev.get("fee_schedule_url") != r.fee_schedule_url:
            changes.append({
                "slug": r.slug,
                "name": r.name,
                "previous_date": prev.get("last_updated"),
                "new_date": r.last_updated,
                "previous_url": prev.get("fee_schedule_url"),
                "new_url": r.fee_schedule_url,
                "detected_at": datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z",
            })
    return changes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Weekly Gebührenverzeichnis monitor for all 53 Handwerkskammern.")
    parser.add_argument("--chamber", help="Only check one chamber slug (see data/chambers_national.json).")
    parser.add_argument("--dry-run", action="store_true", help="Crawl and log, but write nothing.")
    parser.add_argument("--delay", type=float, default=1.0, help="Seconds between requests (politeness).")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    chambers = load_chambers(args.chamber)
    crawler = Crawler(delay=args.delay, verbose=args.verbose)

    results: list[ChamberResult] = []
    for i, chamber in enumerate(chambers, 1):
        logger.info("[%d/%d] %s (%s)", i, len(chambers), chamber["name"], chamber["slug"])
        r = crawler.check_chamber(chamber)
        if r.error:
            logger.warning("  -> %s", r.error)
        else:
            logger.info("  -> %s | Stand: %s (%s)", r.fee_schedule_url, r.last_updated, r.detection_method)
        results.append(r)

    ok = sum(1 for r in results if r.last_updated)
    logger.info("Done: %d/%d chambers resolved a date.", ok, len(results))

    if args.dry_run:
        logger.info("Dry run — nothing written.")
        return 0

    previous = _load_previous_status()
    changes = diff_results(previous, results)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATUS_JSON.write_text(
        json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )

    if not CHANGES_JSON.exists():
        CHANGES_JSON.write_text("[]\n", encoding="utf-8")

    if changes:
        history = json.loads(CHANGES_JSON.read_text(encoding="utf-8"))
        history.extend(changes)
        CHANGES_JSON.write_text(json.dumps(history, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        logger.info("*** %d Gebührenverzeichnis-Änderung(en) erkannt ***", len(changes))
        for c in changes:
            logger.info("  %s: %s -> %s", c["name"], c["previous_date"], c["new_date"])

    errors = [r for r in results if r.error]

    if changes or errors:
        marker_lines: list[str] = []
        if changes:
            marker_lines.append("## Änderungen")
            for c in changes:
                marker_lines.append(f"- {c['name']} ({c['slug']}): {c['previous_date']} -> {c['new_date']}")
        if errors:
            marker_lines.append("## Fehler")
            for r in errors:
                marker_lines.append(f"- {r.name} ({r.slug}): {r.error}")
        marker_lines.append(f"\n{ok}/{len(results)} Daten bestimmt. {len(changes)} Änderungen, {len(errors)} Fehler.")
        CHANGE_MARKER.write_text("\n".join(marker_lines) + "\n", encoding="utf-8")
    elif CHANGE_MARKER.exists():
        CHANGE_MARKER.unlink()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
