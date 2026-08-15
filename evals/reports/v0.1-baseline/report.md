# Eval sweep report

model: claude-sonnet-5

| scenario | pass | iters (avg) | cost (avg) | p95 wall |
|---|---|---|---|---|
| bad-image-tag | 5/5 | 7.6 | $0.29 | 96s |
| oom-limit | 5/5 | 7.8 | $0.26 | 66s |
| missing-configmap-key | 5/5 | 12.0 | $0.35 | 106s |
| bad-probe-path | 4/5 | 8.4 | $0.38 | 348s |
| bad-env-endpoint | 5/5 | 7.4 | $0.38 | 61s |
| quota-conflict | 5/5 | 10.0 | $0.56 | 290s |
