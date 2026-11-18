"""The nightly pipeline for Sommus's own small model (docs/decisions/0014-own-model-nightly.md).

Stages: export real interactions, split, synthetic expansion, train (only on hardware that can), evaluate, report.
Run it with `sommus model-nightly` or `python -m sommus.modeltrain`.
"""
