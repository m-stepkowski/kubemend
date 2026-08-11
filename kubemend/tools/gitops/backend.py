"""GitBackend Protocol (ARCHITECTURE.md §4.2).

Three operations — open a branch, write files, open a draft PR. Deliberately
narrow: there is no method here that could push to a protected branch or touch a
cluster, which is what makes invariant I5 hold by construction.
"""
