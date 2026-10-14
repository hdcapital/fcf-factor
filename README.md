# fcf-factor

A free-data free-cash-flow factor screen and **genuine forward test** for five
markets: Australia, the United States, the United Kingdom, New Zealand and
Canada.

Each country is a completely separate universe, cross-section, ranking and
portfolio. Nothing is pooled across borders.

The system runs itself on GitHub Actions: it rebalances quarterly, emails the
new selections, captures daily prices for everything it holds, and tracks each
market's NAV from the day the first signal was actually generated. There is no
backtest here and there never will be one — see
[Why there is no backtest](#why-there-is-no-backtest).

---

## Contents

- [What the factor does](#what-the-factor-does)
- [The exact formulas](#the-exact-formulas)
- [Why there is no backtest](#why-there-is-no-backtest)
- [How execution timing avoids look-ahead bias](#how-execution-timing-avoids-look-ahead-bias)
- [Universe sources](#universe-sources)
- [Data limitations and Yahoo caveats](#data-limitations-and-yahoo-caveats)
- [Data-quality safeguards](#data-quality-safeguards)
- [Folder structure](#folder-structure)
- [Install locally](#install-locally)
- [Running it](#running-it)
- [Inspecting current picks](#inspecting-current-picks)
- [Inspecting performance](#inspecting-performance)
- [GitHub Actions](#github-actions)
- [Required GitHub Secrets](#required-github-secrets)
- [Configuring Gmail or another SMTP provider](#configuring-gmail-or-another-smtp-provider)
- [Changing factor parameters](#changing-factor-parameters)
- [Replacing the data provider](#replacing-the-data-provider)
- [Tests](#tests)

---

## What the factor does

The design is inspired by the VettaFi/Victory **VFLO** free-cash-flow
methodology, adapted for smaller companies where sell-side consensus forecasts
simply do not exist.

VFLO buys companies that are cheap on expected free cash flow *and* have good
growth prospects, in that order. This system keeps that two-stage structure and
replaces the analyst forecast with a mechanical one built from reported
financial history.

For each market, independently, every quarter:

1. **Build the universe** from official exchange listings, dropping funds,
   trusts, preference shares, warrants, units, rights, shells, debt securities,
   REITs and financials.
2. **Filter on size**: market capitalisation of at least **US$50m**, converted
   at the FX rate captured on the observation date.
3. **Retrieve financial statements** and compute free cash flow ourselves.
4. **Score every eligible company** on expected FCF yield and on a
   growth/quality score built from three cross-sectional trend z-scores.
5. **Select in two stages**: keep the cheapest 18.75% on FCF yield, then keep
   the best two thirds of those on growth. About 12.5% of the market survives.
6. **Weight** by capped FCF yield times the cube root of expected free cash
   flow, subject to a 3% per-stock and 35% per-sector cap.
7. **Freeze the whole thing** to an immutable directory, email it, and start
   tracking it from the next market open.

---

## The exact formulas

### Free cash flow

```
FCF = CFO - |CapEx|
```

The absolute value is deliberate. Yahoo reports capital expenditure as a
negative cash outflow; other sources report the same outflow as a positive
number. Taking `abs()` produces the right answer under either convention, and a
positive reported CapEx raises a `capex_reported_positive` flag for review.

```
FCF_margin = FCF / Revenue        (undefined when Revenue <= 0)
```

### Trailing twelve months, and its fallback ladder

For revenue, EBITDA and free cash flow independently, in order:

1. the **sum of the latest four quarterly statements** (accepted only when
   those four quarter-ends span 240-400 days, are each 60-120 days apart, so
   no quarter is missing, and every quarter carries the field),
2. the **provider's own TTM figure**,
3. the **most recent fiscal year**.

For free cash flow, the provider's own `freeCashflow` field is **never** used,
not even as a last resort. It is not `CFO - |CapEx|` but a levered estimate,
and when it was checked against the statements on 14 October 2026 it read
+A$111m for a trust whose operating cash flow was -A$71m, and about 3x the
reported figure for Harbour Energy and Fonterra. A company with no CFO and
CapEx is excluded (`ttm_fcf_unavailable`, flagged `fcf_provider_field_ignored`).
Half-yearly reporters (most of the UK, Australia and New Zealand) have no
usable quarterly window, so the latest fiscal year decides their FCF.

Whichever rung was used is written into every snapshot as `ttm_source`, for
example `revenue=quarterly_sum|ebitda=provider_ttm|fcf=latest_fiscal_year`.

Revenue is cross-checked before it is accepted, because one misreported quarter
flows straight through forward FCF into the yield (Yahoo once reported a
C$1.2bn Cascades quarter as C$91bn). The quarterly sum is rejected when the
total is more than 3x away from the latest fiscal year (in either direction).
Only when there is no fiscal year from the last 18 months is the weaker test
used instead: one quarter more than 4x the median of the other three. It is
weaker because seasonal businesses (H&R Block) have such a quarter honestly. The
provider's TTM is built from the same quarters, so it gets the fiscal-year
check too. A rejection is flagged `ttm_revenue_quarterly_rejected` or
`ttm_revenue_provider_rejected` and the next rung is used.

### Normalised FCF margin

```
NormalizedFCFMargin = median(FY-2 margin, FY-1 margin, FY margin, TTM margin)
```

However many of those four are available are used. The median, rather than the
mean, is what stops a single year of unusual working-capital movement from
setting a company's valuation.

### Mechanical forward revenue growth

```
RevenueCAGR    = (latest revenue / earliest revenue) ^ (1 / years) - 1
LatestYoY      = latest FY revenue / prior FY revenue - 1

g              = 0.60 * RevenueCAGR + 0.40 * LatestYoY
g              = clamp(g, -15%, +30%)

ForwardRevenue = TTMRevenue * (1 + g)
ForwardFCF     = ForwardRevenue * NormalizedFCFMargin
```

When only one of the two growth components can be computed (an earliest revenue
of zero makes the CAGR undefined, for instance), that component is used alone
and the fallback is flagged. When neither can be computed, the company is
excluded with `forward_growth_unavailable`.

### Expected free cash flow

```
ExpectedFCF_group = 0.50 * CurrentTTMFCF + 0.50 * ForwardFCF
ExpectedFCF       = ExpectedFCF_group * (1 - NCIShare)
```

A company must have `ExpectedFCF > 0` and positive operating profitability
(`TTM EBITDA > 0`, or latest-FY EBITDA > 0 with the fallback flagged).

### Minority interests

When a company consolidates a subsidiary it only partly owns, 100% of that
subsidiary's cash flow lands in group operating cash flow, even though minority
holders own a slice of it. So expected free cash flow is scaled down to the
listed company's own share, and minority interests are **not** added to
enterprise value, which would count them twice.

```
NCIShare = sum(NCI earnings) / sum(group net income)     profitable years among the latest three
```

Loss years are left out, because adding a loss to two profitable years shrinks
the denominator towards zero and inflates the ratio. If every year in the window
was a loss, the fallback is
`NCI book value / total equity including NCI`. A company that shows minority
interests on its balance sheet but gives no way to measure their share is
excluded as `nci_present_share_unknown`, never assumed to be zero. Both
`expected_fcf_group` and the attributable `expected_fcf` are stored.

### Enterprise value

```
EV = MarketCap + Debt + PreferredStock - Cash
Debt = borrowings + lease liabilities
```

All components are converted to USD **before** they are combined.

**Leases are debt.** Under IFRS 16 and ASC 842 a lease is a liability, and the
principal repaid each year sits in *financing* cash flow. Free cash flow
(operating cash flow less capex) is therefore already before lease repayments,
so the matching denominator must carry the lease liability as debt. That is
what lets a retailer that rents its stores compare fairly with one that owns
them: one carries more debt, the other more capex.

Yahoo publishes no lease line on the cash flow statement, only lease
liabilities on the balance sheet, and its "total debt" figure sometimes
includes them and sometimes does not. So the system checks every company
rather than assuming: if total debt matches borrowings plus leases they are
already included; if it matches borrowings alone, or is smaller than the lease
liability, the leases are added. The outcome is stored per company as
`lease_treatment`.

Preferred stock genuinely absent from a balance sheet is treated as zero. A
*missing* debt or cash field is not: assuming zero net debt would quietly
flatter every company whose balance sheet failed to download, so the
calculation is abandoned and the provider's own enterprise value is used and
flagged instead, with minority interests removed from it. Both figures and the
source used are stored. `EV <= 0` excludes the company.

### FCF yield

```
FCFYield       = ExpectedFCF_USD / EV_USD          (used for ranking, uncapped)
WeightFCFYield = min(FCFYield, 15%)                (used for weighting only)
```

Extreme yields are flagged for review but are never re-ranked, unless a genuine
unit or data error is detected — that is a data-quality decision, not a factor
decision.

### Growth / quality score

Three scale-free trends over the annual history, oldest to newest:

```
RevenueTrend      = OLS slope of annual Revenue      / mean(|annual Revenue|)
EBITDATrend       = OLS slope of annual EBITDA       / mean(|annual EBITDA|)
FCFPerShareTrend  = OLS slope of annual FCF/share    / mean(|annual FCF/share|)
```

FCF per share uses diluted weighted-average shares, falling back to basic
shares. If neither is available for enough years, the component is **omitted**
rather than invented.

A company must have `RevenueTrend` plus at least one of the other two.

Each trend is standardised **within its own market** and clipped:

```
Z = clip(z-score(Trend), -3, +3)
GrowthScore = mean of the available Z scores
```

Z-scores are computed across the entire eligible market universe **before** the
FCF-yield screen, so a company's growth score reflects its position in the whole
market rather than in the value shortlist it happened to land in.

### Two-stage selection

```
N       = fully eligible companies in the market
n_value = ceil(N * 0.1875)          rank by FCF yield descending
n_final = ceil(n_value * 2/3)       rank the shortlist by GrowthScore descending
        ~= N * 0.125
```

The two screens are never blended into one weighted score. The architecture is
"cheap on free cash flow first, then fundamental growth and quality".

Ties break deterministically: stage one sorts by `(-FCFYield, -GrowthScore,
ticker)` and stage two by `(-GrowthScore, -FCFYield, ticker)`, always ending on
the ticker alphabetically.

### Weighting

```
RawWeight = min(FCFYield, 15%) * cbrt(ExpectedFCF_USD)
```

normalised to 100%, then pushed through a per-stock cap (3%) and a per-sector
cap (35%) by **water-filling** rather than clip-and-renormalise — clipping and
renormalising re-inflates the names just capped, which is the classic way to end
up with a "3% cap" portfolio holding 3.4%.

If a cap is arithmetically impossible — nine holdings cannot fill 100% at 3%
each — it is relaxed to at least `1 / number_of_holdings`, logged, and recorded
in `metadata.json`.

---

## Why there is no backtest

Running today's financial statements through the factor to produce "historical"
selections would be worthless: those statements were not available on the dates
they would be attributed to, they have since been restated, and the surviving
universe excludes every company that delisted. Such a backtest cannot lose.

So this system creates a **prospective** experiment instead. It records what it
selected, when, from what data, under which configuration — and then measures
what happens next. The first data point is the first quarterly run after the
software is deployed. There will be no returns before that date, and that is the
point.

The rules that protect this are enforced in code:

| Rule | Where it is enforced |
|---|---|
| A quarterly snapshot is written once and never rewritten | `storage.write_signal_snapshot` raises `SnapshotExistsError` |
| A recorded price row is never overwritten by a later correction | `storage.upsert_prices` skips existing `(date, ticker)` keys |
| The signal day's close is never an entry price | `portfolio.nav` only executes on sessions strictly after the signal date |
| Invented data never enters the record | `pipeline.persist_screen` refuses any snapshot from the synthetic provider |
| The methodology that produced a snapshot is provable | `metadata.json` stores `methodology_version` and a SHA-256 `config_hash` |

Do not tune thresholds because a different setting produces better returns. If
the methodology must change, bump `METHODOLOGY_VERSION` in `fcf_factor/config.py`
so the change is visible in every subsequent snapshot and the two regimes can be
told apart later.

---

## How execution timing avoids look-ahead bias

The signal is generated **after** a market close. A portfolio that could act on
that close would be trading on information it did not have.

So the portfolio may only transact at the **next available market open**:

```
Friday 4 Sep    signal generated after the close      signal_date
Monday 7 Sep    old book sold at the open             execution_date
                new book bought at the same open      execution_open_price
Monday 7 Sep    NAV first marked at the close
```

Every execution record stores `signal_date`, `signal_timestamp`,
`execution_date` and the `execution_open_price` of each position. Between
rebalances the portfolio drifts naturally; it is never rebalanced back to target
weights daily.

**Stocks that do not trade on the rebalance day.** Thinly traded small caps often
have no trade on a given day, and nothing can be bought or sold at a price that
never printed. So:

- a new purchase that cannot trade has its cash set aside, and is bought at the
  first later open where the stock trades;
- an exit that cannot trade stays in the book at its last price, and is sold at
  the first later open where it trades;
- a retained holding that cannot trade keeps its shares.

Each completed deferred trade is logged in `fills` in the state file and as a
`filled:` or `sold:` event in the performance file. A purchase still waiting at
the next rebalance is cancelled and its cash returns to the pool. While an exit
is waiting, its replacement has already been bought, so cash can briefly be
negative until the sale completes. Transaction costs are charged at the
rebalance on the full intended turnover.

Total return uses the dividends and splits that were observable at the time,
from raw (non dividend-adjusted) price bars, so a later re-adjustment of Yahoo's
series cannot rewrite past returns.

---

## Universe sources

| Market | Venues | Source |
|---|---|---|
| AU | ASX | ASX company directory CSV |
| US | NYSE, Nasdaq, NYSE American | Nasdaq Trader symbol directory (`nasdaqlisted.txt`, `otherlisted.txt`) |
| UK | LSE Main Market, AIM | LSE monthly instrument list (newest upload found automatically) |
| NZ | NZX Main Board | NZX Main Board securities listing |
| CA | TSX, TSX Venture | TMX company-directory JSON |

Each market has its own adapter in `fcf_factor/universe/`.

**UK source freshness.** LSE publishes its instrument list monthly as a new
numbered file (`Instrument list_84.xlsx`, then `_85`, and so on); the
un-numbered address stopped updating in September 2020. The adapter searches
for the newest upload on every run, reads the "As at" date printed in the
file, and refuses anything older than 70 days rather than silently screening a
stale list. It keeps only lines LSE itself labels as shares (MiFIR `SHRS`) on
the Main Market or AIM, dropping ETFs, depositary receipts, the International
Order Book, the Specialist Fund Segment and investment funds.

To check every market's source without screening or committing anything, run
the **Universe check** workflow from the Actions tab. Parsing is separated
from fetching, so every parser is tested against a captured fixture file — if an
exchange changes its format, the test suite fails rather than production
silently producing an empty universe.

Symbol mapping keeps `exchange_symbol` and `yahoo_symbol` as separate fields:

| Exchange symbol | Yahoo symbol |
|---|---|
| `BHP` (ASX) | `BHP.AX` |
| `AIR` (NZX) | `AIR.NZ` |
| `BT.A` (LSE) | `BT-A.L` |
| `CCL.B` (TSX) | `CCL-B.TO` |
| `ABC` (TSXV) | `ABC.V` |
| `BRK.A` (NYSE) | `BRK-A` |

A suffix is never assumed to be correct. If the provider returns nothing for a
mapped symbol, the company is excluded with `symbol_unresolved` and appears in
`excluded.csv` — so a broken suffix shows up as a visible hole, not a silent one.

**Provider throttling.** Yahoo throttles heavy use ("Too Many Requests"). A
throttle pauses every worker (30s, doubling up to 5 minutes) without using up
the ordinary retries, and a failed fetch is never cached. After the first pass,
every security that came back without metadata or statements is fetched again,
one at a time, after a two-minute cool-down (two passes). If more than 3% of the
universe is still unresolved, the screen **fails** rather than publishing: on
14 October 2026 a throttle silently removed most of the US list after "K" and
the Canadian list after "S", which is a screen of part of the alphabet, not of
the market. `refetched`, `refetch_recovered` and `provider_throttle_events` are
recorded in the coverage summary.

**If a universe refresh fails**, the most recently stored universe is reused,
`is_stale` is set, and the reason travels into `metadata.json`, the market report
and the email. A stale universe is never presented as a fresh one. If the raw
universe is below the configured floor for that market, or has shrunk by more
than 35% since the previous stored universe, the run raises
`UniverseIntegrityError` rather than quietly screening a truncated list.

---

## Data limitations and Yahoo caveats

Free data is not clean data. These are the known limitations, all handled
explicitly rather than papered over:

- **About four annual periods.** Yahoo typically exposes four years of annual
  statements, sometimes three. The factor is designed to work with a minimum of
  three (`MIN_ANNUAL_PERIODS`); companies with fewer are excluded, and those
  below the preferred four are flagged.
- **London prices are in pence.** Yahoo quotes `.L` shares in `GBp` while
  reporting the same company's market cap in pounds. Mixing the two is a silent
  100x valuation error. Quoted prices are normalised to major units, and the
  reported market cap is reconciled against `price x shares` — see below.
- **EBITDA is often missing** for smaller companies. It is reconstructed as
  `EBIT + D&A` and flagged (`ebitda_is_derived`) rather than dropping the
  company.
- **Statement row labels change between yfinance releases.** Each field has a
  list of aliases in `fcf_factor/providers/yahoo.py`.
- **No analyst estimates are used anywhere.** By design.
- **Reporting currency can differ from listing currency.** A Canadian company
  reporting in USD is common. Statement values convert from the financial
  currency; market cap converts from the listing currency; the mismatch is
  flagged.
- **Rate limits.** Requests are serialised through a rate limiter with
  exponential backoff, bounded concurrency (4 workers by default) and a local
  on-disk cache. Please do not raise `PROVIDER_MAX_WORKERS` aggressively.
- **Missing data is never fabricated.** Every excluded company carries one or
  more machine-readable reasons in `excluded.csv`.

---

## Data-quality safeguards

Checks that run on every screening pass:

| Check | Behaviour |
|---|---|
| `price x shares` versus reported market cap | Ratio within 0.5-2.0 accepted; ~100x or ~0.01x resolved as a units error and flagged; beyond 5x excluded as `market_cap_inconsistent` |
| GBp / GBX / pence quotes | Normalised to GBP, flagged `price_quoted_in_minor_units` |
| Reporting currency mismatch | Flagged `reporting_currency_differs` |
| Implausible market cap | Excluded above US$10tn |
| Implausible share count | Share counts below 1,000 ignored, flagged |
| Negative or zero EV | Excluded |
| Missing debt or cash field | EV calculation abandoned, provider EV used, flagged |
| Lease liabilities missing from debt | Added to debt and flagged `lease_liabilities_added_to_debt` |
| Minority interests | Removed from cash flow; unmeasurable ones exclude the company |
| Bar fetched while the session was trading | Not stored; any already stored is replaced by the real close |
| Placeholder bar with no price | Not stored; any already stored is replaced by the real session |
| Positive reported CapEx | Flagged `capex_reported_positive` |
| Stale statements | Flagged beyond 640 days |
| Empty provider payload | Flagged `provider_returned_no_statements` |
| Extreme FCF yield | Flagged above 50%, ranking unchanged |
| Duplicate tickers | De-duplicated at universe build, each collision recorded |
| Universe shrinkage | Run fails beyond a 35% drop, or below the per-market floor |
| Exchange suffix errors | Surface as `symbol_unresolved` exclusions |
| Provider throttling | Shared pause, re-fetch passes; run fails above 3% unresolved |
| Missing or misreported quarter | TTM window rejected; revenue cross-checked against the fiscal year |
| Provider EV on a disputed share basis | Not used (`provider_ev_basis_unverifiable`) |

A coverage summary is produced every run and appears in `metadata.json`, the
market report and the email.

---

## Folder structure

```
fcf_factor/
  config.py              every tunable assumption, plus the config hash
  schedule.py            first-Friday logic and session timing
  currency.py            GBp/GBP normalisation and USD conversion
  fx.py                  FX rate capture for a run
  storage.py             CSV/JSON persistence and the immutability guarantee
  pipeline.py            screening orchestration
  performance.py         reading the forward-test record back
  reporting.py           markdown audit reports
  cli.py                 the command line
  providers/             DataProvider interface, Yahoo, synthetic, cache
  universe/              per-market adapters, symbol mapping, security filters
  factor/                the factor itself: pure functions, no I/O
  portfolio/             daily price capture, execution and NAV
  notify/                the quarterly email

data/                    the research record (committed - see data/README.md)
reports/latest/          one markdown report per market, plus an index
tests/                   313 tests, none of which touch the network
.github/workflows/       quarterly_factor.yml, daily_prices.yml, tests.yml
```

---

## Install locally

Requires Python 3.11 or newer (the workflows use 3.12).

```bash
git clone https://github.com/hdcapital/fcf-factor.git
cd fcf-factor

python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate

pip install -r requirements-dev.txt
```

Verify the install without touching the network:

```bash
pytest -q
python -m fcf_factor screen --market NZ --limit 20 --dry-run --provider synthetic
```

---

## Running it

### Offline smoke test

Uses the built-in synthetic provider. No network, no writes, invented data that
can never be persisted:

```bash
python -m fcf_factor screen --market NZ --limit 20 --dry-run --provider synthetic
```

### Refresh a universe

```bash
python -m fcf_factor universe --market NZ
python -m fcf_factor universe --markets all
```

### Screen one market against live data

```bash
python -m fcf_factor screen --market NZ --dry-run
```

Drop `--dry-run` to write the snapshot and the report. Add `--limit 50` to
sample a market quickly while developing.

### Screen all five markets

```bash
python -m fcf_factor screen --markets all --dry-run
```

### Run the full quarterly pipeline

This screens, freezes the snapshot, captures prices, updates the portfolio,
rewrites the report and sends the email:

```bash
# Only acts if yesterday was the first Friday of a rebalance month
python -m fcf_factor quarterly-run --markets all

# Ignore the schedule and run now, against the latest completed session
python -m fcf_factor quarterly-run --markets all --manual

# Everything except the writing and the sending
python -m fcf_factor quarterly-run --markets all --manual --dry-run
```

### Trigger a quarterly run on GitHub

Actions → **Quarterly FCF factor** → **Run workflow**. Set `markets` and leave
`force` at `true` to screen immediately regardless of the calendar.

### Daily forward tracking

```bash
python -m fcf_factor daily-prices                   # fetch and store bars
python -m fcf_factor update-portfolio               # execute and roll NAV
python -m fcf_factor report                         # rewrite reports/latest/
```

`daily-prices` re-requests the last 10 calendar days every run and backfills any
gaps, so one failed workflow run does not leave a hole. A bar is stored only once
its session has closed in that market's own timezone. That matters because
GitHub routinely starts scheduled jobs hours late, which lands the 23:35 UTC job
in the middle of the next Australian and New Zealand trading day. Duplicate `(date,
ticker)` rows are impossible by construction. A market holiday simply produces no
rows for that day.

### Check the schedule

```bash
python -m fcf_factor check-rebalance                     # is today a rebalance day?
python -m fcf_factor check-rebalance --date 2026-09-05
```

### Email the latest picks manually

```bash
python -m fcf_factor send-email --market NZ --dry-run    # build, do not send
python -m fcf_factor send-email --markets all            # send
```

---

## Inspecting current picks

```bash
# Human-readable report per market
cat reports/latest/NZ.md
cat reports/latest/index.md

# The frozen snapshot itself
ls data/signals/NZ/
column -s, -t < data/signals/NZ/2026-09-04/selected.csv | less -S

# Why a specific company was rejected
grep BHP.AX data/signals/AU/2026-09-04/excluded.csv

# Every metric for every eligible company
column -s, -t < data/signals/AU/2026-09-04/factor_scores.csv | less -S

# What the run knew about itself
python -m json.tool data/signals/AU/2026-09-04/metadata.json
```

---

## Inspecting performance

```bash
python -m fcf_factor status                    # NAV and returns for all five
cat data/performance/NZ.csv                    # daily NAV series
python -m json.tool data/state/NZ.json         # positions and execution history
```

`data/state/<MARKET>.json` holds current positions with their target weights and
their actual drifted weights, plus the complete execution history including each
position's execution open price.

---

## GitHub Actions

Three workflows, all with `workflow_dispatch`:

| Workflow | Schedule | What it does |
|---|---|---|
| `quarterly_factor.yml` | 06:00 UTC Saturdays and Sundays in the rebalance months | Checks whether the preceding Friday was the first Friday; if so, screens all five markets in parallel, emails the picks, and commits once. Sunday is a backup for a late or skipped Saturday trigger; markets already screened are skipped |
| `daily_prices.yml` | `35 23 * * 1-5` | Captures daily bars, executes pending signals, rolls NAV, commits |
| `tests.yml` | on push and pull request | Lint, tests, offline smoke test |

The quarterly workflow runs each market as a separate matrix job that uploads
its own artifact, then a single `persist` job downloads all five and makes
**one** commit. Parallel jobs never push concurrently, and both data workflows
share a `concurrency` group so the quarterly and daily jobs cannot collide.

Generated-data commits are messaged `Update FCF factor data YYYY-MM-DD [skip ci]`
and touch only `data/` and `reports/`. `tests.yml` ignores those paths and is not
triggered by schedule, so there is no way for the workflows to trigger each
other.

---

## Required GitHub Secrets

Settings → Secrets and variables → Actions → New repository secret:

| Secret | Example | Notes |
|---|---|---|
| `SMTP_HOST` | `smtp.gmail.com` | |
| `SMTP_PORT` | `587` | 587 uses STARTTLS; 465 uses implicit TLS |
| `SMTP_USER` | `you@gmail.com` | |
| `SMTP_PASSWORD` | a 16-character app password | never your account password |
| `EMAIL_FROM` | `you@gmail.com` | |
| `EMAIL_TO` | `you@gmail.com` | comma-separated for several recipients |

If any of these is missing the quarterly run **fails loudly** with an
`EmailConfigurationError` naming the missing variables. It never reports success
while sending nothing.

Credentials are read only from the environment. Nothing is hard-coded, nothing
is logged, and `.env` is gitignored.

---

## Configuring Gmail or another SMTP provider

Gmail rejects plain account passwords. You need an **App Password**:

1. Enable 2-Step Verification on the Google account.
2. Go to <https://myaccount.google.com/apppasswords>.
3. Create an app password (name it "fcf-factor").
4. Use the 16-character value as `SMTP_PASSWORD`, with
   `SMTP_HOST=smtp.gmail.com` and `SMTP_PORT=587`.

Other providers work the same way. Examples:

| Provider | Host | Port |
|---|---|---|
| Gmail | `smtp.gmail.com` | 587 |
| Outlook / Microsoft 365 | `smtp.office365.com` | 587 |
| Fastmail | `smtp.fastmail.com` | 465 |
| SendGrid | `smtp.sendgrid.net` | 587 (user `apikey`) |

Test locally before trusting the workflow:

```bash
cp .env.example .env      # fill it in; .env is gitignored
set -a && source .env && set +a
python -m fcf_factor send-email --market NZ --dry-run   # builds only
python -m fcf_factor send-email --market NZ             # actually sends
```

---

## Changing factor parameters

Everything lives in **`fcf_factor/config.py`**. There are no magic numbers
scattered through the codebase.

```python
MIN_MARKET_CAP_USD      = 50_000_000   # size floor, in USD
VALUE_SCREEN_PERCENTILE = 0.1875       # fraction kept by the FCF-yield screen
GROWTH_KEEP_RATIO       = 2/3          # fraction of the shortlist kept on growth
FCF_YIELD_WEIGHT_CAP    = 0.15         # yield cap, for weighting only
MAX_STOCK_WEIGHT        = 0.03
MAX_SECTOR_WEIGHT       = 0.35
MIN_ANNUAL_PERIODS      = 3
FORWARD_GROWTH_MIN      = -0.15
FORWARD_GROWTH_MAX      = 0.30
TRANSACTION_COST_BPS    = 0
```

Any of them can also be overridden by an environment variable of the same name,
which is convenient for experiments:

```bash
MIN_MARKET_CAP_USD=250000000 python -m fcf_factor screen --market NZ --dry-run
```

Either way the **effective** configuration is hashed into every snapshot:

```bash
python -m fcf_factor status | head -20        # shows the current config hash
```

If you change the methodology itself — not just a threshold — bump
`METHODOLOGY_VERSION` so future readers can separate the two regimes.

---

## Replacing the data provider

The factor engine never imports `yfinance`; a test asserts this. Everything
provider-specific lives behind one interface:

```python
class DataProvider:
    def get_company_metadata(self, symbol) -> CompanyMetadata | None: ...
    def get_annual_financials(self, symbol) -> list[IncomeCashflowPeriod]: ...
    def get_quarterly_financials(self, symbol) -> list[IncomeCashflowPeriod]: ...
    def get_balance_sheet(self, symbol, period="annual") -> list[BalanceSheetSnapshot]: ...
    def get_prices(self, symbol, start, end) -> list[PriceBar]: ...
    def get_fx_rate(self, base, quote="USD") -> float | None: ...
```

To move to Capital IQ, EODHD, SEC filings, Companies House or anything else:

1. Add `fcf_factor/providers/<name>.py` with a subclass of `DataProvider`.
2. Register it in `fcf_factor/providers/__init__.py:get_provider`.
3. Run it with `--provider <name>`.

No factor code changes. Snapshots record which provider produced them, so a
provider switch is visible in the audit trail.

---

## Tests

313 tests, none of which touch the network — a fixture makes any accidental HTTP
call fail immediately.

```bash
pytest -q                       # everything
pytest tests/test_portfolio.py  # execution timing, dividends, splits, NAV
ruff check .                    # lint
```

Coverage includes: FCF under both CapEx sign conventions, the normalised margin,
revenue CAGR, growth clamping, expected FCF, enterprise value and its fallback,
currency conversion, GBp/GBP normalisation, FCF yield, OLS trends, z-score
clipping, the two-stage ranking, deterministic tie-breaking, weighting, the
stock and sector caps, rebalance execution timing, dividend handling, stock
splits, duplicate daily price prevention, missing-data exclusions, snapshot
immutability, universe parsing for all five markets, and the email.

---

## Methodology changelog

Every snapshot records the version that produced it in `metadata.json`.

| Version | Date | Change |
|---|---|---|
| 1.0.0 | 2026-09-14 | Initial release |
| 1.1.2 | 2026-10-13 | The minorities' earnings share ignores loss years. |
| 1.1.3 | 2026-10-14 | Direct-method operating cash flow (most ASX and NZX companies) is read; before this nine in ten had no annual FCF history. The provider's levered `freeCashflow` field is no longer used at all (it read +A$111m for a trust with -A$71m operating cash flow). TTM windows must be four consecutive quarters; a TTM revenue more than 3x away from the latest fiscal year (or, with no recent fiscal year, with one quarter more than 4x the others) is rejected and the next rung of the ladder used. A provider EV is not used when the market cap and price x shares disagree. Securities that came back empty (usually provider throttling) are re-fetched after a cool-down, and a screen still missing more than 3% of its universe fails instead of publishing. |
| 1.1.1 | 2026-10-13 | Purchases and sales that cannot trade on the rebalance day wait for the first real trade instead of being redistributed or sold at a stale price. |
| 1.1.0 | 2026-10-13 | Minority interests deducted from free cash flow instead of added to enterprise value. Lease liabilities verified to be inside debt, and added when the provider omits them. Price bars stored only after their session closes. Fixed total equity being misread as minority interest. |

The Australian portfolio was restarted under 1.1.0. The 1.0.0 Australian signal
of 15 September 2026 and its four weeks of history remain in git history for
audit; they were built on the total-equity bug and on mid-session prices.

## Disclaimer

This is systematic investment research tooling, not investment advice. The
factor is an unproven hypothesis; the entire purpose of the forward test is to
find out whether it works.
