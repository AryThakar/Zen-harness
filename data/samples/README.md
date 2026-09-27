# Sample files

Small hand-made exports, one per kind of business, used by the tests and for demos.
All names, emails and numbers are made up.

| File | Looks like | What it shows |
|---|---|---|
| `retail_pos_messy.csv` | A shop's point-of-sale register | Title lines above the header, `₹1,299.00` prices, `10%` discounts, day-first dates, a return (negative quantity), mixed-case names, a custom column (Payment Mode), 4 bad rows and a `Total` footer that get rejected with reasons |
| `shopify_orders.csv` | Shopify orders export | Recognised by the built-in profile: the order number is in `Name`, the date and customer are only on the first line of each order (filled in automatically), per-line discounts are used, order totals and personal columns are ignored |
| `superstore_sample.csv` | Kaggle "Superstore" dataset | Only line totals (`Sales`, `Profit`) and a fractional discount - unit price and unit cost are worked out; month-first dates |
| `agency_invoices.csv` | A service agency's invoice list | No quantity or unit price, only an amount per invoice - quantity becomes 1 |

Bigger realistic data comes from the generator:

```bash
python -m app.datagen                            # 18 months, canonical format
python -m app.datagen --format messy --months 6  # POS-style export to test cleaning
```
