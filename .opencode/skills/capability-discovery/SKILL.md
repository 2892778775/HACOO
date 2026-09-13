---
name: capability-discovery
description: Progressive capability discovery on HACOO — how to explore available EDA tools and api_* methods before composing a new workflow. Use when the human asks what the platform can do, or when a request does not match any existing skill.
---

# Capability Discovery Flow (Skills 层)

HACOO exposes capabilities progressively to avoid context bloat (SPEC G5-G6).
When facing a NEW kind of request, follow this exploration order instead of
dumping the full method registry.

## Exploration sequence

1. `hacoo_ping` — confirm the gateway is reachable. If unreachable, tell the
   human to start the platform server (`python -m hacoo.server`).
2. `hacoo_list_methods()` with NO filter — read only method NAMES and
   one-line descriptions (do not fetch details yet).
3. Shortlist 2-5 methods relevant to the human's intent.
4. `hacoo_method_detail(method)` ONLY for shortlisted methods — fetch params,
   required flags, and capability level on demand.
5. Compose the call sequence; validate inputs (file paths, enum values) with
   the human before the first `write`-capability call.

## Session context

- Keep a running note of discovered methods so you do not re-list within the
  same conversation unless the platform was restarted.

## Anti-patterns

- Do NOT call `hacoo_method_detail` for every registered method up front.
- Do NOT invent method names — anything not returned by `hacoo_list_methods`
  will be rejected by the gateway (`UNREGISTERED_METHOD`).
