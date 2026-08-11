"""Token-to-USD accounting (ARCHITECTURE.md §7, §2.7).

Converts per-call usage — input, cached-input, output — into dollars via
config/pricing.yaml. Cached input is priced separately; getting that wrong makes
every cost number in the eval report wrong, so these figures get checked against
a real invoice once in M1.
"""
