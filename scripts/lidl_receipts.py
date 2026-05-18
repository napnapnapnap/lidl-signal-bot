#!/usr/bin/env python3
"""Fetch and parse Lidl UK digital receipts."""

from __future__ import annotations

import argparse
import glob
import getpass
import html
import json
import math
import os
import re
import sys
import time
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SUMMARY_URL = "https://www.lidl.co.uk/mre/api/v1/tickets"
DETAIL_URL = "https://www.lidl.co.uk/mre/api/v1/tickets/{ticket_id}"
LIDL_HOME_URL = "https://www.lidl.co.uk/mla/"
DEFAULT_AUTH_STATE_FILENAME = "lidl_auth_state.json"


class RateLimiter:
    def __init__(self, requests_per_second: float) -> None:
        self.min_interval = 1.0 / requests_per_second if requests_per_second > 0 else 0.0
        self.last_request = 0.0

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        now = time.monotonic()
        sleep_for = self.min_interval - (now - self.last_request)
        if sleep_for > 0:
            time.sleep(sleep_for)
        self.last_request = time.monotonic()


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    tmp_path.replace(path)


def print_err(message: str) -> None:
    print(message, file=sys.stderr)


def auth_state_path(args: argparse.Namespace) -> Path | None:
    if args.no_auth_state:
        return None
    if args.auth_state:
        return args.auth_state.expanduser().resolve()
    return args.data_dir / DEFAULT_AUTH_STATE_FILENAME


def cookie_header_from_cookies(cookies: list[dict[str, Any]], request_path: str = "/mre/api/v1/tickets") -> str:
    now = time.time()
    pairs = []
    for cookie in cookies:
        name = cookie.get("name")
        value = cookie.get("value")
        if not name or value is None:
            continue

        domain = str(cookie.get("domain") or "").lstrip(".")
        if domain and domain != "www.lidl.co.uk" and not "www.lidl.co.uk".endswith(f".{domain}"):
            continue

        path = str(cookie.get("path") or "/")
        if not request_path.startswith(path.rstrip("/") or "/"):
            continue

        expires = float(cookie.get("expires") or -1)
        if expires > 0 and expires < now:
            continue

        pairs.append(f"{name}={value}")

    return "; ".join(pairs)


def cookie_header_from_auth_state(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        state = read_json(path)
    except Exception as exc:  # noqa: BLE001 - explain the bad cache and continue to other auth sources
        print_err(f"Ignoring unreadable Lidl auth state {path}: {exc}")
        return None
    cookie = cookie_header_from_cookies(list(state.get("cookies", [])))
    return cookie or None


def validate_cookie(args: argparse.Namespace, cookie: str) -> bool:
    try:
        get_json(
            make_headers(cookie),
            SUMMARY_URL,
            {"country": args.country, "page": 1},
            RateLimiter(0),
            args.insecure,
        )
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ssl.SSLError):
            raise SystemExit(
                "TLS certificate verification failed while validating Lidl auth. "
                "Use --insecure only if this is a controlled local trust-store issue."
            ) from None
        return False
    except Exception:
        return False
    return True


def resolve_login_credentials(args: argparse.Namespace) -> tuple[str, str]:
    email = args.email or os.environ.get("LIDL_EMAIL")
    password = os.environ.get("LIDL_PASSWORD")

    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\n")

    if not email:
        if sys.stdin.isatty():
            email = input("Lidl email: ").strip()
        else:
            raise SystemExit("Missing Lidl email. Provide --email or set LIDL_EMAIL.")
    if not password:
        if sys.stdin.isatty():
            password = getpass.getpass("Lidl password: ")
        else:
            raise SystemExit("Missing Lidl password. Set LIDL_PASSWORD or pass --password-stdin.")

    return email, password


