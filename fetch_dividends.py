"""Fetch NEPSE proposed-dividend history from ShareSansar and write it to one CSV.

Pulls only the most recent RECENT_YEARS_COUNT fiscal years (dividends for
older years don't change) from the site's DataTables AJAX endpoint at
https://www.sharesansar.com/proposed-dividend, then merges them into the
existing dividend_history.csv, leaving rows for older fiscal years as-is.

Plain HTTP requests (requests/curl) to this endpoint are silently throttled
to empty results from datacenter IP ranges, including GitHub Actions
runners. Issuing the same fetch() call from inside a real headless-browser
page (matching what the site's own JS does) gets through reliably, so this
script drives Playwright instead of calling requests directly.
"""

import csv
import json
import re
import sys
import time

from playwright.sync_api import sync_playwright

BASE_URL = "https://www.sharesansar.com/proposed-dividend"
# The endpoint silently rejects requests for more than ~50 rows per page
# (returns HTTP 202 with an empty result set instead of an error), so keep
# this at or below 50.
PAGE_SIZE = 50
REQUEST_DELAY_SECONDS = 0.8
OUTPUT_FILE = "dividend_history.csv"
RECENT_YEARS_COUNT = 3

CSV_HEADER = [
    "Symbol",
    "Company",
    "Bonus (%)",
    "Cash (%)",
    "Total (%)",
    "Announcement Date",
    "Book Closure Date",
    "Distribution Date",
    "Bonus Listing Date",
    "Fiscal Year",
]

# Fiscal-year select option values -> labels, as served by the site's
# "Fiscal Year Wise" tab, newest first. The site adds one new entry per
# Nepali fiscal year (mid-July); re-check the <select id="year"> options
# on the page if a year is missing. Only the first RECENT_YEARS_COUNT
# entries are actually scraped each run (see main()); the rest is kept
# around so dividend_history.csv's older rows can still be labeled/cross
# -checked if ever needed.
FISCAL_YEARS = {
    31: "2081/2082",
    30: "2080/2081",
    29: "2079/2080",
    28: "2078/2079",
    27: "2077/2078",
    26: "2076/2077",
    24: "2075/2076",
    5: "2074/2075",
    4: "2073/2074",
    3: "2072/2073",
    2: "2071/2072",
    1: "2070/2071",
    16: "2069/2070",
    15: "2068/2069",
    14: "2067/2068",
    13: "2066/2067",
    12: "2065/2066",
    11: "2064/2065",
    23: "2063/2064",
    17: "2062/2063",
    18: "2061/2062",
    19: "2060/2061",
    20: "2059/2060",
    21: "2058/2059",
    22: "2057/2058",
}

TAG_RE = re.compile(r"<[^>]+>")

# Column definitions the site's DataTables JS sends with every request.
# The endpoint 500s without them.
_COLUMNS = [
    ("DT_Row_Index", "", False, False),
    ("symbol", "tbl_company_list.symbol", True, True),
    ("companyname", "tbl_company_list.companyname", True, True),
    ("bonus_share", "", True, True),
    ("cash_dividend", "", True, True),
    ("total_dividend", "", True, True),
    ("announcement_date", "", True, True),
    ("bookclose_date", "", True, True),
    ("distribution_date", "", True, True),
    ("bonus_listing_date", "", True, True),
    ("year", "tbl_macro_year.year", True, True),
]


def _build_query(start, year_id):
    # Key order matches the DataTables JS the site itself sends
    # (draw, columns[], order[], start, length, search, type, year, sector).
    params = [("draw", 1)]
    for i, (data, name, searchable, orderable) in enumerate(_COLUMNS):
        params.append((f"columns[{i}][data]", data))
        params.append((f"columns[{i}][name]", name))
        params.append((f"columns[{i}][searchable]", "true" if searchable else "false"))
        params.append((f"columns[{i}][orderable]", "true" if orderable else "false"))
        params.append((f"columns[{i}][search][value]", ""))
        params.append((f"columns[{i}][search][regex]", "false"))
    params += [
        ("order[0][column]", 6),
        ("order[0][dir]", "desc"),
        ("start", start),
        ("length", PAGE_SIZE),
        ("search[value]", ""),
        ("search[regex]", "false"),
        ("type", "YEARWISE"),
        ("year", year_id),
        ("sector", 0),
    ]
    return "&".join(f"{k}={v}" for k, v in params)


def strip_tags(value):
    if not value:
        return ""
    return TAG_RE.sub("", value).strip()


MAX_ATTEMPTS = 4
RETRY_BACKOFF_SECONDS = 3


def _fetch_page(page, start, year_id):
    query = _build_query(start, year_id)
    js = """
        async (query) => {
            const resp = await fetch('%s?' + query, {
                headers: { 'X-Requested-With': 'XMLHttpRequest' }
            });
            return { status: resp.status, body: await resp.text() };
        }
    """ % BASE_URL

    last_payload = {"data": [], "recordsFiltered": 0}
    for attempt in range(1, MAX_ATTEMPTS + 1):
        result = page.evaluate(js, query)
        if result["status"] == 200:
            payload = json.loads(result["body"])
            if payload.get("recordsFiltered", 0) > 0 or start > 0:
                return payload
            last_payload = payload
        if attempt < MAX_ATTEMPTS:
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    return last_payload


def fetch_year(page, year_id, year_label):
    rows = []
    start = 0
    while True:
        payload = _fetch_page(page, start, year_id)
        data = payload.get("data", [])
        rows.extend(data)

        start += PAGE_SIZE
        if start >= payload.get("recordsFiltered", 0) or not data:
            break
        time.sleep(REQUEST_DELAY_SECONDS)

    print(f"  {year_label}: {len(rows)} rows", file=sys.stderr)
    return rows


def _to_csv_row(row):
    return [
        strip_tags(row.get("symbol")),
        strip_tags(row.get("companyname")),
        row.get("bonus_share") or "",
        row.get("cash_dividend") or "",
        row.get("total_dividend") or "",
        row.get("announcement_date") or "",
        row.get("bookclose_date") or "",
        row.get("distribution_date") or "",
        row.get("bonus_listing_date") or "",
        row.get("year") or "",
    ]


def _load_existing_rows():
    try:
        with open(OUTPUT_FILE, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except FileNotFoundError:
        return []


def main():
    scrape_years = dict(list(FISCAL_YEARS.items())[:RECENT_YEARS_COUNT])
    scraped_labels = set(scrape_years.values())

    fresh_rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        )
        # Load the real page first so requests carry a normal browser
        # session/referer, same as a person browsing the site.
        page.goto(BASE_URL, wait_until="domcontentloaded")

        for year_id, year_label in scrape_years.items():
            try:
                fresh_rows.extend(fetch_year(page, year_id, year_label))
            except Exception as exc:
                print(f"  {year_label}: failed ({exc})", file=sys.stderr)
            time.sleep(REQUEST_DELAY_SECONDS)

        browser.close()

    # Keep existing rows for fiscal years we didn't re-scrape this run;
    # replace rows for the years we just fetched.
    kept_rows = [
        row
        for row in _load_existing_rows()
        if row.get("Fiscal Year") not in scraped_labels
    ]

    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_HEADER)
        for row in fresh_rows:
            writer.writerow(_to_csv_row(row))
        for row in kept_rows:
            writer.writerow([row.get(col, "") for col in CSV_HEADER])

    print(
        f"Wrote {len(fresh_rows)} fresh rows ({', '.join(scraped_labels)}) "
        f"+ {len(kept_rows)} kept rows to {OUTPUT_FILE}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
