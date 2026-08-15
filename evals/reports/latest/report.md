# Eval sweep report

model: claude-haiku-4-5

| scenario | pass | iters (avg) | cost (avg) | p95 wall |
|---|---|---|---|---|
| bad-env-endpoint | 3/5 | 8.5 | $0.11 | 64s |
| bad-image-tag | 4/5 | 6.4 | $0.08 | 55s |
| bad-probe-path | 4/5 | 9.6 | $0.10 | 48s |
| missing-configmap-key | 5/5 | 9.2 | $0.09 | 71s |
| oom-limit | 4/5 | 9.5 | $0.08 | 91s |
| quota-conflict | 4/5 | 12.6 | $0.14 | 64s |