def login_with_browser(args: argparse.Namespace) -> str:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SystemExit(
            "Credential login requires Playwright for Python. Install it with "
            "`python3 -m pip install playwright` and `python3 -m playwright install chromium`."
        ) from exc

    state_path = auth_state_path(args)
    headed = args.auth_headed or args.auth_interactive
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not headed, channel=args.auth_browser_channel)

        if state_path and state_path.exists():
            context = browser.new_context(locale="en-GB", storage_state=str(state_path))
            page = context.new_page()
            page.goto(LIDL_HOME_URL, wait_until="domcontentloaded", timeout=args.auth_timeout * 1000)
            try:
                page.wait_for_load_state("networkidle", timeout=10_000)
            except PlaywrightTimeoutError:
                pass
            cookie = cookie_header_from_cookies(context.cookies(["https://www.lidl.co.uk"]))
            if cookie and validate_cookie(args, cookie):
                browser.close()
                print_err(f"Using saved Lidl browser auth state from {state_path}.")
                return cookie
            context.close()
            print_err("Saved Lidl browser auth state is missing or expired; logging in again.")

        context = browser.new_context(locale="en-GB")
        page = context.new_page()
        page.goto(LIDL_HOME_URL, wait_until="domcontentloaded", timeout=args.auth_timeout * 1000)

        if args.auth_interactive:
            print_err(
                "Complete the Lidl login in the opened browser window. "
                "The script will continue after the browser reaches www.lidl.co.uk/mla/."
            )
        elif "accounts.lidl.com" in page.url:
            email, password = resolve_login_credentials(args)
            email_input = page.locator('[data-testid="input-email"], #input-email').first
            email_input.wait_for(state="visible", timeout=args.auth_timeout * 1000)
            email_input.fill(email)
            page.locator('[data-testid="login-or-register-submit-button"]').click(timeout=15_000)

            password_input = page.locator('[data-testid="login-input-password"], #Password').first
            password_input.wait_for(state="visible", timeout=args.auth_timeout * 1000)
            password_input.fill(password)
            page.locator('[data-testid="button-primary"]').click(timeout=15_000)

        try:
            page.wait_for_url("https://www.lidl.co.uk/mla/**", timeout=args.auth_timeout * 1000)
        except PlaywrightTimeoutError:
            pass
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except PlaywrightTimeoutError:
            pass

        cookie = cookie_header_from_cookies(context.cookies(["https://www.lidl.co.uk"]))
        if not cookie or not validate_cookie(args, cookie):
            current_url = page.url
            browser.close()
            raise SystemExit(
                "Lidl browser login did not produce an authenticated receipt session. "
                f"Current page: {current_url}. If Lidl shows a bot check or MFA challenge, rerun with "
                "--auth-interactive and complete the login manually once."
            )

        if state_path:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            context.storage_state(path=str(state_path))
            print_err(f"Saved Lidl browser auth state to {state_path}.")

        browser.close()
        return cookie


def require_cookie(args: argparse.Namespace) -> str:
    cached_cookie = getattr(args, "_cookie_value", None)
    if cached_cookie:
        return cached_cookie
    if args.cookie_stdin:
        cookie = sys.stdin.read().strip()
    else:
        cookie = args.cookie or os.environ.get("LIDL_COOKIE")
    if not cookie and args.login:
        cookie = login_with_browser(args)
    if not cookie:
        state_path = auth_state_path(args)
        if state_path:
            cookie = cookie_header_from_auth_state(state_path)
    if not cookie:
        raise SystemExit(
            "Missing Lidl authentication. Provide --cookie, set LIDL_COOKIE, or use --login with "
            "LIDL_EMAIL and LIDL_PASSWORD."
        )
    setattr(args, "_cookie_value", cookie)
    return cookie


def make_headers(cookie: str) -> dict[str, str]:
    return {
        "accept": "application/json",
        "accept-language": "en-GB,en;q=0.9",
        "referer": "https://www.lidl.co.uk/mre/purchase-history",
        "user-agent": "Mozilla/5.0",
        "cookie": cookie,
    }


def get_json(
    headers: dict[str, str],
    url: str,
    params: dict[str, str | int],
    limiter: RateLimiter,
    insecure: bool = False,
) -> Any:
    limiter.wait()
    encoded_params = urllib.parse.urlencode(params)
    request = urllib.request.Request(f"{url}?{encoded_params}", headers=headers, method="GET")
    context = ssl._create_unverified_context() if insecure else None
    try:
        with urllib.request.urlopen(request, timeout=30, context=context) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        if exc.code in {401, 403}:
            raise RuntimeError(f"HTTP {exc.code}: Lidl session cookie is expired or unauthorized") from exc
        raise RuntimeError(f"HTTP {exc.code}: {body[:200]}") from exc
    return json.loads(body)


