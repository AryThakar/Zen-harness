# Main slot failures: the model, or the sampling?

Each case replayed 10 times against the raw model (Gemma 4 E4B, thinking on), with the instructions as they were when it failed and none of the code fixes in the way.

| case | setting | outcomes |
|---|---|---|
| ask_user | Qwen leftovers (t 0.6, top_k 20) | ok: 10 |
| ask_user | Gemma's own (t 1.0, top_k 64) | ok: 10 |
| stamp | Qwen leftovers (t 0.6, top_k 20) | ok: 10 |
| stamp | Gemma's own (t 1.0, top_k 64) | ok: 9, copied the stamp: 1 |
| currency | Qwen leftovers (t 0.6, top_k 20) | ok: 9, added a currency: 1 |
| currency | Gemma's own (t 1.0, top_k 64) | ok: 8, added a currency: 2 |
| choice | Qwen leftovers (t 0.6, top_k 20) | ok: 3, vague (day/month): 2, choice missing: 5 |
| choice | Gemma's own (t 1.0, top_k 64) | ok: 5, choice missing: 3, vague (day/month): 2 |
