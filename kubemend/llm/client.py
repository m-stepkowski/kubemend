"""LLMClient Protocol (ARCHITECTURE.md §2.7).

The narrow surface the loop depends on: send a rendered message list plus tool
schemas, get back text, tool calls, and usage. Everything provider-specific —
caching breakpoints, retry policy, token accounting — lives behind it.
"""
