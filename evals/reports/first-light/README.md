# First light

The M3 acceptance e2e: a hand-injected fault (`task lab:break -- bad-image-tag`),
`kubemend run` with the real model, an independently-verified gate pass, and a
draft PR opened in the lab's gitea.

## Run

```
task run -- --task "shop-api pods are stuck and a rollout never finished" \
  --namespace shop --app shop-api --model main
```

- model: claude-sonnet-5
- reason: verified
- draft PR: http://localhost:3000/kubemend/gitops/pulls/2
- trace: trace.jsonl (this directory)

## Gate verdict (independently re-run, not the model's self-report)

| check | result | detail |
|---|---|---|
| helm_template | pass | rendered 1 app(s) |
| kyverno | pass | pass: 6, fail: 0, warn: 0, error: 0, skip: 0 |
| diff | pass | the change produces a real diff |
| scope | pass | 1 resource(s), all in scope |

## What this demonstrates

- the model diagnosed the injected fault (a nonexistent image tag) from pod
  events and deployment status, with evidence citations
- `propose_git_change` wrote a corrected `values.yaml` and opened a PR
- `validate_change` (the model's own self-check) and the harness's independent
  `PipelineGate.verify()` are separate code paths — I1 holds: nothing the
  model reports can end a run by itself
- the PR landed as an actual gitea draft (`WIP:` prefix) with the rationale
  and this same check table in its body
