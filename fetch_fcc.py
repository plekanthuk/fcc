#!/usr/bin/env python3
"""
FCC EAS сканер -> надсилає нормалізовані записи у плагін FCC Import Monitor
(WordPress REST /wp-json/fcc/v1/import).

Джерело даних: apps.fcc.gov (офіційний Equipment Authorization Search).
Сайт захищений Akamai (перевірка TLS-відбитку клієнта) — тому тут
використовується curl_cffi (impersonate=chrome), а не звичайний requests.

Використання:
    python fetch_fcc.py --date-from 2026-08-18 --date-to 2026-08-18 --scan-type daily --enrich
    python fetch_fcc.py --date-from 2021-01-01 --date-to 2026-08-19 --scan-type historical
    python fetch_fcc.py --date-from 2026-08-01 --date-to 2026-08-19 --scan-type manual --enrich

Потрібні змінні середовища (у GitHub Actions — секрети репозиторію):
    WP_URL      напр. https://example.com
    WP_API_KEY  значення поля "X-FCC-Api-Key" з вкладки Scan в адмінці плагіна
"""

import argparse
import os
import re
import sys
import time
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from urllib.parse import unquote

import requests as wp_requests
from bs4 import BeautifulSoup
from curl_cffi import requests as fcc_requests

import form731

SEARCH_URL = "https://apps.fcc.gov/oetcf/eas/reports/GenericSearchResult.cfm?RequestTimeout=500"
EXHIBITS_URL = "https://apps.fcc.gov/oetcf/eas/reports/ViewExhibitReport.cfm"
GRANT_FORM_URL = "https://apps.fcc.gov/oetcf/tcb/reports/Tcb731GrantForm.cfm"
IMPERSONATE = "chrome"

BASE_FIELDS = {
    "grantee_code": "", "product_code": "", "applicant_name": "",
    "grant_date_from": "", "grant_date_to": "", "comments": "",
    "application_purpose": "", "application_purpose_description": "",
    "grant_code_1": "", "grant_code_2": "", "grant_code_3": "",
    "test_firm": "", "application_status": "", "application_status_description": "",
    "equipment_class": "", "equipment_class_description": "",
    "lower_frequency": "", "upper_frequency": "", "freq_exact_match": "on",
    "bandwidth_from": "", "emission_designator": "",
    "tolerance_from": "", "tolerance_to": "", "tolerance_exact_match": "on",
    "power_output_from": "", "power_output_to": "", "power_exact_match": "on",
    "rule_part_1": "", "rule_part_2": "", "rule_part_3": "", "rule_part_exact_match": "on",
    "product_description": "", "modular_type_description": "",
    "tcb_code": "", "tcb_code_description": "", "tcb_scope": "", "tcb_scope_description": "",
    "outputformat": "HTML", "show_records": "5000", "fetchfrom": "0",
    "calledFromFrame": "N",
}

_TOTAL_RE = re.compile(r"Displaying records [\d,]+ through [\d,]+ of ([\d,]+)")


def to_fcc_date(d: date) -> str:
    return d.strftime("%m/%d/%Y")


def fetch_search_html(date_from: str, date_to: str, show_records: int = 5000, _retry: bool = True) -> str:
    """outputformat=HTML РЕАЛЬНО пагінує (на відміну від XML), тому просимо
    одразу з запасом і, якщо загальна кількість перевищує запит, перезапитуємо
    з більшим show_records — на відміну від fetchfrom, який виявився неробочим
    (сервер завжди повертає першу сторінку незалежно від fetchfrom)."""
    payload = dict(BASE_FIELDS)
    payload["grant_date_from"] = date_from
    payload["grant_date_to"] = date_to
    payload["show_records"] = str(show_records)

    resp = fcc_requests.post(SEARCH_URL, data=payload, impersonate=IMPERSONATE, timeout=90)
    resp.raise_for_status()
    text = resp.text

    m = _TOTAL_RE.search(text)
    if m and _retry:
        total = int(m.group(1).replace(",", ""))
        if total > show_records:
            return fetch_search_html(date_from, date_to, show_records=total + 200, _retry=False)
    return text


