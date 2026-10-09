# Acceptance jobs - dev

8/10 passed · 2026-10-10 00:01 · main slot 0, helpers slot 1, http://127.0.0.1:8080

| job | what it chains | result | seconds |
|---|---|---|---|
| expense_memo | messy table -> clean -> compare months -> chart -> Word memo -> note | pass | 221.0 |
| dice_fairness | coder makes data -> it becomes a table -> the analyst tests it | pass | 94.1 |
| meeting_actions | document -> extract -> checklist file -> gantt diagram | pass | 16.6 |
| survey_themes | free text -> the user's labels -> shares -> chart | pass | 64.1 |
| invoice_rupees | live web rate + a table's total -> exact conversion | FAIL: reply lacks total_usd=11204.31 | 93.9 |
| churn_risk | model -> drivers -> score the rest -> save the top 5 | pass | 98.9 |
| verified_picture | coder draws -> the picture is looked at -> confirmed | pass | 63.2 |
| ambiguous_top | an ambiguous question -> ask, or say the reading taken | pass | 37.4 |
| cancel_download | a permission refused -> nothing done, said plainly | pass | 6.0 |
| plan_forecast_deck | plan card -> forecast -> slides | FAIL: slides: 1 (want 2+), pictures: 1 | 108.1 |

**expense_memo** - tools: main:analyze_data(ok), data:inspect_data(ok), data:clean_data(ok), data:query_data(ok), data:make_chart(invalid_arguments), data:make_chart(ok), data:query_data(ok), data:query_data(ok), main:generate_image(ok), main:files(ok), main:notes(ok)

> Travel was the fastest-growing category in Q1, increasing by ₹4,500.00. I've completed your request, Arya: * **Cleaned the data**: Prepared a summary of your Q1 spending. * **Analysis**: Identified Travel as the highest growth category. * **Memo**: Created `memo.docx` with the spending breakdown and a chart. * **Note**: Saved "Travel" as your fastest-growing category for future reference. The chart in the memo shows the monthly trends, and you can find the detailed table in the document.

**dice_fairness** - tools: main:write_code(ok), coder:run_code(ok), main:analyze_data(None), data:inspect_data(ok), data:run_code(invalid_arguments), data:run_code(ok)

> The script has been written and executed, and the results are in. The die appears to be fair. A Chi-squared goodness-of-fit test was performed on the 6,000 rolls, resulting in a p-value of approximately 0.288. Since this is well above the standard 0.05 threshold, we cannot reject the hypothesis that the die is uniform. **Observed counts:** | Roll | Count | | :--- | :--- | | 1 | 970 | | 2 | 974 | | 3 | 989 | | 4 | 1001 | | 5 | 999 | | 6 | 1067 | | **Total** | **6,000** |

**meeting_actions** - tools: main:files(ok), main:files(ok), main:draw_diagram(ok)

> I've created `actions.md` with the checklist and drawn a Gantt chart showing the upcoming deadlines. [Action Items Checklist] - [ ] Priya: Send budget draft (Deadline: 14 November) - [ ] Omar: Book the venue (Deadline: 20 November) - [ ] Lena: Review supplier contract (Deadline: 25 November) - [ ] Sam: Publish the agenda (Deadline: 1 December) [Gantt chart of the deadlines]

**survey_themes** - tools: main:analyze_data(ok), data:inspect_data(ok), data:analyze_text(ok), data:make_chart(ok)

> Customers complain about **delivery** the most, which accounts for **50%** of the total complaints. Here is the breakdown of the complaints: * **Delivery**: 50% * **Price**: 30% * **Quality**: 20% ![share-of-rows-by-label.png](image_1)

**invoice_rupees** - tools: main:research_web(ok), web:web_search(ok), web:read_webpage(ok), main:analyze_data(ok), data:inspect_data(ok), data:query_data(ok), main:calculate(ok)

> The USD to INR exchange rate today is **1 USD = 96.7689 INR**. The total of `invoices.csv` is 1,12,404.31 USD, which converts to exactly **1,084,228.75 INR**.

**churn_risk** - tools: main:analyze_data(ok), data:inspect_data(ok), data:predict(ok), data:query_data(ok), data:query_data(ok), data:run_code(ok)

> The main predictors of churn in your dataset are **support tickets** (more tickets increase churn risk) and **monthly hours** (lower usage increases churn risk). I've saved the 5 customers most at risk as `at_risk.csv`. | customer_id | support_tickets | monthly_hours | tenure_months | plan | churned | risk_score | | :--- | :--- | :--- | :--- | :--- | :--- | :--- | | C0007 | 5 | 21.0 | 29 | pro | no | 0.9452 | | C0290 | 4 | 4.9 | 24 | basic | no | 0.897 | | C0312 | 4 | 17.1 | 31 | basic | no | 0.819 | | C0285 | 4 | 16.9 | 51 | basic | no | 0.7219 | | C0107 | 3 | 5.2 | 48 | basic | no | 0.7026 |

**verified_picture** - tools: main:write_code(ok), coder:run_code(ok), main:view_image(ok)

> I've created that for you, Arya. I've verified the image shows the title 'My budget' and that rent is the largest slice. ![budget.png](image_1)

**ambiguous_top** - tools: main:analyze_data(ok), data:inspect_data(ok), data:clean_data(ok), data:query_data(ok)

> The top product is **Widget Pro** with a total revenue of **7,182.0**.

**cancel_download** - tools: main:files(None)

> I've cancelled the download for the pandas README as requested.

**plan_forecast_deck** - tools: main:plan(ok), main:analyze_data(ok), data:inspect_data(ok), data:clean_data(ok), data:query_data(ok), data:forecast(ok), data:forecast(ok), data:run_code(ok)

> The best month in your sales data was December 2024, with 6,206.50 in sales. For the next quarter (Oct–Dec 2025), sales are forecasted to grow from approximately 6,061 to 6,184. I've created the **outlook.pptx** slide deck containing the forecast chart for you.
