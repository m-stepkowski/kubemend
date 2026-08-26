# Efficacy verification — the gate's largest open gap

Status: **design only, not implemented.** Written 2026-08-26, prompted by the
M14 cheap-tier baseline. Folded into **M16** rather than standing alone; §5
explains why it cannot be separated from the sandbox work.

## 1. The gap, stated precisely

`validate_change` runs five stages: render, policy, diff, scope, quota. Every
one asks a question about the *proposal*:

- does it render?
- does it violate a policy?
- does it change anything real?
- does it stay inside the run's scope?
- does it fit the live quota?

**None asks whether it fixes the incident.** A values edit that renders
cleanly, breaks no policy, produces a genuine in-scope diff and fits the quota
is reported as `verified` — and the model is told it succeeded — even when the
fault is untouched.

This is not a bug in any stage. Each is correct about its own question. It is a
missing question.

## 2. What made it concrete

`fix-needs-template-change` is a negative scenario: the correct outcome is a
`fix_not_expressible_in_values` handoff. Its break adds one line to
`templates/deployment.yaml`:

```yaml
readinessProbe:
  httpGet:
    path: {{ .Values.probes.readiness.path }}
    port: http
    scheme: HTTPS        # <- the break; no values field controls it
```

The probe now speaks HTTPS to a plaintext port, so pods never become Ready.
**No edit to `values.yaml` can fix this**, because `scheme` is a template
literal, not a values reference.

In the M14 baseline the model "fixed" it and the gate agreed **3 times out of
5** (`evals/reports/cheap-baseline/diagnosis.md`). It typically changed
`probes.readiness.path` — a real field, a real render change, a real diff, in
scope, within quota. Five green checks on a change that cannot work.

Half of `bad-env-endpoint`'s failures are the same shape: proposals that passed
the gate with `UPSTREAM_URL` still pointing at the wrong port.

## 3. Why no static check can close it

The tempting fixes all fail on this scenario specifically, which is what makes
it a good test of any proposal:

| idea | why it fails here |
|---|---|
| Require the proposal to touch the field the symptom implicates | Nothing maps a free-text symptom to a values field. The implicated field (`scheme`) is not *in* values at all. |
| Diff the rendered manifest before/after and require a "meaningful" change | There *is* a meaningful change. `probes.readiness.path` genuinely differs. |
| Have the model declare an `expected_effect` and verify it against the render | The model's claim would be **true**: it said it changed the probe path, and the manifest shows exactly that. `scheme: HTTPS` survives regardless. |
| Detect that the fault line is template-literal rather than values-derived | Requires knowing which line is the fault — that is the diagnosis itself, not something the gate can derive. |

The general result: **efficacy is a claim about runtime behaviour, and no
amount of manifest inspection settles it.** The proposal is well-formed by
construction; what is wrong is the world it will produce, and the only way to
learn that is to produce it and look.

This is worth stating plainly because the cheap partials are seductive. The
`expected_effect` idea in particular is a genuinely useful control — it catches
"claimed X, changed Y" — but it addresses a *different* failure class, and
shipping it as "the efficacy fix" would overstate what it does.

## 4. The only sound design: apply and re-probe

1. Render the proposal.
2. Apply it to a **disposable namespace** seeded to mirror the incident.
3. Re-run the scenario's own symptom probe against that namespace.
4. Verified only if the symptom clears; otherwise fail with
   `symptom_persists`, which is precisely the signal the model needs and has
   never had.
5. Tear the namespace down regardless of outcome.

The model gets a genuinely new kind of feedback: not "your YAML is acceptable"
but "your change does not fix the thing you were asked to fix". For
`fix-needs-template-change` that failure repeated once or twice is exactly the
evidence that should push it toward the handoff the scenario expects.

## 5. Why this belongs to M16 and cannot ship before it

Step 2 needs **cluster write access**, and that collides with three standing
commitments at once:

- **CLAUDE.md hard rule 3** — only `propose_git_change` has external side
  effects, and no tool may gain cluster-mutating capability. An applying gate
  is not a tool, but it is unquestionably a new side-effecting path.
- **The read-only identity** (§4.1, `docs/threat-model.md`) — today the agent's
  ServiceAccount cannot mutate anything, and that is load-bearing in the threat
  model, not incidental. `kubectl diff` was already rejected as the primary
  diff engine for exactly this reason: dry-run is authorized like a write.
- **Blast radius** — "apply the untrusted model's manifest to a real cluster"
  is the single most dangerous operation this project could add. It needs
  isolation, a separate identity, guaranteed teardown, and its own threat-model
  section.

That list *is* M16's sandbox: per-run isolated execution, a distinct identity,
and a Kyverno pack constraining what the sandbox may do. Building an applying
gate before the sandbox means building the dangerous half first and the
containment second — precisely backwards.

**Decision: fold into M16.** Efficacy verification becomes a consumer of the
sandbox rather than a milestone that races it, and M16 gains a concrete,
measurable acceptance criterion it did not previously have — better than
"isolation exists", which is hard to falsify.

## 6. What M16 should inherit from this

- **A named check.** `symptom_persists` as a sixth stage, skipped (passing)
  when no sandbox is configured, so single-repo local runs are unaffected.
- **The probe already exists.** `evals/lab.py`'s `SymptomProbe` is the
  re-probe mechanism, and it is already provider-neutral. Production runs would
  need an equivalent supplied by the trigger (an Alertmanager alert carries its
  own firing condition), which is the genuinely open design question.
- **A measurable acceptance test.** `fix-needs-template-change` moves from
  ~40% to reliably producing a handoff, *without* any prompt change. If it only
  moves after prompt tuning, the sandbox is not doing the work.
- **The negative case matters more than the positive.** A sandbox that reports
  "symptom cleared" for a change that did not fix anything is worse than no
  sandbox, because it launders a bad proposal into a verified one. The
  acceptance test should include a proposal known not to fix the fault.

## 7. Interim position

Until M16, the gap stays open and documented rather than papered over:

- `fix-needs-template-change` stays at roughly 40% and the baseline says why.
  That number is honest — it measures a real limitation, and tuning prompts to
  move it would hide the limitation without removing it.
- The `expected_effect` partial is **not** being built. If it is wanted later
  it should be justified on its own terms ("catch proposals that misdescribe
  themselves"), never as efficacy verification.
- `docs/knowledge/tool-contracts.md`'s `validate_change` section should say
  what the gate does *not* check. An adopter reading "verified" deserves to
  know it means safe and well-formed, not effective.
