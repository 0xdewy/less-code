# Pinned corpus baseline

Weighted post-formatter code LOC: **13620 → 13268 (static) → 13268 (full pipeline)** | static 2.58% | raw 2.58% | audited (static, idempotent) 2.58%

| Project | Language | LOC | static % | raw % | audited % | LLM considered / accepted | Tests/API/docs |
|---|---:|---:|---:|---:|---:|---:|---:|
| boltons | python | 8939 → 8619 | 3.58% | 3.58% | 3.58% | - | pass |
| more-itertools | python | 2652 → 2620 | 1.21% | 1.21% | 1.21% | - | pass |
| yocto-queue | javascript | 64 → 64 | 0.0% | 0.0% | 0.0% | - | pass |
| p-limit | javascript | 489 → 489 | 0.0% | 0.0% | 0.0% | - | pass |
| itoa | rust | 414 → 414 | 0.0% | 0.0% | 0.0% | - | pass |
| strsim-rs | rust | 1062 → 1062 | 0.0% | 0.0% | 0.0% | - | pass |

Every revision is pinned in `corpus.toml`; percentages are weighted by code LOC. `LLM considered / accepted` is `ml_stats`: symbols the backend inspected vs. proposals that survived the gate stack. Empty when no `lc` ML backend is wired.
