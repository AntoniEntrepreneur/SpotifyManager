"""Duplicate-edition analysis of the saved-album library.

The judgement logic here is pure: it takes a library snapshot as data and returns a
plan as data. Nothing in `normalize`, `ranking`, `planner` or `resolve` touches the
network, the filesystem or the clock, and nothing here imports from `infra`.

The one exception is `ledger`, which owns the not-duplicates file on disk. It is kept
in this package because the judgements it stores are a dedupe concept, but the
planner never calls it: the pairs reach `plan` as a plain value, passed in by the
command layer.
"""
