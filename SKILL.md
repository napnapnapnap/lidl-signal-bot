---
name: lidl
description: Fetch, resume, and parse Lidl UK digital receipts from lidl.co.uk purchase-history API responses. Use when a user asks to download Lidl receipt summaries, fetch receipt JSON details, parse htmlPrintedReceipt into structured articles/discounts/VAT/payment data, or create/update local Lidl receipt export files under ./data.
---

# Lidl

## Overview

Use this skill to export a user's Lidl UK receipt history into deterministic local JSON files:

- `./data/receipts_summaries.json` for paginated receipt summaries.
- `./data/receipts/{id}.json` for each raw receipt detail response.
- `./data/receipts_detail.json` for parsed receipt, article, discount, VAT, payment, and spend data.

Use the authentication method that fits the runtime. If the agent is definitely running on a machine with an interactive browser UI, Playwright browser auth state can avoid repeated cookie pasting. If the agent is running on a VM, container, remote worker, CI job, or any environment without browser UI, copied-cookie mode is the correct path. Do not hardcode or commit credentials, cookies, tokens, or auth state.

## Workflow

For local analysis questions, use existing JSON files first. Only authenticate when an API refresh is needed.

Common fast paths:

- "What did I buy yesterday / in the past few days?" Run `query` against `./data/receipts_detail.json`; do not call the Lidl API unless the user asks for a refresh or the requested date range may not be present locally.
- "Have I bought anything since last time we checked?" or "Show me what I bought since last time we checked" Run `update` with the appropriate auth option once. It loads `./data/receipts_summaries.json`, finds the max date among `items`, fetches summary pages only until that checkpoint date is covered, fetches details only for new receipt ids, parses, prints the new receipts, then stops.
- "Should I refresh?" Run `status`. It prints current UTC time, max receipt date, and whether the max receipt date is older than `--refresh-after-hours` (default `6`).

Authentication decision:

- If the command does not need Lidl API access, do not authenticate.
- If a valid copied cookie is already available in context, use `--cookie-stdin` or `LIDL_COOKIE`.
- If the agent is on a VM/headless/remote environment without browser UI, ask the user for the full `Cookie` request header from a logged-in Lidl browser request and use `--cookie-stdin` or `LIDL_COOKIE`.
- If, and only if, the agent is definitely on a local machine with interactive browser UI, use `--login` to reuse `./data/lidl_auth_state.json`. If that state is missing or expired, use `auth-check --login --auth-interactive` and ask the user to complete the login in the opened browser.
- Do not attempt headless credential login as the primary strategy. Lidl may reject automated credential submission with `Oops! something went wrong, please try again later.`

Full export workflow:

1. Choose the auth option using the authentication decision above.
2. For VM/headless runs, get a fresh full `Cookie` request header from the user and pass it via `--cookie-stdin` or `LIDL_COOKIE`.
3. For local interactive-browser runs, bootstrap or reuse `./data/lidl_auth_state.json` with `--login`.
4. Fetch summaries first. The script reads `totalCount` and page `size` to request every summary page.
5. Fetch detail JSON next. The script skips existing `./data/receipts/{id}.json` files, so interrupted runs can resume.
6. Parse the saved raw details into `./data/receipts_detail.json`.

Authentication notes:

- Lidl UK uses an OpenID Connect authorization-code flow with PKCE through `accounts.lidl.com`.
- Successful login redirects back to `www.lidl.co.uk/user-api/signin-oidc`, which sets first-party cookies used by `/mre/api/v1/tickets`.
- Relevant post-login receipt cookies include `ldi-user-context`, `authToken`, `ldi-session-info`, `ldi-customertoken`, `tracking-info`, and `customer-info`.
- Headless credential submission may be rejected by Lidl's bot checks with `Oops! something went wrong, please try again later.` Do not try to bypass that.
- On VMs or headless agent environments without browser UI, copied-cookie mode is the recommended path. Ask the user for the full `Cookie` request header and pass it through `LIDL_COOKIE` or `--cookie-stdin`.
- For agent credentials, prefer `LIDL_EMAIL` and `LIDL_PASSWORD` environment variables or `--email` plus `--password-stdin`; never store passwords in the repo.

