# Benchmark results - real public data (run 2026-10-01)

Plan and pass marks were fixed before running: [PLAN.md](PLAN.md). Raw numbers:
`bench/work/summary.txt` and `bench/work/t*.json` (not in git). Rerun with the `t*.py` scripts,
then `python summarize.py`.

Four real businesses: Online Retail II (UK online shop, 1.07M lines, 2 years), Coffee (3 New
York coffee shops, 149k lines), Pizza (one restaurant, 49k lines), CDNOW (music shop, 23,570
customers, 70k purchases). In every ML test the app only saw data from before the period it
was scored on.

## Scorecard

| Test | Result | Verdict |
|---|---|---|
| 1. Reading real files | Every kept row matched an independent pandas reading: 0 wrong amounts, 0 wrong dates, on 1.26M rows | **Pass** |
| 1. Overlapping uploads | Online Retail II's two sheets overlap by 9 days; the app skipped exactly those 22,416 rows | **Pass** |
| 1. Auto column matching | Online Retail II 7/7 (also without its built-in profile). Coffee 5/7 (missed `transaction_qty`, `store_location`), Pizza 5/6 (missed `name` as product), CDNOW 0/5 (file has no header row). 3 of 4 files need the user to fix the mapping screen first | **Fail** (pass mark: required fields right without help) |
| 1. Crash | Uploading Online Retail II crashed once while checking the extra `StockCode` column; 6 more runs of the same step did not | **Bug** (see below) |
| 2. Messy versions | Title lines, `Total` footer, `;` + decimal commas, Windows-1252, currency signs, US month-first and UK day-first text dates: 9 of 9 messy files gave the same rows, revenue and dates as the clean file | **Pass** |
| 3. Forecast vs "same weekday last week" | Better or equal in 28 of 30 windows | **Pass** |
| 3. Forecast vs "average of last 4 same weekdays" (extra, stronger baseline) | Better in only 18 of 30 windows | weak |
| 3. Forecast's claimed accuracy | Right on average (Pizza claims 36% better, really 37%; Online Retail claims 6%, really 5%), but single claims miss by > 20 points in 9 of 30 windows (worst: claimed 28% better, really 119% worse) | **Fail** (pass mark: within 10 points) |
| 3. Forecast's 80% range | Held 76% of real days overall, but 42% for Coffee (too narrow) and 94% for CDNOW (too wide) | Pass overall, uneven |
| 4. Churn | Beat "days since last purchase" in 12 of 12 tests: real AUC 0.67-0.78 vs 0.51-0.62; its top 20% held 27-47% of the customers who left (vs 21-31%). The AUC it reports was within 0.05 of the real one in 12 of 12 | **Pass** |
| 5. Bought together, false pairs | Shuffled Online Retail: 0 false pairs (3 runs). Shuffled Pizza: 1, 9 and 0 false pairs - and on the real Pizza data it found only 1 pair | **Pass** on the big shop, **Fail** on the small menu |
| 5. Bought together, holds later | Online Retail: 82% of pairs (whose products were still sold) still went together in the second half; 60% if pairs with discontinued products count as failures | Pass / Fail depending on that choice (the "still sold" rule was added after seeing the numbers) |
| 6. Unusual days | Found 94% of injected spikes and drops (a simple weekday rule: 57-97%), while flagging fewer other days than that rule. In 3 of 12 runs 1-2 extra other days got flagged | **Pass** on finding, small miss on the strict extra-flag mark |
| 7. Unusual sale lines | Found 7 of 80 injected price typos (x10, x100, /10) and neither known keying error (80,995 and 74,215 units) | **Fail** |
| 8. Segments | Better-named groups did spend more in the next 90 days, but plain RFM-score quintiles ranked future spend better on both datasets (0.55 vs 0.49, 0.40 vs 0.38) | Pass, but no better than the textbook rule |

## What this means

**Works on real data:** reading and cleaning real and messy exports (row-exact), skipping
overlapping uploads, churn prediction (and its reported AUC is honest), finding unusual
days, and forecasting better than a naive repeat of last week. The README's churn number
holds up: it claimed AUC 0.72 vs 0.61 at 90 days on demo data; real data gave 0.67-0.75 vs
0.57-0.60.

**Weak:** the forecast's per-run "X% more accurate" claim and its 80% range on some series;
column matching for names outside the alias list; bought-together on a small menu (can't tell
real pairs from chance); segments add nothing over the simple RFM rule.

**Broken:**
* **Unusual sale lines.** On Pizza with one price per product, 0 of 20 typos found - a pizza
  sold at 100x its price was not reported. Cause: the Isolation Forest trains on samples of 256
  lines, where the price ratio is almost always exactly 1, so it almost never splits on price
  (my reading of the code); and scores for extreme lines tie, so which 25 lines get shown is
  arbitrary. On Online Retail the 25 slots go to wholesale price breaks (1-7% of the usual
  price at 180-1,500x the usual quantity). Measured for comparison: a plain rule "price over 3x
  or under 1/3 of the product's median" caught 20 of 20 typos on every dataset, with no other
  lines on Coffee and Pizza - but 17,183 other lines on Online Retail, where real prices vary.
* **Intermittent upload crash.** `parse_dates` sends day numbers with blanks (NaN) to
  `pd.to_datetime(unit="D")`; pandas 2.3.3 leaves those slots of a working array uninitialised
  (`np.empty` in `cast_from_unit_vectorized`, checked in the pandas source), and rounding that
  leftover memory can overflow, which pandas turns into an error. It depends on what was in
  memory, so it failed 1 of 7 runs. Any 5-digit code column (stock codes, zip codes)
  takes this path.

**Also seen:** Online Retail II has ~5,800 lines that are not product sales (postage, bank
charges, Amazon fees, manual adjustments); the app counts them as sales because nothing
marks them. Customer segments: KMeans made 6 clusters on Online Retail but named them as 4
groups (two clusters can get the same name).

## Not tested here

The Ask AI agent (needs the language model running), document search, the dashboard numbers,
and stronger churn baselines such as BG/NBD (the textbook model for CDNOW).
