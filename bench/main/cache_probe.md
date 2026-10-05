# Prompt cache: old layout vs new

Tokens llama-server read again (prompt_n) and reused from its cache (cache_n), per request.

| turn | what changed before it | old: read | old: reused | new: read | new: reused |
|---|---|---|---|---|---|
| 1 | - | 2210 | 4 | 1443 | 774 |
| 2 | - | 480 | 2173 | 480 | 2176 |
| 3 | a table added | 2330 | 777 | 512 | 2615 |
| 4 | a note saved | 2802 | 799 | 536 | 3086 |
| 5 | a file added | 3253 | 799 | 503 | 3581 |
| 6 | - | 480 | 4011 | 480 | 4043 |

old: read 9,345 tokens over turns 2-6

new: read 2,511 tokens over turns 2-6
