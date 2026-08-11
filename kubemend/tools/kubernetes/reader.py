"""Read-only cluster reader (ARCHITECTURE.md §3.2, tool contract `get_k8s_state`).

Runs against the generated read-only kubeconfig. Non-allow-listed kinds return a
`forbidden_kind` error; ConfigMaps return key names and value lengths only;
Secret values are never fetched. Strips managedFields and last-applied noise,
and caps events at 30 sorted by recency.
"""
