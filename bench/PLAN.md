# Benchmark plan (written before running anything)

The README's numbers (churn AUC 0.72, forecast "40% more accurate", "finds every planted
pair") were measured on the project's own synthetic demo data, where the same code planted
the patterns. This benchmark uses real public data only, and the app never sees the
data it is scored on. Pass marks are fixed here, before the first run.

## Data (downloaded 2026-10-01, kept out of git)

| Name | Source | What it is |
|---|---|---|
| Online Retail II | UCI ML Repository #502 (CC BY 4.0) | UK online gift shop, Dec 2009 - Dec 2011, ~1M lines, customers, cancellations |
| Coffee | Maven Analytics data playground | 3 coffee shops in New York, Jan - Jun 2023, ~149k lines |
| Pizza | Maven Analytics data playground | One pizza restaurant, 2015, ~49k lines in ~21k orders |
| CDNOW | brucehardie.com (Fader & Hardie 2001) | Music shop, 23,570 customers, Jan 1997 - Jun 1998, the classic repeat-buying dataset |

Only preparation: Pizza comes as four linked tables, so they are joined into the one table a
till would export. CDNOW is uploaded as published (no header) for the reading test, and
with a header plus `product = CD` for the ML tests (it has no product column).

## Tests and pass marks

1. **Reading and mapping** - upload each original file with no hand fixes.
   Expected mapping (the reference):
   * Online Retail II: order_id = Invoice, order_date = InvoiceDate, product = Description or
     StockCode, quantity = Quantity, unit_price = Price, customer_id = Customer ID, location = Country
   * Coffee: order_id = transaction_id, order_date = transaction_date, product = product_detail
     (product_type / product_id accepted), quantity = transaction_qty, unit_price = unit_price,
     category = product_category or product_type, location = store_location
   * Pizza: order_id = order_id, order_date = date, product = name or pizza_id, quantity = quantity,
     unit_price = price, category = category
   * CDNOW (as published): customer, date, number of CDs, dollar value - no header, no product

   Pass: required fields mapped right **without** a built-in profile (Online Retail is also run
   with its built-in profile, but that profile was written for this exact file); loaded revenue
   within 0.1% of an independent pandas total over the kept rows; every dropped row has a reason.

2. **Messy versions** - the same real rows written as a messy export: title lines, a total
   footer, `;` delimiter with decimal commas, currency signs, day-first text dates,
   Windows-1252 encoding. Pass: same rows and revenue as the clean upload.

3. **Forecast** - six 30-day windows at the end of each series; for each, the app only gets
   the data before the window. Series: Online Retail total, Coffee total and each of its 3
   shops, Pizza total, CDNOW total. Pass: the app's error (WAPE) is at most the
   "same day last week" baseline's in at least 2/3 of windows; the improvement it claims is
   within 10 points of the real one; its 80% range holds 70-90% of the real days.

4. **Churn** - cut the data at (last date - h), let the app score customers, then check
   who really bought nothing in the hidden h days. h = 30, 60, 90 on Online Retail II and
   CDNOW. Pass: real AUC at least 0.02 above "days since last purchase" on the same
   customers; the AUC the app reports within 0.05 of the real one.

5. **Bought together** - (a) pairs found in the first half of the data must hold in the
   second half (lift > 1 for at least 80% of them); (b) with products shuffled across orders
   (same basket sizes, same product counts) it must find at most 1 pair on average over 3
   shuffles. Online Retail II and Pizza.

6. **Unusual days** - 10 random days per dataset get a spike (orders doubled by copying that
   day's orders) or a drop (70% of that day's orders removed). Pass: at least 80% of them
   flagged, and no more flags on the other days than on the untouched data. A simple
   "weekday median +- 3 MAD" rule is run on the same data for comparison.

7. **Unusual sale lines** - Online Retail II has two known keying errors (80,995 and 74,215
   units, cancelled right after). Plus 20 injected price typos (x10, x100, /10). Pass: both
   known errors and at least 80% of the typos among the lines it reports.

8. **Customer segments** - segments at a cutoff vs spend in the next 90 days: the better
   groups (Champions, Loyal) must spend more later than the worse ones (At risk, Lost,
   Occasional). Compared with plain RFM-score quintiles by rank correlation with future spend.

Not in this run: the Ask AI agent (needs the language model running) and document search.
