"""Fetch NEPSE proposed-dividend history from ShareSansar and write it to one CSV.

Pulls every fiscal year (2057/2058 onward) from the site's DataTables AJAX
endpoint at https://www.sharesansar.com/proposed-dividend and combines all
rows into a single dividend_history.csv.
"""

import csv
import re
import sys
import time

import requests

BASE_URL = "https://www.sharesansar.com/proposed-dividend"
PAGE_SIZE = 100
REQUEST_DELAY_SECONDS = 1.5
OUTPUT_FILE = "dividend_history.csv"

# Fiscal-year select option values -> labels, as served by the site's
# "Fiscal Year Wise" tab. The site adds one new entry per Nepali fiscal
# year (mid-July); re-check the <select id="year"> options on the page
# if a year is missing.
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

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Referer": BASE_URL,
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


def _build_params(start, year_id):
    # Key order matches the DataTables JS the site itself sends
    # (draw, columns[], order[], start, length, search, type, year, sector).
    # The endpoint silently returns an empty result set if the params
    # arrive in a different order.
    params = {"draw": 1}
    for i, (data, name, searchable, orderable) in enumerate(_COLUMNS):
        params[f"columns[{i}][data]"] = data
        params[f"columns[{i}][name]"] = name
        params[f"columns[{i}][searchable]"] = "true" if searchable else "false"
        params[f"columns[{i}][orderable]"] = "true" if orderable else "false"
        params[f"columns[{i}][search][value]"] = ""
        params[f"columns[{i}][search][regex]"] = "false"
    params.update(
        {
            "order[0][column]": 6,
            "order[0][dir]": "desc",
            "start": start,
            "length": PAGE_SIZE,
            "search[value]": "",
            "search[regex]": "false",
            "type": "YEARWISE",
            "year": year_id,
            "sector": 0,
        }
    )
    return params


def strip_tags(value):
    if not value:
        return ""
    return TAG_RE.sub("", value).strip()


MAX_ATTEMPTS = 6
RETRY_BACKOFF_SECONDS = 5


def _get_page(session, start, year_id):
    """Fetch one page, retrying on non-200 or on an empty result.

    The endpoint occasionally responds 202 with an empty record set
    under request-rate pressure even for years that have data; retrying
    after a short backoff reliably gets a real 200 response.
    """
    params = _build_params(start, year_id)
    last_payload = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        resp = session.get(BASE_URL, params=params, headers=HEADERS, timeout=30)
        if resp.status_code == 200:
            payload = resp.json()
            if payload.get("recordsFiltered", 0) > 0 or start > 0:
                return payload
            last_payload = payload
        if attempt < MAX_ATTEMPTS:
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    return last_payload or {"data": [], "recordsFiltered": 0}


def fetch_year(session, year_id, year_label):
    rows = []
    start = 0
    while True:
        payload = _get_page(session, start, year_id)
        data = payload.get("data", [])
        rows.extend(data)

        start += PAGE_SIZE
        if start >= payload.get("recordsFiltered", 0) or not data:
            break
        time.sleep(REQUEST_DELAY_SECONDS)

    print(f"  {year_label}: {len(rows)} rows", file=sys.stderr)
    return rows


def main():
    session = requests.Session()
    all_rows = []

    for year_id, year_label in FISCAL_YEARS.items():
        try:
            all_rows.extend(fetch_year(session, year_id, year_label))
        except requests.RequestException as exc:
            print(f"  {year_label}: failed ({exc})", file=sys.stderr)
        time.sleep(REQUEST_DELAY_SECONDS)

    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
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
        )
        for row in all_rows:
            writer.writerow(
                [
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
            )

    print(f"Wrote {len(all_rows)} rows to {OUTPUT_FILE}", file=sys.stderr)


if __name__ == "__main__":
    main()
