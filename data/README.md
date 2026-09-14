# Generated data

Everything under `data/` is the research record of the forward test. It is
committed to git on purpose: the value of the experiment is that the selections
and prices cannot be revised after the fact.

```
data/
  universes/<MARKET>/YYYY-MM-DD.csv   the candidate list used on that date
  universes/<MARKET>/latest.json      provenance of the most recent refresh
  signals/<MARKET>/YYYY-MM-DD/        one immutable quarterly snapshot
      universe.csv                    every candidate considered
      raw_financials.csv              statements retrieved for screened names
      factor_scores.csv               every metric for every company
      selected.csv                    the portfolio and its target weights
      excluded.csv                    every rejection, with reasons
      metadata.json                   config hash, FX rates, coverage, warnings
  prices/<MARKET>/YYYY-MM.csv         daily bars, dividends and splits
  performance/<MARKET>.csv            daily NAV and returns
  state/<MARKET>.json                 current positions and execution history
```

Two rules are enforced in code, not by convention:

* a quarterly signal directory is written once and never rewritten
  (`SnapshotExistsError`);
* an existing `(date, ticker)` price row is never overwritten, so a later
  provider re-adjustment cannot rewrite history.