def parse_search_rows(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    tbody = soup.find(id="offTblBdy")
    if tbody is None:
        return []

    rows = []
    for tr in tbody.find_all("tr", recursive=False):
        cells = tr.find_all("td", recursive=False)
        if len(cells) < 16:
            continue

        detail_cell = cells[2]
        application_id = None
        detail_link = detail_cell.find("a")
        if detail_link and detail_link.get("href"):
            m = re.search(r"application_id=([^&]+)", detail_link["href"])
            if m:
                application_id = unquote(m.group(1))

        def text_of(cell):
            return cell.get_text(strip=True)

        fcc_id = text_of(cells[11])
        if not fcc_id:
            continue

        rows.append({
            "application_id": application_id,
            "applicant_name": text_of(cells[5]),
            "address": text_of(cells[6]),
            "city": text_of(cells[7]),
            "state": text_of(cells[8]),
            "country": text_of(cells[9]),
            "zip_code": text_of(cells[10]),
            "fcc_id": fcc_id,
            "application_purpose": text_of(cells[12]),
            "grant_date_mmddyyyy": text_of(cells[13]),
            "lower_freq_mhz": text_of(cells[14]),
            "upper_freq_mhz": text_of(cells[15]),
        })
    return rows


def _format_freq(value: str) -> str:
    if not value:
        return value
    try:
        num = float(value)
    except ValueError:
        return value
    text = f"{num:.6f}".rstrip("0").rstrip(".")
    return text or "0"


def to_iso_date(mmddyyyy: str) -> str:
    return datetime.strptime(mmddyyyy, "%m/%d/%Y").strftime("%Y-%m-%d")


def group_by_fcc_id(rows: list[dict]) -> dict:
    grouped: dict[str, dict] = {}
    for r in rows:
        key = (r["fcc_id"], r["grant_date_mmddyyyy"])
        if key not in grouped:
            grantee_code = r["fcc_id"].split("-")[0] if "-" in r["fcc_id"] else r["fcc_id"][:5]
            grouped[key] = {
                "fcc_id": r["fcc_id"],
                "grantee_code": grantee_code,
                "product_code": r["fcc_id"][len(grantee_code):].lstrip("-"),
                "application_id": r["application_id"],
                "applicant_name": r["applicant_name"],
                "address": r["address"],
                "city": r["city"],
                "state": r["state"],
                "country": r["country"],
                "zip_code": r["zip_code"],
                "application_purpose": r["application_purpose"],
                "grant_date": to_iso_date(r["grant_date_mmddyyyy"]),
                "freq_ranges": [],
            }
        lo, hi = _format_freq(r["lower_freq_mhz"]), _format_freq(r["upper_freq_mhz"])
        if lo or hi:
            rng = f"{lo}-{hi} MHz"
            if rng not in grouped[key]["freq_ranges"]:
                grouped[key]["freq_ranges"].append(rng)
    for rec in grouped.values():
        rec["freq_ranges"] = "; ".join(rec["freq_ranges"])
    return grouped


# ---------------------------------------------------------------------------
# Збагачення (Equipment Class / Device Description + список документів)
# ---------------------------------------------------------------------------

SUMMARY_URL = "https://apps.fcc.gov/oetcf/eas/reports/ViewExhibitReport.cfm"
FORM731_URL = "https://apps.fcc.gov/tcb/GetTcb731Report.do"


def _fetch_exhibit_links(fcc_id: str, application_id: str) -> dict:
    """mode=Exhibits: only the documents that are actually downloadable right
    now. Keyed by lowercased title, since this view has no stable id we can
    join on with the Summary view other than the description text."""
    links = {}
    resp = fcc_requests.get(
        EXHIBITS_URL,
        params={"mode": "Exhibits", "RequestTimeout": "500", "calledFromFrame": "N",
                "application_id": application_id, "fcc_id": fcc_id},
        impersonate=IMPERSONATE, timeout=45,
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    tbody = soup.find(id="offTblBdy")
    if not tbody:
        return links
    for tr in tbody.find_all("tr", recursive=False):
        cells = tr.find_all("td", recursive=False)
        if len(cells) < 6:
            continue
        link = cells[1].find("a")
        if not link or not link.get("href"):
            continue
        m = re.search(r"[?&]id=(\d+)", link["href"])
        if not m:
            continue
        title = link.get_text(strip=True)
        href = link["href"]
        links[title.lower()] = {
            "attachment_id": m.group(1),
            "download_url": "https://apps.fcc.gov" + href if href.startswith("/") else href,
        }
    return links


def _fetch_exhibit_summary(fcc_id: str, application_id: str) -> list[dict]:
    """mode=Sum: the FULL list including confidential/embargoed documents
    that have no download link yet (fccid.io shows these as "Metadata only").
    Columns: (blank), Exhibit Type, File Type, File Size, Description,
    Submission Date, Permanent Confidential, Short-Term Confidential,
    Supercede, Date Available."""
    rows = []
    resp = fcc_requests.get(
        SUMMARY_URL,
        params={"mode": "Sum", "calledFromFrame": "N", "RequestTimeout": "500",
                "application_id": application_id, "fcc_id": fcc_id},
        impersonate=IMPERSONATE, timeout=45,
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    tbody = soup.find(id="offTblBdy")
    if not tbody:
        return rows
    for tr in tbody.find_all("tr", recursive=False):
        cells = tr.find_all("td", recursive=False)
        if len(cells) < 10:
            continue
        text = lambda i: cells[i].get_text(strip=True)
        rows.append({
            "exhibit_type": text(1), "file_format": text(2),
            "file_size": text(3), "title": text(4),
            "submitted_date": _safe_iso(text(5)),
            "available_date": _safe_iso(text(9)),
        })
    return rows


def enrich_record(fcc_id: str, application_id: str) -> dict:
    """Returns {'equipment_class', 'device_description', 'documents': [...],
    'application_info': {...}}."""
    result = {
        "equipment_class": None, "device_description": None,
        "documents": [], "application_info": None,
    }

    try:
        links_by_title = _fetch_exhibit_links(fcc_id, application_id)
        summary_rows = _fetch_exhibit_summary(fcc_id, application_id)
        for row in summary_rows:
            link = links_by_title.get(row["title"].lower())
            result["documents"].append({
                "attachment_id": link["attachment_id"] if link else None,
                "title": row["title"],
                "exhibit_type": row["exhibit_type"],
                "file_format": row["file_format"],
                "file_size": row["file_size"],
                "submitted_date": row["submitted_date"],
                "available_date": row["available_date"],
                "download_url": link["download_url"] if link else None,
            })
    except Exception as e:
        print(f"[enrich] {fcc_id} documents failed: {type(e).__name__}: {e}", file=sys.stderr)

    try:
        form_resp = fcc_requests.get(
            FORM731_URL,
            params={"applicationId": application_id, "fcc_id": fcc_id},
            impersonate=IMPERSONATE, timeout=45,
        )
        form_resp.raise_for_status()
        info = form731.parse_application_info(form_resp.text)
        info["application_id"] = application_id
        result["application_info"] = info
        result["equipment_class"] = info.get("equipment_class") or None
        result["device_description"] = info.get("device_description") or None
    except Exception as e:
        print(f"[enrich] {fcc_id} form731 failed: {type(e).__name__}: {e}", file=sys.stderr)

    try:
        grant_resp = fcc_requests.get(
            GRANT_FORM_URL,
            params={"mode": "COPY", "RequestTimeout": "500", "tcb_code": "",
                    "application_id": application_id, "fcc_id": fcc_id},
            impersonate=IMPERSONATE, timeout=45,
        )
        grant_resp.raise_for_status()
        if result["application_info"] is None:
            result["application_info"] = {}
        result["application_info"]["grant_html"] = form731.parse_grant_html(grant_resp.text)
    except Exception as e:
        print(f"[enrich] {fcc_id} grant text failed: {type(e).__name__}: {e}", file=sys.stderr)

    return result


def _safe_iso(mmddyyyy: str):
    try:
        return to_iso_date(mmddyyyy)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Пуш у WordPress
# ---------------------------------------------------------------------------

def push_to_wp(wp_url: str, api_key: str,
                scan_type: str, date_from: str, date_to: str,
                records: list[dict], scan_request_id: int | None = None,
                started_at: str | None = None) -> dict:
    # ?rest_route=... замість /wp-json/..., бо REST-запис /wp-json/ вимагає
    # активних "pretty permalinks" у WP, а на цільовому сайті вони вимкнені
    # (без цього шлях повертає 404 від самого веб-сервера, ще до WP).
    #
    # Авторизація через X-FCC-Api-Key, а не WP Application Password:
    # на цьому хостингу (nginx) заголовок Authorization обрізається
    # ще до PHP, тому Basic Auth ніколи не доходить.
    endpoint = wp_url.rstrip("/") + "/?rest_route=/fcc/v1/import"
    payload = {
        "scan_type": scan_type,
        "date_from": date_from,
        "date_to": date_to,
        "records": records,
    }
    if scan_request_id:
        payload["scan_request_id"] = scan_request_id
    if started_at:
        # Коли ЦЕЙ python-процес почав тягнути дані з FCC (UTC ISO 8601) —
        # щоб лог у WP показував реальну тривалість сканування, а не лише
        # час на upsert у БД під час цього POST-запиту.
        payload["started_at"] = started_at

    # nginx на цьому хостингу блокує запити з UA "python-requests" (403
    # ще до PHP) — тому видаємо себе за звичайний браузер.
    headers = {
        "X-FCC-Api-Key": api_key,
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    }
    resp = wp_requests.post(endpoint, json=payload, headers=headers, timeout=120)
    resp.raise_for_status()
    return resp.json()


def _wp_headers(api_key: str) -> dict:
    return {
        "X-FCC-Api-Key": api_key,
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    }


def get_backfill_status(wp_url: str, api_key: str) -> dict:
    endpoint = wp_url.rstrip("/") + "/?rest_route=/fcc/v1/backfill-status"
    resp = wp_requests.get(endpoint, headers=_wp_headers(api_key), timeout=30)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Оркестрація одного діапазону дат
# ---------------------------------------------------------------------------

def scan_range(d_from: date, d_to: date, enrich: bool, sleep_between: float = 0.15) -> list[dict]:
    html = fetch_search_html(to_fcc_date(d_from), to_fcc_date(d_to))
    rows = parse_search_rows(html)
    grouped = group_by_fcc_id(rows)

    records = list(grouped.values())

    if enrich:
        t0 = time.time()
        for i, rec in enumerate(records):
            if not rec.get("application_id"):
                continue
            extra = enrich_record(rec["fcc_id"], rec["application_id"])
            rec["equipment_class"] = extra["equipment_class"]
            rec["device_description"] = extra["device_description"]
            rec["documents"] = extra["documents"]
            rec["application_info"] = extra["application_info"]
            time.sleep(sleep_between)
            if (i + 1) % 20 == 0:
                print(f"[enrich] {i + 1}/{len(records)} in {time.time() - t0:.1f}s", file=sys.stderr)

    return records


def month_chunks(d_from: date, d_to: date):
    cur = d_from.replace(day=1)
    while cur <= d_to:
        last_day = monthrange(cur.year, cur.month)[1]
        chunk_end = min(date(cur.year, cur.month, last_day), d_to)
        chunk_start = max(cur, d_from)
        yield chunk_start, chunk_end
        if cur.month == 12:
            cur = date(cur.year + 1, 1, 1)
        else:
            cur = date(cur.year, cur.month + 1, 1)


def run_backfill_tick(wp_url: str, wp_api_key: str) -> None:
    """Один 'тік' історичного бекфілу: сканує РІВНО один день (учора від
    найранішої дати вже в базі) і завершується. Викликається кроном раз на
    10 хв (.github/workflows/backfill.yml) — це навмисно один короткий,
    ідемпотентний запуск замість одного величезного багатогодинного job."""
    status = get_backfill_status(wp_url, wp_api_key)

    if not status.get("enabled"):
        print("[backfill] disabled in WP settings, skipping", file=sys.stderr)
        return

    earliest = status.get("earliest_scanned_date")
    start_year = int(status.get("start_year") or (date.today().year - 5))
    target = date(start_year, 1, 1)

    if earliest:
        next_day = date.fromisoformat(earliest) - timedelta(days=1)
    else:
        next_day = date.today() - timedelta(days=1)

    if next_day < target:
        print(f"[backfill] reached target year {start_year}, nothing to do", file=sys.stderr)
        return

    print(f"[backfill] scanning {next_day} (target: {target})", file=sys.stderr)
    started_at = datetime.now(timezone.utc).isoformat()
    records = scan_range(next_day, next_day, enrich=True)
    print(f"[backfill]   {len(records)} unique records", file=sys.stderr)

    result = push_to_wp(wp_url, wp_api_key, "historical", next_day.isoformat(), next_day.isoformat(),
                         records, started_at=started_at)
    print(f"[backfill]   -> {result.get('counts')}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="FCC EAS scanner -> WordPress push")
    parser.add_argument("--date-from", type=date.fromisoformat)
    parser.add_argument("--date-to", type=date.fromisoformat)
    parser.add_argument("--scan-type", default="manual", choices=["daily", "historical", "manual"])
    parser.add_argument("--enrich", action="store_true",
                         help="Тягнути Equipment Class/Description + список документів (повільніше)")
    parser.add_argument("--scan-request-id", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true", help="Не пушити в WP, лише показати кількість")
    parser.add_argument("--backfill", action="store_true",
                         help="Один тік історичного бекфілу (керується налаштуваннями у WP), ігнорує --date-from/to")
    args = parser.parse_args()

    wp_url = os.environ.get("WP_URL")
    wp_api_key = os.environ.get("WP_API_KEY")
    if not args.dry_run and not all([wp_url, wp_api_key]):
        print("ERROR: потрібні env WP_URL, WP_API_KEY (або --dry-run)", file=sys.stderr)
        sys.exit(1)

    if args.backfill:
        run_backfill_tick(wp_url, wp_api_key)
        return

    if not args.date_from or not args.date_to:
        print("ERROR: потрібні --date-from і --date-to (або --backfill)", file=sys.stderr)
        sys.exit(1)

    is_multi_month = (args.date_to.year, args.date_to.month) != (args.date_from.year, args.date_from.month)
    chunks = list(month_chunks(args.date_from, args.date_to)) if is_multi_month else [(args.date_from, args.date_to)]

    total_new = total_changed = total_unchanged = total_errors = 0

    for chunk_start, chunk_end in chunks:
        print(f"[scan] {chunk_start} .. {chunk_end}", file=sys.stderr)
        chunk_started_at = datetime.now(timezone.utc).isoformat()
        records = scan_range(chunk_start, chunk_end, enrich=args.enrich)
        print(f"[scan]   {len(records)} унікальних записів", file=sys.stderr)

        if args.dry_run:
            continue

        result = push_to_wp(
            wp_url, wp_api_key,
            args.scan_type, chunk_start.isoformat(), chunk_end.isoformat(),
            records, scan_request_id=args.scan_request_id, started_at=chunk_started_at,
        )
        counts = result.get("counts", {})
        total_new += counts.get("new", 0)
        total_changed += counts.get("changed", 0)
        total_unchanged += counts.get("unchanged", 0)
        total_errors += counts.get("errors", 0)
        print(f"[scan]   -> {counts}", file=sys.stderr)

    print(f"[scan] DONE: new={total_new} changed={total_changed} "
          f"unchanged={total_unchanged} errors={total_errors}", file=sys.stderr)


if __name__ == "__main__":
    main()
