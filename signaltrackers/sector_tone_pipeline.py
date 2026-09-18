"""
Sector Management Tone Pipeline — US-123.1

Fetches S&P 500 company 8-K earnings call filings from SEC EDGAR (no API key
required), scores management sentiment per filing using FinBERT
(ProsusAI/finbert from Hugging Face), aggregates scores at GICS sector level
per quarter, and stores up to 4 quarters of trend history.

Caching:
- Results stored in data/sector_tone_cache.json
- Cache is read by Flask context processor on every request
- Cache is refreshed by update_sector_management_tone() in run_data_collection()

Graceful degradation:
- If EDGAR fetch fails for a company, that company is skipped (non-fatal)
- If FinBERT is unavailable (transformers not installed), update function raises
  an ImportError with a clear message
- If all companies in a sector fail, sector is included with a 0.0 (neutral) score

Docker / image-size note:
- This module requires `transformers` + `torch` (CPU-only sufficient for inference).
- torch CPU-only: ~250MB; transformers: ~50MB; ProsusAI/finbert model: ~500MB.
- Total added to image: ~800MB. Consider a separate ML container for Phase 6+.
- Install: pip install transformers torch --index-url https://download.pytorch.org/whl/cpu
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# Score threshold for tone classification (PM decision, 2026-02-25 — validate
# against actual score distribution; tighten to ±0.10 if >60% land as neutral)
SCORE_THRESHOLD = 0.15

# Cache file path
CACHE_FILE = Path(__file__).parent / 'data' / 'sector_tone_cache.json'

# Directory for saved quarterly report texts (JSONL)
QUARTERLY_REPORTS_DIR = Path(__file__).parent / 'data' / 'quarterly_reports'

# All 11 GICS sector names (canonical ordering)
GICS_SECTORS: list[str] = [
    "Information Technology",
    "Health Care",
    "Financials",
    "Consumer Discretionary",
    "Communication Services",
    "Industrials",
    "Consumer Staples",
    "Energy",
    "Materials",
    "Real Estate",
    "Utilities",
]

# Short name mapping (PM canonical list — only 4 sectors abbreviated; ≤18 chars)
SHORT_NAME_MAP: dict[str, str] = {
    "Consumer Discretionary":  "Cons. Discretionary",
    "Consumer Staples":        "Cons. Staples",
    "Communication Services":  "Comm. Services",
    "Information Technology":  "Technology",
}

# Representative S&P 500 companies by GICS sector (used to scope EDGAR queries)
SP500_BY_SECTOR: dict[str, list[str]] = {
    "Information Technology": [
        "AAPL", "MSFT", "NVDA", "AVGO", "CRM", "ORCL", "AMD", "INTC", "AMAT", "LRCX",
    ],
    "Health Care": [
        "UNH", "JNJ", "LLY", "ABBV", "MRK", "TMO", "ABT", "DHR", "BMY", "AMGN",
    ],
    "Financials": [
        "JPM", "V", "MA", "BAC", "WFC", "GS", "MS", "BLK", "SCHW", "AXP",
    ],
    "Consumer Discretionary": [
        "AMZN", "TSLA", "HD", "MCD", "NKE", "LOW", "SBUX", "TJX", "BKNG", "ORLY",
    ],
    "Communication Services": [
        "META", "GOOGL", "NFLX", "DIS", "TMUS", "VZ", "T", "CMCSA", "EA", "TTWO",
    ],
    "Industrials": [
        "UPS", "CAT", "BA", "HON", "GE", "RTX", "LMT", "DE", "MMM", "FDX",
    ],
    "Consumer Staples": [
        "PG", "KO", "PEP", "COST", "WMT", "PM", "MO", "CL", "GIS", "KMB",
    ],
    "Energy": [
        "XOM", "CVX", "SLB", "EOG", "COP", "MPC", "PSX", "VLO", "OXY", "HAL",
    ],
    "Materials": [
        "LIN", "APD", "SHW", "ECL", "NEM", "FCX", "NUE", "ALB", "DD", "PPG",
    ],
    "Real Estate": [
        "PLD", "AMT", "EQIX", "PSA", "O", "SPG", "DLR", "WELL", "EQR", "AVB",
    ],
    "Utilities": [
        "NEE", "DUK", "SO", "D", "AEP", "EXC", "SRE", "PCG", "XEL", "ED",
    ],
}

# EDGAR endpoints (HTTPS only — no API key required)
_EDGAR_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_EDGAR_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
_EDGAR_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
_EDGAR_ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"

# HTTP timeout for EDGAR requests (seconds)
_EDGAR_TIMEOUT = 30

# Number of filings to process per company (bounds pipeline runtime)
_MAX_FILINGS_PER_COMPANY = 2

# Max chars passed to FinBERT per text segment (~512 tokens at 4 chars/token)
_MAX_FINBERT_INPUT_CHARS = 2048

# Rate-limit pause between EDGAR requests (seconds — SEC rate limit: 10 req/s)
_EDGAR_RATE_LIMIT_PAUSE = 0.15

# Sort priority for tone: positive → neutral → negative
_TONE_SORT_ORDER: dict[str, int] = {"positive": 0, "neutral": 1, "negative": 2}

# ---------------------------------------------------------------------------
# Tone classification
# ---------------------------------------------------------------------------


def _classify_tone(score: float) -> str:
    """Classify an aggregate FinBERT score to a tone label.

    Uses SCORE_THRESHOLD constant (not a hardcoded literal) per spec AC.

    Returns:
        'positive' if score > SCORE_THRESHOLD
        'negative' if score < -SCORE_THRESHOLD
        'neutral'  otherwise (inclusive of exact ±SCORE_THRESHOLD boundaries)
    """
    if score > SCORE_THRESHOLD:
        return "positive"
    elif score < -SCORE_THRESHOLD:
        return "negative"
    return "neutral"


def _get_quarter_label(dt: datetime) -> tuple[str, int]:
    """Return (quarter_label, year) for the given datetime.

    Q1 = Jan–Mar, Q2 = Apr–Jun, Q3 = Jul–Sep, Q4 = Oct–Dec.
    """
    month = dt.month
    year = dt.year
    if month <= 3:
        return ("Q1", year)
    elif month <= 6:
        return ("Q2", year)
    elif month <= 9:
        return ("Q3", year)
    return ("Q4", year)


def _quarter_date_range(quarter: str, year: int) -> tuple[str, str]:
    """Return (startdt, enddt) ISO strings for a quarter."""
    q_ranges: dict[str, tuple[int, int, int, int]] = {
        "Q1": (1, 1, 3, 31),
        "Q2": (4, 1, 6, 30),
        "Q3": (7, 1, 9, 30),
        "Q4": (10, 1, 12, 31),
    }
    sm, sd, em, ed = q_ranges[quarter]
    return (f"{year}-{sm:02d}-{sd:02d}", f"{year}-{em:02d}-{ed:02d}")


# ---------------------------------------------------------------------------
# SEC EDGAR helpers
# ---------------------------------------------------------------------------


def _fetch_edgar_ticker_map() -> dict[str, str]:
    """Fetch the SEC EDGAR company ticker → zero-padded CIK mapping.

    Returns dict mapping uppercase ticker → 10-digit zero-padded CIK string.
    Makes one HTTPS request to EDGAR (no API key).
    """
    headers = {"User-Agent": "SignalTrackers financial-research@signaltrackers.app"}
    resp = requests.get(_EDGAR_TICKERS_URL, timeout=_EDGAR_TIMEOUT, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    result: dict[str, str] = {}
    for entry in data.values():
        ticker = str(entry.get("ticker", "")).upper().strip()
        cik_raw = entry.get("cik_str", "")
        cik = str(cik_raw).zfill(10)
        if ticker:
            result[ticker] = cik
    return result


def _strip_html(raw: str) -> str:
    """Strip HTML/SGML markup from an EDGAR document, returning plain text.

    EDGAR documents use an SGML envelope followed by HTML body content.
    Passing raw HTML to FinBERT wastes the 512-token window on markup
    instead of prose — this helper removes tags and decodes HTML entities
    using only Python builtins (no extra dependency required).
    """
    # Remove entire SGML envelope (everything before the first <html or <body)
    body_match = re.search(r'<(?:html|body)\b', raw, re.IGNORECASE)
    if body_match:
        raw = raw[body_match.start():]

    # Remove SGML envelope headers (e.g. <DOCUMENT>, <TYPE>EX-99.1, etc.)
    text = re.sub(r'<(?!/?[a-zA-Z])[^>]*>', '', raw)
    # Strip HTML tags
    text = re.sub(r'<[^>]+>', ' ', text)
    # Decode HTML entities (e.g. &amp; &nbsp; &#160;)
    text = html.unescape(text)
    # Collapse whitespace
    text = re.sub(r'\s+', ' ', text).strip()

    # Remove exhibit header artifacts (e.g. "EX-99.1 2 filename.htm EX-99.1 Document")
    text = re.sub(
        r'^EX-\d+\.\d+\s+\d+\s+\S+\.htm\s+EX-\d+\.\d+\s+Document\s*',
        '', text, flags=re.IGNORECASE,
    )
    # Remove "Exhibit 99.1" header line
    text = re.sub(r'^Exhibit\s+\d+\.\d+\s*', '', text, flags=re.IGNORECASE)

    return text.strip()


def _extract_ex991_url(index_html: str, int_cik: int, accession_clean: str) -> Optional[str]:
    """Extract the EX-99.1 exhibit URL from a filing index HTML page.

    Searches table rows for an EX-99.1 entry and returns the full HTTPS URL
    to the exhibit document. Returns None if no EX-99.1 row is found.
    """
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', index_html, re.DOTALL | re.IGNORECASE)
    for row in rows:
        if re.search(r'\bEX-99\.1\b', row, re.IGNORECASE):
            hrefs = re.findall(
                r'href="(/Archives/edgar/data/[^"]+\.htm[^"]*)"',
                row,
                re.IGNORECASE,
            )
            if hrefs:
                return f"https://www.sec.gov{hrefs[0]}"
    return None


def _fetch_recent_8k_filing_texts(
    cik: str,
    quarter: str,
    year: int,
) -> list[dict]:
    """Fetch text and metadata from recent 8-K earnings filings for a company.

    Uses the EDGAR submissions API (company-specific by design) to get the
    8-K filing list for the company. Filters for Item 2.02 ("Results of
    Operations and Financial Condition") to target earnings releases only,
    then fetches the EX-99.1 press release text from the filing index.

    Returns a list of dicts (up to _MAX_FILINGS_PER_COMPANY), each containing:
        - text: full untruncated filing text
        - accession_number: SEC accession number
        - filing_date: ISO date string
    Returns [] on any error.
    """
    headers = {"User-Agent": "SignalTrackers financial-research@signaltrackers.app"}
    startdt, enddt = _quarter_date_range(quarter, year)

    # Step 1: Fetch company-specific submissions JSON (zero-padded 10-digit CIK)
    submissions_url = _EDGAR_SUBMISSIONS_URL.format(cik=cik)
    try:
        resp = requests.get(submissions_url, timeout=_EDGAR_TIMEOUT, headers=headers)
        if resp.status_code == 429:
            logger.warning("EDGAR rate limited (429) for CIK %s — skipping", cik)
            return []
        resp.raise_for_status()
        submissions = resp.json()
    except Exception as exc:
        logger.warning("EDGAR submissions fetch failed for CIK %s: %s", cik, exc)
        return []

    time.sleep(_EDGAR_RATE_LIMIT_PAUSE)

    # Step 2: Filter 8-K filings with Item 2.02 (earnings) within the quarter
    recent = submissions.get("filings", {}).get("recent", {})
    accession_numbers = recent.get("accessionNumber", [])
    forms = recent.get("form", [])
    filing_dates = recent.get("filingDate", [])
    primary_docs = recent.get("primaryDocument", [])
    items_list = recent.get("items", [])

    matching: list[tuple[str, str, str]] = []  # (accession_no, primary_doc, filing_date)
    for i, (accession, form, filing_date, primary_doc) in enumerate(zip(
        accession_numbers, forms, filing_dates, primary_docs
    )):
        if form != "8-K":
            continue
        if not (startdt <= filing_date <= enddt):
            continue
        # Item 2.02 = "Results of Operations and Financial Condition" (earnings)
        items = items_list[i] if i < len(items_list) else ""
        if "2.02" not in items:
            logger.debug("8-K %s has items=%s (no 2.02) — skipping", accession, items)
            continue
        matching.append((accession, primary_doc, filing_date))
        if len(matching) >= _MAX_FILINGS_PER_COMPANY:
            break

    if not matching:
        return []

    results: list[dict] = []
    int_cik = int(cik)

    for accession, primary_doc, filing_date in matching:
        accession_clean = accession.replace("-", "")
        index_url = (
            f"{_EDGAR_ARCHIVES_BASE}/{int_cik}/{accession_clean}/{accession}-index.htm"
        )

        # Step 3: Fetch filing index HTML
        try:
            idx_resp = requests.get(index_url, timeout=_EDGAR_TIMEOUT, headers=headers)
            idx_resp.raise_for_status()
            index_html = idx_resp.text
        except Exception as exc:
            logger.warning("EDGAR index fetch failed for %s: %s", index_url, exc)
            time.sleep(_EDGAR_RATE_LIMIT_PAUSE)
            continue

        time.sleep(_EDGAR_RATE_LIMIT_PAUSE)

        # Step 4: Locate EX-99.1 and fetch earnings press release text
        # Skip filings without EX-99.1 (non-earnings 8-Ks like proxy votes,
        # executive appointments, etc.)
        ex99_url = _extract_ex991_url(index_html, int_cik, accession_clean)
        if not ex99_url:
            logger.debug("No EX-99.1 in 8-K %s — skipping (not an earnings release)", accession)
            time.sleep(_EDGAR_RATE_LIMIT_PAUSE)
            continue

        text = ""
        try:
            doc_resp = requests.get(ex99_url, timeout=_EDGAR_TIMEOUT, headers=headers)
            doc_resp.raise_for_status()
            text = _strip_html(doc_resp.text)
        except Exception as exc:
            logger.warning("EDGAR EX-99.1 fetch failed for %s: %s", ex99_url, exc)

        if text and text.strip():
            results.append({
                "text": text,
                "accession_number": accession,
                "filing_date": filing_date,
            })

        time.sleep(_EDGAR_RATE_LIMIT_PAUSE)

    return results


# ---------------------------------------------------------------------------
# Quarterly report fetching (standalone — no FinBERT dependency)
# ---------------------------------------------------------------------------


def fetch_quarterly_reports(
    quarter: Optional[str] = None,
    year: Optional[int] = None,
) -> list[dict]:
    """Fetch quarterly 8-K filing texts for all sectors and save to JSONL.

    This function can be called independently of the FinBERT scoring pipeline,
    e.g. to collect training data for a local model. No ML dependencies required.

    Args:
        quarter: Quarter label (Q1–Q4). Defaults to current quarter.
        year: Year. Defaults to current year.

    Returns:
        List of dicts, one per filing, each containing:
            ticker, sector, quarter, year, filing_date, accession_number, text
    """
    current_dt = datetime.utcnow()
    if quarter is None or year is None:
        quarter, year = _get_quarter_label(current_dt)

    logger.info("Fetching quarterly reports for %s %d", quarter, year)

    try:
        ticker_map = _fetch_edgar_ticker_map()
        logger.info("Loaded %d tickers from EDGAR", len(ticker_map))
    except Exception as exc:
        logger.error("Failed to load EDGAR ticker map: %s — aborting fetch", exc)
        return []

    all_reports: list[dict] = []

    for sector in GICS_SECTORS:
        tickers = SP500_BY_SECTOR.get(sector, [])

        for ticker in tickers:
            cik = ticker_map.get(ticker.upper())
            if not cik:
                logger.debug("No CIK for ticker %s — skipping", ticker)
                continue

            filings = _fetch_recent_8k_filing_texts(cik, quarter, year)

            for filing in filings:
                all_reports.append({
                    "ticker": ticker,
                    "sector": sector,
                    "quarter": quarter,
                    "year": year,
                    "filing_date": filing["filing_date"],
                    "accession_number": filing["accession_number"],
                    "text": filing["text"],
                })

    # Save to JSONL
    if all_reports:
        _save_reports_jsonl(all_reports, quarter, year)

    logger.info(
        "Fetched %d quarterly reports for %s %d",
        len(all_reports), quarter, year,
    )
    return all_reports


def _save_reports_jsonl(reports: list[dict], quarter: str, year: int) -> Path:
    """Save fetched reports to a JSONL file in the quarterly_reports directory.

    Returns the path to the written file.
    """
    QUARTERLY_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{year}_{quarter}_reports.jsonl"
    filepath = QUARTERLY_REPORTS_DIR / filename

    with open(filepath, "w") as f:
        for report in reports:
            f.write(json.dumps(report) + "\n")

    logger.info("Saved %d reports to %s", len(reports), filepath)
    return filepath


# ---------------------------------------------------------------------------
# FinBERT scoring (lazy import — module importable without transformers)
# ---------------------------------------------------------------------------


def _score_text_with_finbert(text: str, pipe) -> float:
    """Score a text segment with FinBERT, returning a value in [-1.0, +1.0].

    ProsusAI/finbert labels: 'positive', 'neutral', 'negative'.
    Maps positive label → +confidence, negative → -confidence, neutral → 0.0.
    Out-of-range FinBERT scores are clamped to [-1.0, +1.0].
    Empty / whitespace text returns 0.0 without calling the model.
    """
    if not text or not text.strip():
        return 0.0

    try:
        results = pipe(text[:_MAX_FINBERT_INPUT_CHARS], truncation=True, max_length=512)
        result = results[0] if isinstance(results, list) else results
        label = str(result.get("label", "neutral")).lower()
        raw_score = float(result.get("score", 0.0))
        # Clamp to [0.0, 1.0] per contract (FinBERT confidence is 0–1)
        raw_score = max(0.0, min(1.0, raw_score))
        if label == "positive":
            return raw_score
        elif label == "negative":
            return -raw_score
        return 0.0
    except Exception as exc:
        logger.warning("FinBERT scoring error: %s", exc)
        return 0.0


# ---------------------------------------------------------------------------
# Sort
# ---------------------------------------------------------------------------


def _sort_sectors(sectors: list[dict]) -> list[dict]:
    """Sort sectors: positive first → neutral → negative; alphabetical within tier."""
    return sorted(
        sectors,
        key=lambda s: (_TONE_SORT_ORDER.get(s.get("current_tone", "neutral"), 1), s["name"]),
    )


# ---------------------------------------------------------------------------
# Cache read / write
# ---------------------------------------------------------------------------


def get_sector_management_tone() -> Optional[dict]:
    """Read sector management tone data from cache.

    Returns the cached dict (with keys: quarter, year, data_available, sectors)
    or None if no cache exists or the cache file is unreadable.
    Does NOT make any network calls.
    """
    if not CACHE_FILE.exists():
        return None
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except Exception as exc:
        logger.warning("Failed to read sector tone cache: %s", exc)
        return None


def _write_cache(data: dict) -> None:
    """Write data dict to the sector tone cache file."""
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------


def update_sector_management_tone() -> None:
    """Run the full SEC EDGAR + FinBERT sector tone pipeline and refresh cache.

    Intended to be called as a scheduled batch job (e.g., quarterly, after
    earnings season). This function is NOT called on each homepage request.

    Fetches quarterly reports (saving full texts to JSONL), then scores
    them with FinBERT for sector tone classification.

    Raises:
        ImportError: if the `transformers` package is not installed.

    Docker / image-size warning (see module docstring for details).
    """
    # Lazy import — keeps module importable without transformers/torch installed
    try:
        from transformers import pipeline as hf_pipeline  # type: ignore[import]
    except ImportError as exc:
        raise ImportError(
            "The 'transformers' package is required for FinBERT scoring.\n"
            "Install with: pip install transformers torch\n"
            f"Original error: {exc}"
        ) from exc

    logger.info("Loading ProsusAI/finbert model...")
    finbert_pipe = hf_pipeline(
        "text-classification",
        model="ProsusAI/finbert",
    )

    current_dt = datetime.utcnow()
    quarter_label, year = _get_quarter_label(current_dt)
    logger.info("Running sector tone pipeline for %s %d", quarter_label, year)

    # Fetch all quarterly reports (also saves full texts to JSONL)
    reports = fetch_quarterly_reports(quarter_label, year)

    # Group reports by sector for scoring
    sector_reports: dict[str, list[dict]] = {}
    for report in reports:
        sector_reports.setdefault(report["sector"], []).append(report)

    # Load existing cache to preserve trend history across quarters
    existing = get_sector_management_tone() or {}
    existing_sectors_by_name: dict[str, dict] = {
        s["name"]: s for s in existing.get("sectors", [])
    }

    sectors_output: list[dict] = []

    for sector in GICS_SECTORS:
        sector_scores: list[float] = []

        for report in sector_reports.get(sector, []):
            text = report["text"]
            if text and text.strip():
                # Truncate to FinBERT input limit for scoring only
                score = _score_text_with_finbert(
                    text[:_MAX_FINBERT_INPUT_CHARS], finbert_pipe,
                )
                sector_scores.append(score)

        if sector_scores:
            current_score = sum(sector_scores) / len(sector_scores)
        else:
            logger.warning("No filings scored for sector '%s' — defaulting to neutral", sector)
            current_score = 0.0

        current_tone = _classify_tone(current_score)
        short_name = SHORT_NAME_MAP.get(sector, sector)

        # Build trend: preserve prior quarters, append/replace current quarter
        prior_trend: list[dict] = [
            t for t in existing_sectors_by_name.get(sector, {}).get("trend", [])
            if not (t.get("quarter") == quarter_label and t.get("year") == year)
        ]
        prior_trend.append({"quarter": quarter_label, "year": year, "tone": current_tone})
        # Keep at most 4 quarters (oldest first)
        trend = prior_trend[-4:]

        sectors_output.append({
            "name": sector,
            "short_name": short_name,
            "current_tone": current_tone,
            "current_score": round(current_score, 4),
            "trend": trend,
        })

    sectors_output = _sort_sectors(sectors_output)

    output: dict = {
        "quarter": quarter_label,
        "year": year,
        "data_available": True,
        "sectors": sectors_output,
    }
    _write_cache(output)
    logger.info(
        "Sector tone pipeline complete: %d sectors, %s %d",
        len(sectors_output),
        quarter_label,
        year,
    )
