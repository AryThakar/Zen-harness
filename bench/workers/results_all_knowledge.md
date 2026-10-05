# Worker benchmark - all

Local model: Gemma 4 E4B (Q4_K_XL + MTP draft) on llama-server, worker slot, thinking on, RTX 3050 Laptop 4 GB.

| role | passed | median seconds | model calls |
|---|---|---|---|
| knowledge | 7/7 | 4.7 | 2.1 avg |

**Total: 7/7**

| task | result | seconds | tools | detail |
|---|---|---|---|---|
| k_returns | pass | 5.9 | search_files | expects ('14',); sources ['store_policy.md - "Returns"'] |
| k_note | pass | 8.9 | recall, search_files | expects ('net_sales',); sources ["note 'Revenue column' (note_1)"] |
| k_chat | pass | 4.1 | recall | expects ('6 months',); sources ['conversation old1'] |
| k_missing | pass | 4.2 | search_files | should say it isn't covered; sources [] |
| h_leave | pass | 3.9 | search_files | expects ('24',); sources ['employee_handbook.docx - "Expenses"'] |
| h_bulk | pass | 4.7 | recall | expects ('8%',); sources ["note 'Revenue column' (note_1)", 'conversation old2'] |
| h_summary | pass | 10.6 | read_file | mentions at least 3 of: 9:00, laptop, 24 days, 500 |

## Answers

**k_returns** (pass): Customers may return opened phones within 14 days if the box and all accessories are complete (store_policy.md - "Returns").

**k_note** (pass): We decided to use net_sales as the revenue column, excluding refunded orders.

**k_chat** (pass): In an earlier conversation on 2026-09-20 10:00, it was agreed to forecast 6 months ahead, by month.

**k_missing** (pass): The CEO's salary is not mentioned in the store policy, the employee handbook, or the pinned notes.

**h_leave** (pass): New employees get 24 days of annual leave, plus public holidays.

**h_bulk** (pass): Bulk orders of 50 units or more get an 8% discount.

**h_summary** (pass): Here is a summary of the employee handbook:  *   **Office Hours:** 9:00 to 18:00 on weekdays. *   **Warehouse Opening:** 7:00. *   **Equipment:** New employees receive a laptop on their first day, which must be returned when leaving. *   **Annual Leave:** Employees receive 24 days of annual leave, plus public holidays. *   **Expenses:** Purchases exceeding 500 require manager approval.
