"""The forecast worker's hindcast mode: P1's 24 h-lead fetch plan and archived runs in the live
files' format (PRD §13.2, §13.4, §8.3; D-008, D-027).

Modules: :mod:`.storage` (the hindcast's own tree, ``<data dir>/forecast/hindcast/``, and a
read-only view of the live data dir), :mod:`.quiet` (no request between :50 and :05),
:mod:`.cachebucket` (listings and inventories kept, a persisted network ledger), :mod:`.sun`
(the Sun's altitude), :mod:`.frames` (the captured frames), :mod:`.plan` (frames to runs and
hours, with byte sizes), :mod:`.extractor` (the extractor in hindcast mode), :mod:`.fetch`
(the capped, resumable fetch), :mod:`.compare` (S3 availability against the live lag model),
:mod:`.leads` (design P's hours per run and MB per day) and :mod:`.cli`
(``snow-globe-forecast hindcast ...``).
"""
