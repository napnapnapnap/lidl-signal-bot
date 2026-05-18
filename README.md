# Lidl Receipt Skill

An agent skill for exporting and parsing Lidl UK digital receipts from the Lidl purchase-history API.

The skill downloads receipt summaries, resumes missing receipt detail downloads, and parses each receipt's `htmlPrintedReceipt` into structured JSON for analysis.

## What It Creates

- `data/receipts_summaries.json`: paginated receipt summaries from Lidl.
- `data/receipts/{id}.json`: raw JSON detail response for each receipt.
- `data/receipts_detail.json`: parsed receipt, article, discount, VAT, payment, and spend data.

The `data/` directory is ignored by git because it contains personal receipt data.

## Skill Layout

```text
.
├── SKILL.md
├── README.md
├── scripts/
│   └── lidl_receipts.py
└── .gitignore
```

`SKILL.md` is the portable agent skill entrypoint. `scripts/lidl_receipts.py` is the deterministic helper used by agents to fetch and parse the data.

## Requirements

- Python 3.10 or newer.
- For automatic login: Python Playwright with a browser installed.
- For legacy cookie mode: a logged-in Lidl UK browser session and a fresh `Cookie` request header copied from Chrome DevTools.

Most receipt parsing uses only Python's standard library. Browser login requires Playwright:

```bash
python3 -m pip install playwright
python3 -m playwright install chromium
```

## Smoke Test

```bash
make smoke
```

## Usage

From the repo root:

```bash
python3 scripts/lidl_receipts.py auth-check --login --auth-interactive --auth-browser-channel chrome
python3 scripts/lidl_receipts.py update --login --include-articles
```

The first command opens a browser, lets the user complete Lidl login manually, validates receipt API access, and saves Playwright storage state to `data/lidl_auth_state.json`. Later commands reuse that saved state and do not need cookie pasting.

Subcommands:

```bash
python3 scripts/lidl_receipts.py auth-check --login
python3 scripts/lidl_receipts.py summaries --login
python3 scripts/lidl_receipts.py update --login --include-articles
python3 scripts/lidl_receipts.py summaries-since --login
python3 scripts/lidl_receipts.py details --login
python3 scripts/lidl_receipts.py parse
python3 scripts/lidl_receipts.py status
python3 scripts/lidl_receipts.py query --days 3 --include-articles
```

`update` is optimized for "since last time we checked" questions. It reads the current `data/receipts_summaries.json`, uses the newest saved summary date as the checkpoint, fetches only enough paginated summary pages to cover newer receipts, downloads details only for new ids, reparses, and prints the new receipts.

`query` is optimized for local date-range questions and does not call Lidl:

```bash
python3 scripts/lidl_receipts.py query --start 2026-05-09 --end 2026-05-10 --include-articles
```

If an agent already has the cookie in context, it can avoid putting the cookie on the process command line:

```bash
python3 scripts/lidl_receipts.py all --cookie-stdin
```

Legacy copied-cookie mode still works:

```bash
LIDL_COOKIE='copy the full Cookie header here' python3 scripts/lidl_receipts.py all
```

This is the recommended fallback for headless VMs or remote agents where no interactive browser UI is available:

```bash
python3 scripts/lidl_receipts.py update --include-articles --cookie-stdin
```

## Lidl Authentication

The Lidl UK site uses an OpenID Connect authorization-code flow with PKCE. Visiting `https://www.lidl.co.uk/mla/` redirects to `https://accounts.lidl.com/Account/Login?...`, then a successful login redirects back to `https://www.lidl.co.uk/user-api/signin-oidc`. The Lidl site then sets first-party cookies on `www.lidl.co.uk`; the receipt endpoints under `/mre/api/v1/tickets` authenticate with those cookies.

The important post-login cookies observed for receipt access include `ldi-user-context`, `authToken`, `ldi-session-info`, `ldi-customertoken`, `tracking-info`, and `customer-info`. The script does not store or print a raw Cookie header. It stores Playwright browser storage state under `data/lidl_auth_state.json`, which is git-ignored with the rest of `data/`.

Pure headless credential submission can be rejected by Lidl's anti-bot checks with `Oops! something went wrong, please try again later.` When that happens, use the interactive login bootstrap:

```bash
python3 scripts/lidl_receipts.py auth-check --login --auth-interactive --auth-browser-channel chrome --auth-timeout 180
```

For agent use, prefer this setup:

- First run: ask the user to complete `auth-check --login --auth-interactive` in the opened browser.
- Later runs: use `--login`; the script reuses `data/lidl_auth_state.json`.
- If the cached state expires: repeat the interactive bootstrap.
- On VMs or headless agent environments without browser UI: ask the user for a fresh full `Cookie` request header and pass it through `LIDL_COOKIE` or `--cookie-stdin`.
- If explicit credentials are appropriate for the environment: pass the email via `LIDL_EMAIL` and the password via `LIDL_PASSWORD`, or use `--email` with `--password-stdin`. Do not put passwords in README examples, shell history, commits, or final answers.

## Options

- `--data-dir`: output directory, default `data`.
- `--country`: country code, default `GB`.
- `--language-code`: receipt language, default `en-GB`.
- `--cookie`: full Lidl Cookie header. Prefer `LIDL_COOKIE` instead.
- `--cookie-stdin`: read the full Lidl Cookie header from stdin.
- `--login`: derive a Cookie header with Playwright browser auth or cached storage state.
- `--email`: Lidl login email. Prefer `LIDL_EMAIL` for agent runs.
- `--password-stdin`: read the Lidl password from stdin.
- `--auth-state`: custom Playwright storage-state path, default `data/lidl_auth_state.json`.
- `--no-auth-state`: do not read or write browser auth state.
- `--auth-headed`: show the browser during automated credential login.
- `--auth-interactive`: open a browser and wait for the user to complete login manually.
- `--auth-browser-channel`: optional Playwright browser channel, for example `chrome`.
- `--rate`: maximum API requests per second, default `3`.
- `--insecure`: disable TLS certificate verification only when the local Python trust store rejects the connection in a controlled environment.
- `--refresh-after-hours`: age threshold used by `status`, default `6`.
- `--start` / `--end`: inclusive start and exclusive end for `query`.
- `--days`: query receipts from the last N days.
- `--include-articles`: include article and discount lines in `query` or `update` output.

## Privacy And Safety

Do not commit credentials, cookies, access tokens, Playwright auth state, raw receipts, or parsed receipt data.

The script skips existing files under `data/receipts/`, so interrupted detail downloads can be resumed safely.

## Disclaimer

This project is unofficial and is not affiliated with, endorsed by, or supported by Lidl.
