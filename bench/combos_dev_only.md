# Acceptance jobs - dev

1/2 passed · 2026-10-09 23:46 · main slot 0, helpers slot 1, http://127.0.0.1:8080

| job | what it chains | result | seconds |
|---|---|---|---|
| invoice_rupees | live web rate + a table's total -> exact conversion | FAIL: reply lacks total_usd=11204.31 | 153.9 |
| cancel_download | a permission refused -> nothing done, said plainly | pass | 7.7 |

**invoice_rupees** - tools: main:research_web(ok), web:web_search(ok), web:read_webpage(ok), main:analyze_data(ok), data:inspect_data(ok), data:query_data(ok), main:calculate(ok)

> The USD to INR exchange rate for today is 96.66. The total of `invoices.csv` is 1,083,008.60 INR.

**cancel_download** - tools: main:files(None)

> I couldn't download it because the request was cancelled.