def fetch_summaries(args: argparse.Namespace) -> dict[str, Any]:
    cookie = require_cookie(args)
    headers = make_headers(cookie)
    limiter = RateLimiter(args.rate)
    output_path = args.data_dir / "receipts_summaries.json"

    first_page = get_json(headers, SUMMARY_URL, {"country": args.country, "page": 1}, limiter, args.insecure)
    size = int(first_page.get("size") or len(first_page.get("items", [])) or 10)
    total_count = int(first_page.get("totalCount") or len(first_page.get("items", [])))
    total_pages = max(1, math.ceil(total_count / size))
    items = list(first_page.get("items", []))

    print(f"Summaries page 1/{total_pages}: {len(items)}/{total_count}")
    for page in range(2, total_pages + 1):
        data = get_json(headers, SUMMARY_URL, {"country": args.country, "page": page}, limiter, args.insecure)
        page_items = data.get("items", [])
        items.extend(page_items)
        print(f"Summaries page {page}/{total_pages}: {len(items)}/{total_count}", flush=True)

    export = {
        "fetched_at": utc_now(),
        "page": 1,
        "size": size,
        "totalCount": total_count,
        "items": items,
    }
    write_json(output_path, export)
    print(f"Saved {len(items)} summaries to {output_path}")
    return export


def load_summaries(data_dir: Path) -> dict[str, Any]:
    path = data_dir / "receipts_summaries.json"
    if not path.exists():
        raise SystemExit(f"Missing {path}. Run the summaries command first.")
    return read_json(path)


def summary_item_date(item: dict[str, Any]) -> datetime | None:
    return parse_datetime(item.get("date") or item.get("purchaseDate") or item.get("createdAt"))


def max_summary_date(export: dict[str, Any]) -> datetime | None:
    dates = [summary_item_date(item) for item in export.get("items", [])]
    return max((d for d in dates if d is not None), default=None)


def min_summary_date(items: list[dict[str, Any]]) -> datetime | None:
    dates = [summary_item_date(item) for item in items]
    return min((d for d in dates if d is not None), default=None)


