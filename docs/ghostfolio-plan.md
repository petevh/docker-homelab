# Ghostfolio: plan and spec

Status: planned, nothing deployed (31/10/2026). Findings below come from a desktop web search and the Ghostfolio and Export-To-Ghostfolio READMEs; items marked **verify** were not confirmed and must be tested before relying on them.

## Goal

One self-hosted place to see positions, movements and performance for:

- **Pete:** all Interactive Brokers (IBKR) positions and movements, plus older pension-fund holdings now entered by hand in Sharesight.
- **Parents:** Allan Gray, EasyEquities, Sygnia and Computershare (Julia's shares, today monitored in Sharesight).

It replaces Sharesight for monitoring, and the Yahoo Finance widget lists that need manual updating. Tax reports stay in Sharesight until Ghostfolio is proven (see Gaps).

## What Ghostfolio gives us

- Open source (AGPLv3), Docker images, needs PostgreSQL and Redis.
- Accounts, activities (BUY, SELL, DIVIDEND, FEE, INTEREST, LIABILITY), multi-currency, performance as return on average investment over day/week/month/year/5-year/max periods, allocation and risk views, mobile friendly.
- **Import API:** `POST /api/v1/import` with a JSON `activities` array (symbol, dataSource, date, type, quantity, unitPrice, fee, currency, optional account and comment). Duplicates are rejected, so a re-run does not double count. `GET /api/v1/export` returns everything. Auth is a bearer token obtained with the user's security token.
- **Prices:** Yahoo Finance (free default), CoinGecko, and a `MANUAL` source for assets with no ticker (user-entered prices, or a scraper configuration that reads a price from a URL; **verify** the exact fields on our version).
- **Sharing:** per-user portfolios and an Access tab that grants other people restricted or full views (the experimental MCP access type is separate and not needed).
- Experimental OIDC login (could sit behind our identity setup later).

## Automation per source

| Source | What exists | Plan | Confidence |
|---|---|---|---|
| IBKR | Flex Web Service: a token plus saved query ids return Trades and Cash Transactions reports by HTTP. The community tool Export-To-Ghostfolio converts IBKR Flex CSV (Trades: buy/sell, date, ISIN, quantity, price, commission; Cash Transactions: dividends) into Ghostfolio import JSON and can call the import API. | n8n schedule: fetch both Flex reports with the token, convert (reuse or port Export-To-Ghostfolio's mapping, ISIN to Yahoo symbol), POST to the import API. The token is read-only. | High, **verify** corporate actions, FX trades, fees and non-Yahoo listings on real data. |
| Computershare | No API or CSV found; Investor Centre appears to offer PDF exports for transaction and dividend history. Plan shares may sit on a separate portal. | Start with a one-off manual import of history; then monthly: statement PDFs through the existing bank-statements workflow, parsed in this repository's parser style. Alternatively read Sharesight's API for the existing history (Sharesight offers API access on request for Investor plans and above; trade listing via the older v2 routes per a community client, **verify**). | Low to medium |
| EasyEquities | Statements and full transaction history can be downloaded from the platform; CSV not confirmed. Unofficial Python clients (`easy-equities-client`, `pyeasyequities`) log in with username and password and read holdings and transactions; one is unmaintained. | Prefer a manual monthly export. Avoid storing the parents' password in n8n for an unofficial client unless there is no other way. | Low to medium |
| Allan Gray | Public daily unit trust prices page; no transaction export or API found. | Prices: scrape or manual source. Transactions: statements (PDF) or manual entry; check the client portal for exports. | Low |
| Sygnia | No export, API or price feed found in the search. | As Allan Gray; check the portal and the fund price pages. | Low |
| Pension funds (history) | Entered by hand in Sharesight. | One-off: export from Sharesight (or its API) and load as manual assets. | Medium |

The honest summary: IBKR can be fully automated; the South African platforms need a monthly manual export or statement parsing, which is acceptable for a long-term portfolio that changes slowly.

## Quantities versus prices (South African portfolios)

Decision (31/10/2026): the parents' portfolios change slowly, so manual statement imports are acceptable. The two things that move are handled separately:

- **Prices** change daily and must be automated (see Pricing). Unit trust prices are the main item.
- **Quantities** change only on events: contributions, withdrawals, switches, and distributions reinvested as extra units. Allan Gray unit counts therefore drift between statements even when nobody does anything. Each statement import records these events (BUY, SELL, and DIVIDEND where the distribution is shown separately).
- **Monthly unit check:** compare each fund's units on the statement with Ghostfolio's units. Any difference is a missed event; book it as an adjusting BUY or SELL dated at the statement date, and note it in the comment. This is the check that catches missed distributions, and it costs one comparison per fund per statement.
- **Price source: verify.** Allan Gray publishes daily unit prices on its website. Whether there is a downloadable or machine-readable price file, or a listing with Yahoo or another feed by ISIN or fund code, is not confirmed. Check this first, because it decides whether the prices workflow is a scrape or a feed.

## Pricing

- IBKR securities: Yahoo Finance through ISIN-to-symbol mapping (as in Export-To-Ghostfolio). JSE listings are expected to use the `.JO` suffix; **verify** coverage and currency (ZAR versus ZAc) for the specific holdings.
- Unit trusts and pension funds (Allan Gray, Sygnia, others): `MANUAL` data source. Daily price feed from n8n (a small workflow that reads the fund price pages and pushes market data), or Ghostfolio's scraper configuration if it handles the pages. Needs one asset profile per fund.
- Cash accounts and a loan or two (LIABILITY) can be tracked as accounts; our wealth-ledger project remains the place for property and net worth.

## Users and access

- One Ghostfolio user per household portfolio: Pete, and a parents' user (Julia and Rob), each with its own base currency (ZAR for the parents, a currency for Pete to be decided).
- Parents get a login (or a share link) with restricted or full view; no write access unless they ask for it. Pete keeps the admin role.
- Multiple accounts inside a user: one per platform (IBKR, Allan Gray, EasyEquities, Sygnia, Computershare, pensions), so allocation by platform is visible.

## Deployment (docker-homelab)

- New directory `ghostfolio/` with its own `docker-compose.yml`: `ghostfolio`, `postgres` (15 or newer), `redis`, on the existing network behind Traefik with a hostname on our domain.
- Secrets (JWT secret, access token salt, database password) in the same secrets pattern as the other services, never committed.
- Backups: nightly `pg_dump` to the NAS share with the others; test one restore.
- Update strategy: pinned image tag, same as the other services.
- Resource use is small (a Node app, Postgres, Redis).

## n8n workflows to add

1. **IBKR sync** (daily): Flex fetch (two queries), convert, import, summary email with counts (new, duplicate, rejected).
2. **Prices** (daily): unit trust and pension fund prices into the `MANUAL` assets.
3. **Statement intake** (monthly, as needed): the existing bank-statements workflow files Allan Gray, Sygnia, EasyEquities and Computershare statements to OneDrive; a parser turns them into the import JSON (patterns kept in bank-automation, private details in the private config).
4. A reconciliation check: each month compare Ghostfolio's total per account with the platform's statement total and report differences, in the same spirit as the YNAB dry run.

## Parallel run and exit criteria

Run Ghostfolio beside Sharesight for at least a quarter. Switch the monitoring over when, for each portfolio, positions and total value match Sharesight within a rounding difference, dividends match, and the manual feeds have run without gaps for a month. Keep Sharesight for tax reports until the gaps below are settled.

## Gaps and risks

- **Tax and dividend reporting:** Ghostfolio is not tax software. Sharesight's tax and capital gains reports (and foreign income handling) have no equivalent here; **verify** what the accountant needs and whether the wealth-ledger project should produce it.
- **Performance metric:** Ghostfolio reports return on average investment; Sharesight reports money-weighted and time-weighted returns. Numbers will differ; agree which one is the reference.
- **Corporate actions and splits:** manual handling may be needed.
- **Unofficial EasyEquities clients** can break or be blocked, and need the parents' credentials.
- **Data in two places:** the wealth-ledger design plans to import positions and values from Ghostfolio, not every trade.
- **Privacy:** the parents' portfolio is sensitive; the instance is not exposed publicly beyond authenticated access.

## Alternatives looked at

| Tool | Notes | Verdict |
|---|---|---|
| **Ghostfolio** | Web app, Docker, Postgres plus Redis, import API, Yahoo plus manual prices, sharing between users. | Chosen to start. |
| **Wealthfolio** | Open source, self-hostable, richer analytics (reported). Broker sync is a paid cloud service (Wealthfolio Connect, reportedly through SnapTrade), which is not self-hosted; CSV import is free (an IBKR export can be imported). | Worth a trial for analytics; the broker sync defeats the self-hosting goal. |
| **Portfolio Performance** | Free desktop application, strong performance maths (TTWROR and IRR), imports IBKR Flex XML directly. Not a web service, so sharing with the parents is by file. | Good as a cross-check of the numbers, not as the dashboard. |
| **Sharesight** | Today's tool; hosted, not self-hostable, has an API on request. | Keep for tax reports during the parallel run. |
| Others (Maybe, Beancount/Fava, Firefly III) | Not investigated. | Open. |

## Tasks

- [ ] Read the Ghostfolio compose file and docs for the current version; decide the image tag.
- [ ] Create `ghostfolio/` in docker-homelab (compose, secrets pattern, Traefik labels, backup job).
- [ ] Create users (Pete, parents) and one account per platform.
- [ ] IBKR: create the two Flex Queries and the token; test the conversion on a real export; check the Yahoo mapping for each holding.
- [ ] Test JSE tickers (`.JO`) and unit trust pricing on a few holdings.
- [ ] Ask Sharesight for API access (or export the history) to carry over Computershare and pension history.
- [ ] Check the Allan Gray, Sygnia, EasyEquities and Computershare portals for CSV/Excel exports.
- [ ] Build the n8n workflows (IBKR sync, prices, statement intake, monthly reconciliation).
- [ ] Parallel run against Sharesight, then decide on the switch.
- [ ] Decide what tax output the accountant needs and where it comes from.
