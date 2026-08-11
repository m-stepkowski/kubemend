"""Configuration model for `kubemend.yaml` (ARCHITECTURE.md §8).

Loads the single config file with environment-variable overrides via
pydantic-settings: model tiers and pricing table, run budgets, context knobs,
observability endpoints, the read-only kubeconfig, and the GitOps backend with
its `writable_globs` path policy.
"""