def merge_summary_items(existing: list[dict[str, Any]], new_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for item in existing:
        receipt_id = item.get("id")
        if receipt_id:
            merged[receipt_id] = item
    for item in new_items:
        receipt_id = item.get("id")
        if receipt_id:
            merged[receipt_id] = item

    return sorted(
        merged.values(),
        key=lambda item: summary_item_date(item) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )


def fetch_summaries_after(args: argparse.Namespace, since: datetime) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    cookie = require_cookie(args)
    headers = make_headers(cookie)
    limiter = RateLimiter(args.rate)
    output_path = args.data_dir / "receipts_summaries.json"

    existing_export = load_summaries(args.data_dir)
    existing_items = list(existing_export.get("items", []))
    first_page = get_json(headers, SUMMARY_URL, {"country": args.country, "page": 1}, limiter, args.insecure)
    size = int(first_page.get("size") or len(first_page.get("items", [])) or 10)
    total_count = int(first_page.get("totalCount") or len(first_page.get("items", [])))
    total_pages = max(1, math.ceil(total_count / size))

    fetched_items: list[dict[str, Any]] = []
    page = 1
    while True:
        data = first_page if page == 1 else get_json(
            headers,
            SUMMARY_URL,
            {"country": args.country, "page": page},
            limiter,
            args.insecure,
        )
        page_items = list(data.get("items", []))
        fetched_items.extend(page_items)
        page_min = min_summary_date(page_items)
        print(
            f"Summaries page {page}/{total_pages}: {len(page_items)} items, min date {format_datetime(page_min)}",
            flush=True,
        )
        if page >= total_pages or (page_min is not None and since >= page_min):
            break
        page += 1

    new_items = [item for item in fetched_items if (summary_item_date(item) or datetime.min.replace(tzinfo=timezone.utc)) > since]
    merged_items = merge_summary_items(existing_items, new_items)
    export = {
        "fetched_at": utc_now(),
        "page": 1,
        "size": size,
        "totalCount": max(total_count, len(merged_items)),
        "items": merged_items,
    }
    write_json(output_path, export)
    print(f"Saved {len(merged_items)} summaries to {output_path}; new since checkpoint: {len(new_items)}")
    return export, new_items


def fetch_detail(headers: dict[str, str], receipt_id: str, args: argparse.Namespace, limiter: RateLimiter) -> Any:
    return get_json(
        headers,
        DETAIL_URL.format(ticket_id=receipt_id),
        {"country": args.country, "languageCode": args.language_code},
        limiter,
        args.insecure,
    )


def fetch_details(args: argparse.Namespace, receipt_ids: list[str] | None = None) -> None:
    cookie = require_cookie(args)
    if receipt_ids is None:
        export = load_summaries(args.data_dir)
        receipt_ids = [item["id"] for item in export.get("items", []) if item.get("id")]
    raw_dir = args.data_dir / "receipts"
    raw_dir.mkdir(parents=True, exist_ok=True)
    existing = {p.stem for p in raw_dir.glob("*.json") if p.name != "_manifest.json"}
    to_fetch = [rid for rid in receipt_ids if rid not in existing]

    print(f"Total: {len(receipt_ids)}, already fetched: {len(existing)}, remaining: {len(to_fetch)}")
    if not to_fetch:
        return

    headers = make_headers(cookie)
    limiter = RateLimiter(args.rate)
    errors: list[dict[str, str]] = []
    success = 0
    start = time.time()

    for index, receipt_id in enumerate(to_fetch, start=1):
        try:
            data = fetch_detail(headers, receipt_id, args, limiter)
            write_json(raw_dir / f"{receipt_id}.json", data)
            success += 1
        except Exception as exc:  # noqa: BLE001 - report and continue
            errors.append({"id": receipt_id, "error": f"{type(exc).__name__}: {exc}"})

        if index % 25 == 0 or index == len(to_fetch):
            elapsed = max(time.time() - start, 0.001)
            print(
                f"Details {index}/{len(to_fetch)} | OK:{success} ERR:{len(errors)} | {elapsed:.0f}s",
                flush=True,
            )

    manifest = {
        "fetched_at": utc_now(),
        "total_receipts": len(receipt_ids),
        "already_present_at_start": len(existing),
        "successfully_fetched_this_run": success,
        "errors": errors,
    }
    write_json(raw_dir / "_manifest.json", manifest)
    if errors:
        print("First errors:")
        for error in errors[:5]:
            print(f"  {error['id']}: {error['error']}")


def command_status(args: argparse.Namespace) -> None:
    export = load_summaries(args.data_dir)
    newest = max_summary_date(export)
    now = datetime.now(timezone.utc)
    age_hours = None if newest is None else round((now - newest).total_seconds() / 3600, 2)
    should_fetch = newest is None or now > newest + timedelta(hours=args.refresh_after_hours)
    print(
        json.dumps(
            {
                "now": format_datetime(now),
                "fetched_at": export.get("fetched_at"),
                "summary_count": len(export.get("items", [])),
                "max_receipt_date": format_datetime(newest),
                "max_receipt_age_hours": age_hours,
                "refresh_after_hours": args.refresh_after_hours,
                "should_fetch": should_fetch,
            },
            indent=2,
        )
    )


def command_summaries_since(args: argparse.Namespace) -> None:
    export = load_summaries(args.data_dir)
    since = parse_datetime(args.since) if args.since else max_summary_date(export)
    if since is None:
        raise SystemExit("No checkpoint date found. Run summaries first or pass --since YYYY-MM-DD.")
    fetch_summaries_after(args, since)


def receipt_date(receipt: dict[str, Any]) -> datetime | None:
    return parse_datetime(receipt.get("date"))


def load_parsed_receipts(data_dir: Path) -> dict[str, Any]:
    path = data_dir / "receipts_detail.json"
    if not path.exists():
        raise SystemExit(f"Missing {path}. Run the parse command first.")
    return read_json(path)


def filter_receipts(
    receipts: list[dict[str, Any]],
    start: datetime | None,
    end: datetime | None,
) -> list[dict[str, Any]]:
    selected = []
    for receipt in receipts:
        dt = receipt_date(receipt)
        if dt is None:
            continue
        if start is not None and dt < start:
            continue
        if end is not None and dt >= end:
            continue
        selected.append(receipt)
    return sorted(selected, key=lambda receipt: receipt_date(receipt) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)


def compact_receipt(receipt: dict[str, Any], include_articles: bool) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": receipt.get("id"),
        "date": receipt.get("date"),
        "store_name": receipt.get("store_name"),
        "total_amount": receipt.get("total_amount"),
        "article_count": receipt.get("article_count"),
        "discount_count": receipt.get("discount_count"),
    }
    if include_articles:
        result["articles"] = [
            {
                "description": article.get("description"),
                "quantity": article.get("quantity"),
                "unit_price": article.get("unit_price"),
                "line_total": article.get("line_total"),
            }
            for article in receipt.get("articles", [])
        ]
        result["discounts"] = receipt.get("discounts", [])
    return result


