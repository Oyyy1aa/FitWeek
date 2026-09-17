# Evaluation

FitWeek includes deterministic offline evaluation and contract datasets. Evaluation is designed to verify controlled invariants and regression behavior without requiring a live model provider.

## Datasets

| Dataset | Cases | Purpose |
| --- | ---: | --- |
| Plan | 200 | Planning constraints, scheduling boundaries, immutable history, side-effect gates, and determinism |
| Memory | 108 | Candidate review, status/TTL/evidence filters, user isolation, degraded Context, and determinism |
| Profile Agent contracts | 50 | Scope and closed-output contract behavior |
| Session Design contracts | 60 | Frozen exercise candidate selection and validation |
| Schedule contracts | 72 | Controlled slot selection and timing rules |
| Recovery contracts | 80 | Recovery scope and candidate-action restrictions |

The Plan and Memory JSONL files have JSON Schemas and a manifest containing counts and SHA-256 hashes. Tests reject duplicate IDs/content, forbidden privacy fields, local paths, and manifest drift.

## Runner

```powershell
python -m app.evaluation.runner --dataset plan
python -m app.evaluation.runner --dataset memory
python -m app.evaluation.runner --ablations
```

The runner supports case/tag filters for focused evaluation, fixed seeds, bounded per-case timeouts, deterministic repetitions, stable fingerprints, machine-readable JSON output, and non-zero exit codes for invalid data or failed gates.

## Gates

Configured gates include pass-rate and minimum-case thresholds plus zero-tolerance checks for hard-constraint escape, unexpected side effects, duplicate Calendar events, immutable-history violations, internal evaluation errors, and non-deterministic output.

A high average score cannot hide a failed zero-tolerance gate.

## Controlled ablations

Six modes are represented:

- full behavior;
- no Memory Context;
- deterministic Session fallback;
- manual-only Calendar;
- no Recovery adjustment;
- template-only behavior.

Ablation configuration cannot disable protected safety gates. The comparison output is intended to show behavioral dependence, not statistical model-quality significance.

## What the evaluation proves

- dataset integrity and privacy-field checks;
- deterministic output for the covered local evaluators;
- enforcement of the encoded domain invariants;
- safe behavior of declared fallback/ablation paths;
- stable report and gate calculation.

## What it does not prove

- clinical safety or exercise-program effectiveness;
- live LLM semantic quality;
- real-user usability or intent accuracy;
- provider availability or OAuth correctness;
- production latency, throughput, cost, or SLO compliance;
- completion of the Recovery Application MySQL runtime gap.

Generated reports in `evaluation/reports/` are reference artifacts for the committed deterministic dataset/version. Re-run the runner after changing evaluation code, data, schemas, gates, or deterministic policies.
