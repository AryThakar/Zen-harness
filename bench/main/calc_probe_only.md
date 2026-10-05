# How the main model uses calculate

Real model (Gemma 4 E4B, thinking on), main's instructions and tools, the real calculate and its description. 20 questions.

- Right answers: 3/3
- Used calculate: 2/3; calls in all: 2; refused expressions: 0
- Handed to another tool first: 0 ()
- Functions written: tan x1, asin x1
- Notation: plain

| # | question | right | expressions written (-> result or error) | other tool | answer |
|---|---|---|---|---|---|
| 1 | What is sin of 30 degrees? | yes | (none) |  | [Sat 03 Oct 2026, 14:46] sin of 30 degrees is 0.5. |
| 2 | What is tan of 35 degrees? | yes | `tan(35 degrees)` -> 0.70020753821 |  | The tangent of 35 degrees is approximately 0.7002. |
| 3 | What angle in degrees has a sine of 0.5? | yes | `asin(0.5) * 180 / pi` -> 30.0 |  | [Sat 03 Oct 2026, 14:46] The angle in degrees that has a sine of 0.5 is 30 degrees. |
