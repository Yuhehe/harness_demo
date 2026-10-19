### Cost

Standard (peak) price for every token, the same formula for all runs (`llm/cost.py`).

| Run | Tasks | Total | $/task mean (P50) | $/resolved | Tokens in/out (mean) | Cache hit | Compaction share | Steps |
|---|---|---|---|---|---|---|---|---|
| M0 | 50 (`verified_50`) | $10.293 | $0.206 ($0.116) | $0.278 | 1910k / 20k | 98.2% | 0.0% | 53.4 |
| B0 | 50 (`verified_50`) | $8.046 | $0.161 ($0.108) | $0.206 | 1293k / 17k | 97.8% | — | 41.7 |
| M0-r2 | 25 (`verified_25`) | $4.173 | $0.167 ($0.122) | $0.174 | 1384k / 18k | 98.1% | — | 47.5 |
| A2 | 25 (`verified_25`) | $5.282 | $0.211 ($0.141) | $0.230 | 1847k / 21k | 98.1% | 0.0% | 54.4 |
| A5 | 25 (`verified_25`) | $4.191 | $0.168 ($0.120) | $0.191 | 1419k / 18k | 98.0% | — | 47.3 |
| A6 | 25 (`verified_25`) | $6.241 | $0.250 ($0.178) | $0.284 | 1132k / 20k | 92.4% | 4.2% | 59.8 |
| A6xA1 | 25 (`verified_25`) | $2.003 | $0.080 ($0.087) | $0.134 | 470k / 8.6k | 95.8% | — | 29.0 |

Where the calls go (per task; classes from the command text, `aggregate.py`):

| Run | Explore | Run scripts | Run tests | Edit source | Todo | Tasks with one source edit | Responses with several calls |
|---|---|---|---|---|---|---|---|
| M0 | 29.8 | 12.9 | 5.1 | 2.5 | 1.7 | 24/50 | 4.0% |
| B0 | 24.3 | 10.5 | 3.9 | 1.8 | 0.0 | 32/50 | 5.4% |