## Commands

Use `scripts/lidl_receipts.py`:

VM/headless or remote-agent mode:

```bash
python3 scripts/lidl_receipts.py all --cookie-stdin
```

Local interactive-browser mode:

```bash
python3 scripts/lidl_receipts.py auth-check --login --auth-interactive --auth-browser-channel chrome
python3 scripts/lidl_receipts.py all --login
```

Useful subcommands:

```bash
python3 scripts/lidl_receipts.py auth-check [AUTH_OPTION]
python3 scripts/lidl_receipts.py summaries [AUTH_OPTION]
python3 scripts/lidl_receipts.py update [AUTH_OPTION] --include-articles
python3 scripts/lidl_receipts.py summaries-since [AUTH_OPTION]
python3 scripts/lidl_receipts.py details [AUTH_OPTION]
python3 scripts/lidl_receipts.py parse
python3 scripts/lidl_receipts.py status
python3 scripts/lidl_receipts.py query --start 2026-05-07 --end 2026-05-08 --include-articles
python3 scripts/lidl_receipts.py query --days 3 --include-articles
```

Use `[AUTH_OPTION]` as one of:

- `--cookie-stdin` when the user supplies the full Cookie header through stdin.
- no explicit option when `LIDL_COOKIE` is set in the environment.
- `--login` only on a machine with interactive browser UI or an existing valid `./data/lidl_auth_state.json`.

When an agent already has the cookie in conversation context, prefer stdin to avoid putting the cookie on the process command line:

```bash
python3 scripts/lidl_receipts.py all --cookie-stdin
```

Default options:

- Data directory: `./data`
- Country: `GB`
- Language code: `en-GB`
- Rate limit: `3` requests/second
- Summary endpoint: `https://www.lidl.co.uk/mre/api/v1/tickets?country=GB&page={page}`
- Detail endpoint: `https://www.lidl.co.uk/mre/api/v1/tickets/{id}?country=GB&languageCode=en-GB`

Use `--data-dir`, `--country`, `--language-code`, or `--rate` only when the user asks or local context requires it.

Use `--insecure` only when the local Python TLS trust store rejects the connection with a certificate-chain error in a controlled environment.

## Efficient Query Recipes

For "since last time we checked":

```bash
python3 scripts/lidl_receipts.py update [AUTH_OPTION] --include-articles
```

If you only need to know whether a refresh is likely needed:

```bash
python3 scripts/lidl_receipts.py status
```

For date-range questions, calculate explicit date boundaries and use `query`. `--start` is inclusive and `--end` is exclusive:

```bash
python3 scripts/lidl_receipts.py query --start 2026-05-09 --end 2026-05-10 --include-articles
```

## Output Contract

The parsed output should contain:

- `parsed_at`, `total_receipts`, `total_articles`, `total_discounts`, `total_spent`
- `receipts[]` entries with `id`, `date`, store fields, `total_amount`, `payment_method`, `card_last4`, `vat_breakdown`, `loyalty_points`, `articles`, `discounts`, `article_count`, and `discount_count`

Parsing notes:

- Parse article rows from `<span class="article">` elements and skip weight continuation rows whose visible text starts with whitespace.
- Parse discounts sequentially from `<span class="discount css_bold">` rows instead of grouping only by promotion id.
- Prefer computed totals from article line totals plus discounts when close to the displayed total, because some Lidl HTML total spans truncate.
- Extract payment method from `data-tender-description` and card last 4 from masked card patterns such as `***********0615`.
- Extract VAT from `data-tax-type`, `data-tax-percentage`, `data-tax-base-amount`, and `data-tax-amount`.

## Failure Handling

- If an API call returns `401` or `403`, refresh the chosen auth method. On VM/headless runs, ask for a fresh copied Cookie header. On local interactive-browser runs, rerun `auth-check --login --auth-interactive`.
- If detail fetching stops partway through, rerun `details` or `all`; existing receipt files are skipped.
- If parsing reports missing HTML receipts, keep the raw JSON files and summarize the affected receipt ids.
- Keep credentials, cookies, tokens, and auth state out of commits and final answers.
