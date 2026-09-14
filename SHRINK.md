# less-code report

- language: `python`
- LOC: 629 -> 609 (post-static) -> 609 (final)
- reduction: **3.18%**
- tests green: True  |  API preserved: True (baseline: original)
- documentation preserved: True
- LOC counted after canonical formatting: True
- gate strength: 7 tests

## Per-layer yield

- **external-static** 629 -> 629 [reverted]
- **rules** 629 -> 609 [kept] (snapshot.py: rules {'inline-single-use-temp': 5, 'self-default-assignment': 1, 'pack-assignments': 14})
- **ruff-again** 609 -> 609 [reverted]
- **rules-again** 609 -> 609 [reverted]
