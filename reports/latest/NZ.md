# FCF Factor - NZ

_Signal generated after market close. Forward-test execution occurs at the next available market open._

- Methodology version: `1.1.3`
- Configuration hash: `4b3c6ad7e1b16be340e583aeccabb258e994e03ef840d298a3702373c8e73aa6`
- Latest signal: **2026-10-13** (2026 Q4)
- Signal generated at: `2026-10-13T23:38:34+00:00`
- Data provider: `yahoo`

## Universe and coverage

| Stage | Count |
|---|---|
| Raw universe | 170 |
| Provider metadata resolved | 170 |
| Passed security-type / sector filters | 150 |
| Above US$50,000,000 market cap | 74 |
| Fully eligible (financial data complete) | 48 |
| FCF-yield shortlist | 9 |
| Selected | 6 |

Metadata coverage: 100.00% - financial-data coverage of market-cap-qualified names: 64.86%

> Note: stock cap relaxed from 0.0300 to 0.1667: 6 holdings cannot fill 100% at the configured cap

## Latest picks

| Ticker | Company | Sector | Mkt cap (USD) | FCF yield | Growth score | Weight |
|---|---|---|---|---|---|---|
| LIC.NZ | Livestock Improvement Corporation Limite | Consumer Defensive | 89.5m | 14.33% | -0.146 | 16.67% |
| CMO.NZ | The Colonial Motor Company Limited | Consumer Cyclical | 135.5m | 21.23% | -0.645 | 16.67% |
| FCG.NZ | Fonterra Co-operative Group Limited | Consumer Defensive | 7,095.1m | 18.60% | -0.020 | 16.67% |
| MHJ.NZ | Michael Hill International Limited | Consumer Cyclical | 91.8m | 27.54% | -0.547 | 16.67% |
| SPK.NZ | Spark New Zealand Limited | Communication Services | 2,051.5m | 17.97% | -0.641 | 16.67% |
| WIN.NZ | Winton Land Limited | Real Estate | 193.2m | 15.43% | -0.434 | 16.67% |

## Changes versus the previous rebalance

Previous signal: `none`

- **Additions (6):** `CMO.NZ`, `FCG.NZ`, `LIC.NZ`, `MHJ.NZ`, `SPK.NZ`, `WIN.NZ`
- **Removals (0):** _none_
- **Retained (0):** _none_

## Distributions (selected names)

### FCF yield

| Min | P25 | Median | P75 | Max |
|---|---|---|---|---|
| 14.33% | 16.06% | 18.28% | 20.57% | 27.54% |

| Bucket | Count |
|---|---|
| 0.143 to 0.17 | 2 |
| 0.17 to 0.196 | 2 |
| 0.196 to 0.223 | 1 |
| 0.223 to 0.249 | 0 |
| 0.249 to 0.275 | 1 |

### Growth score

| Min | P25 | Median | P75 | Max |
|---|---|---|---|---|
| -0.645 | -0.618 | -0.491 | -0.218 | -0.020 |

| Bucket | Count |
|---|---|
| -0.645 to -0.52 | 3 |
| -0.52 to -0.395 | 1 |
| -0.395 to -0.27 | 0 |
| -0.27 to -0.145 | 1 |
| -0.145 to -0.0203 | 1 |

### Sector weights

| Sector | Weight |
|---|---|
| Consumer Defensive | 33.33% |
| Consumer Cyclical | 33.33% |
| Communication Services | 16.67% |
| Real Estate | 16.67% |

## Exclusion reasons

| Reason | Count |
|---|---|
| market_cap_unavailable | 45 |
| below_min_market_cap | 31 |
| excluded_sector | 10 |
| expected_fcf_not_positive | 10 |
| excluded_industry | 8 |
| insufficient_annual_history | 5 |
| ttm_ebitda_not_positive | 5 |
| ttm_fcf_unavailable | 4 |
| ebitda_unavailable | 1 |
| quote_type | 1 |
| quote_type_missing | 1 |
| ttm_revenue_unavailable | 1 |

## Data-quality flags raised

| Flag | Count |
|---|---|
| fcf_ttm_from_provider_fcf_field | 64 |
| revenue_ttm_from_provider | 64 |
| ebitda_ttm_from_provider | 63 |
| preferred_stock_assumed_zero | 48 |
| nci_adjusted | 19 |
| reporting_currency_differs | 10 |
| annual_history_below_preferred | 9 |
| lease_inclusion_assumed | 7 |
| nci_loss_years_excluded | 7 |
| lease_liabilities_not_reported | 6 |
| stale_statements | 5 |
| market_cap_from_price_times_shares | 4 |
| nci_share_negative_floored | 4 |
| revenue_ttm_from_latest_fy | 4 |
| provider_returned_no_statements | 3 |
| statement_date_unknown | 3 |
| growth_clamped_high | 2 |
| ebitda_ttm_from_latest_fy | 1 |
| growth_clamped_low | 1 |
| nci_adjusted_from_book | 1 |
| ttm_quarter_window_unusable | 1 |

## Forward-test performance

| Metric | Value |
|---|---|
| NAV | 101.3939 |
| Inception date | 2026-10-14 |
| As at | 2026-10-23 |
| Since-inception return | 1.39% |
| Return since last rebalance | 0.87% |
| Latest daily return | -0.41% |
| Holdings | 6 |
| Turnover at last rebalance | 50.00% |
| Sessions recorded | 8 |

### Current positions (drifted weights)

| Ticker | Target weight | Drifted weight | Entry price | Last price |
|---|---|---|---|---|
| FCG.NZ | 16.67% | 18.97% | 5.2000 | 6.0000 |
| LIC.NZ | 16.67% | 16.58% | 1.1200 | 1.1300 |
| WIN.NZ | 16.67% | 16.44% | 1.1600 | 1.1600 |
| MHJ.NZ | 16.67% | 16.25% | 0.4300 | 0.4250 |
| SPK.NZ | 16.67% | 15.93% | 1.9400 | 1.8800 |
| CMO.NZ | 16.67% | 15.84% | 7.3800 | 7.1100 |