def command_query(args: argparse.Namespace) -> None:
    parsed = load_parsed_receipts(args.data_dir)
    now = datetime.now(timezone.utc)
    start = parse_datetime(args.start) if args.start else None
    end = parse_datetime(args.end) if args.end else None
    if args.days is not None:
        start = now - timedelta(days=args.days)
    selected = filter_receipts(parsed.get("receipts", []), start, end)
    output = {
        "start": format_datetime(start),
        "end": format_datetime(end),
        "receipt_count": len(selected),
        "total_spent": round(sum(receipt.get("total_amount") or 0 for receipt in selected), 2),
        "receipts": [compact_receipt(receipt, args.include_articles) for receipt in selected],
    }
    print(json.dumps(output, indent=2, ensure_ascii=False))


def command_update(args: argparse.Namespace) -> None:
    export = load_summaries(args.data_dir)
    checkpoint = max_summary_date(export)
    if checkpoint is None:
        raise SystemExit("No checkpoint date found. Run summaries first.")

    _, new_items = fetch_summaries_after(args, checkpoint)
    new_ids = [item["id"] for item in new_items if item.get("id")]
    if new_ids:
        fetch_details(args, new_ids)
        parse_receipts(args)
        parsed = load_parsed_receipts(args.data_dir)
        new_id_set = set(new_ids)
        selected = [receipt for receipt in parsed.get("receipts", []) if receipt.get("id") in new_id_set]
        selected = sorted(
            selected,
            key=lambda receipt: receipt_date(receipt) or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        output = {
            "checkpoint": format_datetime(checkpoint),
            "new_receipt_count": len(selected),
            "total_spent": round(sum(receipt.get("total_amount") or 0 for receipt in selected), 2),
            "receipts": [compact_receipt(receipt, args.include_articles) for receipt in selected],
        }
        print(json.dumps(output, indent=2, ensure_ascii=False))
    else:
        print("No new receipt summaries found.")


def command_auth_check(args: argparse.Namespace) -> None:
    cookie = require_cookie(args)
    data = get_json(
        make_headers(cookie),
        SUMMARY_URL,
        {"country": args.country, "page": 1},
        RateLimiter(args.rate),
        args.insecure,
    )
    items = list(data.get("items", []))
    print(
        json.dumps(
            {
                "authenticated": True,
                "totalCount": data.get("totalCount"),
                "page_size": data.get("size"),
                "first_receipt_date": items[0].get("date") if items else None,
            },
            indent=2,
        )
    )


def parse_float(value: str | None) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def parse_html_receipt(receipt_html: str) -> dict[str, Any]:
    h = html.unescape(receipt_html)
    articles: list[dict[str, Any]] = []

    article_pattern = re.compile(r'<span[^>]*class="[^"]*\barticle\b[^"]*"[^>]*>(.*?)</span>', re.DOTALL)
    for match in article_pattern.finditer(h):
        span_content = match.group(1)
        full_span = match.group(0)
        raw_visible = re.sub(r"<[^>]+>", "", span_content)
        visible = raw_visible.strip()

        if raw_visible and raw_visible[0].isspace():
            continue
        if visible in {"", "£"}:
            continue

        art_id = re.search(r'data-art-id="([^"]*)"', full_span)
        desc = re.search(r'data-art-description="([^"]*)"', full_span)
        unit_price = re.search(r'data-unit-price="([^"]*)"', full_span)
        tax_type = re.search(r'data-tax-type="([^"]*)"', full_span)
        quantity = re.search(r'data-art-quantity="([^"]*)"', full_span)
        total_match = re.search(r"(-?\d+\.\d{1,2})\s*(?:[A-Z])?\s*$", visible)

        articles.append(
            {
                "article_id": art_id.group(1) if art_id else None,
                "description": desc.group(1) if desc else None,
                "quantity": parse_float(quantity.group(1) if quantity else None) or 1.0,
                "unit_price": parse_float(unit_price.group(1) if unit_price else None),
                "line_total": parse_float(total_match.group(1) if total_match else None),
                "tax_type": tax_type.group(1) if tax_type else None,
            }
        )

    discounts: list[dict[str, Any]] = []
    discount_pattern = re.compile(
        r'<span[^>]*class="[^"]*\bdiscount\b[^"]*\bcss_bold\b[^"]*"[^>]*data-promotion-id="([^"]*)"[^>]*>'
        r"(.*?)</span>",
        re.DOTALL,
    )
    pending_labels: dict[str, str] = {}
    for discount in discount_pattern.finditer(h):
        promotion_id = discount.group(1)
        text = re.sub(r"<[^>]+>", "", discount.group(2)).strip()
        amount_text = text.replace("£", "")
        if re.fullmatch(r"-?\d+\.\d{1,2}", amount_text):
            discounts.append(
                {
                    "promotion_id": promotion_id,
                    "label": pending_labels.get(promotion_id),
                    "amount": float(amount_text),
                }
            )
        elif text:
            pending_labels[promotion_id] = text

    article_total = sum(a["line_total"] for a in articles if a["line_total"] is not None)
    discount_total = sum(d["amount"] for d in discounts if d["amount"] is not None)
    computed_total = round(article_total + discount_total, 2)
    summary_start = h.find("purchase_summary")
    summary_end = h.find("purchase_tender_information")
    summary_section = h[summary_start:summary_end] if summary_start != -1 and summary_end != -1 else h
    html_total_match = re.search(r"TOTAL.*?(\d+\.\d{1,2})", summary_section, re.DOTALL)
    html_total = parse_float(html_total_match.group(1) if html_total_match else None)
    total_amount = computed_total if html_total is None or abs(computed_total - html_total) <= 0.10 else html_total

    tender_match = re.search(r'data-tender-description="([^"]*)"', h)
    card_match = re.search(r"\*{6,}(\d{4})", h)
    vat_items = []
    for vat in re.finditer(
        r'data-tax-type="([^"]*)"[^>]*data-tax-percentage="([^"]*)"[^>]*'
        r'data-tax-base-amount="([^"]*)"[^>]*data-tax-amount="([^"]*)"',
        h,
    ):
        vat_items.append(
            {
                "tax_type": vat.group(1),
                "percentage": float(vat.group(2)),
                "base_amount": float(vat.group(3)),
                "tax_amount": float(vat.group(4)),
            }
        )

    return {
        "articles": articles,
        "discounts": discounts,
        "total_amount": total_amount,
        "payment_method": tender_match.group(1) if tender_match else None,
        "card_last4": card_match.group(1) if card_match else None,
        "vat_breakdown": vat_items,
        "article_count": len(articles),
        "discount_count": len(discounts),
    }


def parse_receipts(args: argparse.Namespace) -> None:
    export = load_summaries(args.data_dir)
    meta_lookup = {item["id"]: item for item in export.get("items", []) if item.get("id")}
    raw_files = sorted(
        Path(path)
        for path in glob.glob(str(args.data_dir / "receipts" / "*.json"))
        if not path.endswith("_manifest.json")
    )
    parsed = []
    errors = []

    for raw_file in raw_files:
        receipt_id = raw_file.stem
        meta = meta_lookup.get(receipt_id, {})
        try:
            data = read_json(raw_file)
            ticket = data.get("ticket", {})
            receipt_html = ticket.get("htmlPrintedReceipt") or ""
            store = ticket.get("store") or {}
            if not receipt_html:
                errors.append({"id": receipt_id, "error": "no htmlPrintedReceipt"})
                continue
            result = parse_html_receipt(receipt_html)
            parsed.append(
                {
                    "id": receipt_id,
                    "date": ticket.get("date") or meta.get("date"),
                    "store_name": store.get("name") or meta.get("store"),
                    "store_address": store.get("address"),
                    "store_postcode": store.get("postalCode"),
                    "locality": store.get("locality"),
                    "total_amount": result["total_amount"] if result["total_amount"] is not None else meta.get("totalAmount"),
                    "payment_method": result["payment_method"],
                    "card_last4": result["card_last4"],
                    "vat_breakdown": result["vat_breakdown"],
                    "loyalty_points": (data.get("collectingModel") or {}).get("points", 0),
                    "articles": result["articles"],
                    "discounts": result["discounts"],
                    "article_count": result["article_count"],
                    "discount_count": result["discount_count"],
                }
            )
        except Exception as exc:  # noqa: BLE001 - keep parsing remaining files
            errors.append({"id": receipt_id, "error": f"{type(exc).__name__}: {exc}"})

    total_articles = sum(r["article_count"] for r in parsed)
    total_discounts = sum(r["discount_count"] for r in parsed)
    total_spent = round(sum(r["total_amount"] or 0 for r in parsed), 2)
    output = {
        "parsed_at": utc_now(),
        "total_receipts": len(parsed),
        "total_articles": total_articles,
        "total_discounts": total_discounts,
        "total_spent": total_spent,
        "receipts": parsed,
    }
    write_json(args.data_dir / "receipts_detail.json", output)
    print(f"Parsed {len(parsed)} receipts to {args.data_dir / 'receipts_detail.json'}")
    print(f"Total articles: {total_articles}, discounts: {total_discounts}, spent: GBP {total_spent:.2f}")
    if errors:
        print("First parse errors:")
        for error in errors[:5]:
            print(f"  {error['id']}: {error['error']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fetch and parse Lidl UK digital receipts.")
    parser.add_argument(
        "command",
        choices=["auth-check", "summaries", "summaries-since", "details", "parse", "all", "update", "status", "query"],
    )
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--country", default="GB")
    parser.add_argument("--language-code", default="en-GB")
    parser.add_argument("--cookie", help="Full Lidl Cookie header. Prefer LIDL_COOKIE instead.")
    parser.add_argument("--cookie-stdin", action="store_true", help="Read the full Lidl Cookie header from stdin.")
    parser.add_argument("--login", action="store_true", help="Use Playwright to log in with Lidl credentials.")
    parser.add_argument("--email", help="Lidl login email. Prefer LIDL_EMAIL for agent runs.")
    parser.add_argument("--password-stdin", action="store_true", help="Read the Lidl password from the first stdin line.")
    parser.add_argument(
        "--auth-state",
        type=Path,
        help="Playwright storage-state path. Defaults to data/lidl_auth_state.json when --login is used.",
    )
    parser.add_argument("--no-auth-state", action="store_true", help="Do not read or write browser auth state.")
    parser.add_argument("--auth-headed", action="store_true", help="Show the browser during credential login.")
    parser.add_argument(
        "--auth-interactive",
        action="store_true",
        help="Open a browser and wait for the user to complete Lidl login manually, then save auth state.",
    )
    parser.add_argument(
        "--auth-browser-channel",
        help="Optional Playwright browser channel for login, for example chrome.",
    )
    parser.add_argument("--auth-timeout", type=int, default=90, help="Browser login timeout in seconds.")
    parser.add_argument("--rate", type=float, default=3.0, help="Maximum API requests per second.")
    parser.add_argument("--insecure", action="store_true", help="Disable TLS certificate verification.")
    parser.add_argument("--since", help="Checkpoint date for summaries-since, for example 2026-05-07.")
    parser.add_argument("--refresh-after-hours", type=float, default=6.0)
    parser.add_argument("--start", help="Inclusive query start date/datetime, for example 2026-05-07.")
    parser.add_argument("--end", help="Exclusive query end date/datetime, for example 2026-05-08.")
    parser.add_argument("--days", type=float, help="Query receipts from the last N days.")
    parser.add_argument("--include-articles", action="store_true", help="Include article and discount lines in query output.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.data_dir = args.data_dir.expanduser().resolve()

    if args.command in {"summaries", "all"}:
        fetch_summaries(args)
    if args.command == "summaries-since":
        command_summaries_since(args)
    if args.command in {"details", "all"}:
        fetch_details(args)
    if args.command in {"parse", "all"}:
        parse_receipts(args)
    if args.command == "update":
        command_update(args)
    if args.command == "auth-check":
        command_auth_check(args)
    if args.command == "status":
        command_status(args)
    if args.command == "query":
        command_query(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
