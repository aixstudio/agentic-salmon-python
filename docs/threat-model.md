# Threat Model

## Protected Assets

- Human safety and authority.
- Personal images and household context.
- Credentials and provider configuration.
- Private prompts, sessions, traces, and research notes.
- Accuracy of the public architectural claim.

## Primary Risks and Controls

| Risk | Control |
| --- | --- |
| Vision overclaims identity, freshness, or safety | Separate observations from unknowns; require human evidence; never let confidence bypass policy. |
| Connection invents or mis-types a relationship | Require evidence-bound mentions; validate entity kinds and relationship shapes against a versioned ontology; fail on violations. |
| Prompt injection in labels or image-associated text | Treat extracted text as untrusted data; model adapters cannot alter policy or tool authority. |
| Unsafe action after missing evidence | Guard-enforced `ASK`/`STOP`/`ALLOW_BOUNDED` decision with negative-path tests. |
| Invalid or still-unknown hypothesis authorizes mismatched guidance | Review accepts only an offered supported/plausible hypothesis for action; Act revalidates and renders that exact selection. |
| Retrieved text injects instructions or unsupported claims | Use a reviewed immutable corpus; treat chunks as evidence data; require claim-specific citations; never let retrieval expand authority. |
| MCP tool exceeds its intended capability | Expose only read-only `search_knowledge` and `get_source`; accept no filesystem path, URL, shell, or write argument. |
| Semantic result is irrelevant or manipulated | Return scores, source IDs, content hashes, and reviewed metadata; test relevance and fail Act when its temperature claim lacks a supporting chunk. |
| Preference feedback weakens safety rules | Learning records preference only; policy is code-controlled and unchanged. |
| Personal metadata leaks through media | Keep raw media private; publish only reviewed crops with metadata removed. |
| Chats or hidden reasoning enter public Git | Clean-room allowlist export; deny session, prompt, trace, and private paths. |
| Credentials enter source or traces | Ignore `.env`, traces and `log/`; keep the public demo credential-free. |
| Wrong-order or repeated release | Persist phase claims with the run checkpoint; require saved adjudication before Proof-3 and reject replays. |
| Guessed human observations erase uncertainty | The proof flow accepts only APPROVE or STOP; no free-text observations are collected and all unknowns remain. |
| Embedding-model acquisition changes the trust boundary | Pin the model identity, isolate the cache, and keep unit tests independent of network/model downloads. |
| Model cooking advice conflicts with authoritative guidance | Do not export raw chat recommendations; cite USDA guidance and require a food thermometer. |

## Boundary

This repository demonstrates architecture. It does not determine food freshness or safety and does not replace authoritative food-safety guidance or human judgment. USDA guidance says fish should reach 145°F (62.8°C), measured with a food thermometer.

SQLite checkpoints are trusted local state, not signed approvals. Do not automatically retry an active operation interrupted by a crash. The demo does not claim production approval identity or exactly-once external side effects.
