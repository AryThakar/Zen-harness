# How the main model uses calculate

Real model (Gemma 4 E4B, thinking on), main's instructions and tools, the real calculate and its description. 20 questions.

- Right answers: 20/20
- Used calculate: 19/20; calls in all: 21; refused expressions: 1
- Handed to another tool first: 1 (research_web)
- Functions written: log2 x4, tan x2, ln x1, log10 x1, sin x1, asin x1, sqrt x1
- Notation: ^, °

| # | question | right | expressions written (-> result or error) | other tool | answer |
|---|---|---|---|---|---|
| 1 | What is 18% of 2,450? | yes | `2450 * 0.18` -> 441.0 |  | 18% of 2,450 is 441. |
| 2 | A price went from 1,250 to 1,410. What is the percentage increase? | yes | `((1410 - 1250) / 1250) * 100` -> 12.8 |  | The percentage increase is 12.8%. |
| 3 | What is 7 to the power 5? | yes | `7^5` -> 16807 |  | 7 to the power of 5 is 16,807. |
| 4 | What is the square root of 1764? | yes | (none) |  | [Sat 03 Oct 2026, 14:40] The square root of 1764 is 42. |
| 5 | What is the cube root of 2197? | yes | `2197^(1/3)` -> 13.0 |  | The cube root of 2197 is 13. |
| 6 | What is the natural log of 50? | yes | `ln(50)` -> 3.91202300543 |  | The natural log of 50 is approximately 3.9120. |
| 7 | What is log base 2 of 1024? | yes | `log2(1024)` -> 10.0 |  | Log base 2 of 1024 is 10. |
| 8 | What is log10 of 0.001? | yes | `log10(0.001)` -> -3.0 |  | The $\log_{10}$ of 0.001 is -3.0. |
| 9 | What is sin of 30 degrees? | yes | `sin(30 degrees)` -> 0.5 |  | The sine of 30 degrees is 0.5. |
| 10 | What is tan of 35 degrees? | yes | `tan(35°)$` -> Can't evaluate that expression: invalid syntax (<unknown>, line 1).<br>`tan(35)` -> 0.473814720414 | research_web | [Sat 03 Oct 2026, 14:41] The tangent of 35 degrees is approximately 0.70021. |
| 11 | What angle in degrees has a sine of 0.5? | yes | `asin(0.5) * (180 / pi)` -> 30.0 |  | The angle in degrees that has a sine of 0.5 is 30 degrees. |
| 12 | What is 10 factorial? | yes | `10!` -> 3628800 |  | 10 factorial is 3,628,800. |
| 13 | In how many ways can I choose 3 people from a group of 12? | yes | `(12 * 11 * 10) / (3 * 2 * 1)` -> 220.0 |  | There are 220 ways to choose 3 people from a group of 12. |
| 14 | I invest 50,000 at 7.5% a year, compounded monthly. How much do I have after 3 years? | yes | `50000 * (1 + 0.075 / 12)^(12 * 3)` -> 62572.3067757 |  | After 3 years, you will have **$62,572.31**. |
| 15 | What is the average of 12, 15, 22, 9 and 30? | yes | `(12 + 15 + 22 + 9 + 30) / 5` -> 17.6 |  | The average of 12, 15, 22, 9, and 30 is 17.6. |
| 16 | What is the population standard deviation of 2, 4, 4, 4, 5, 5, 7, 9? | yes | `sqrt(( (2-5)^2 + (4-5)^2 + (4-5)^2 + (4-5)^2 + (5-5)^2 + (5-5)^2 + (7-5)^2 + (9-5)^2 ) / 8)` -> 2.0 |  | The population standard deviation of 2, 4, 4, 4, 5, 5, 7, and 9 is **2**. |
| 17 | Solve 2x^2 - 7x + 3 = 0. | yes | `(7 + 5) / 4` -> 3.0<br>`(7 - 5) / 4` -> 0.5 |  | The solutions to the equation $2x^2 - 7x + 3 = 0$ are $x = 3$ and $x = 0.5$. |
| 18 | What is e to the power -0.5? | yes | `e^-0.5` -> 0.606530659713 |  | [Sat 03 Oct 2026, 14:42] $e$ to the power of $-0.5$ is approximately $0.60653$. |
| 19 | What is the entropy in bits of a distribution with probabilities 0.5, 0.25 and 0.25? | yes | `-(0.5 * log2(0.5) + 0.25 * log2(0.25) + 0.25 * log2(0.25))` -> 1.5 |  | The entropy of a distribution with probabilities 0.5, 0.25, and 0.25 is 1.5 bits. |
| 20 | Convert 72 degrees Fahrenheit to Celsius. | yes | `(72 - 32) * 5 / 9` -> 22.2222222222 |  | 72 degrees Fahrenheit is equal to 22.22 degrees Celsius. |
