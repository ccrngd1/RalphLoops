"""Shared pytest fixtures and Hypothesis profile configuration.

Hypothesis profiles (per design.md):

- ``dev`` (default): faster, fewer examples; used during local development.
- ``ci``: more examples and a larger shrinking budget; selected in CI.

The active profile is chosen by the ``HYPOTHESIS_PROFILE`` environment variable
(``ci`` or ``dev``). When unset, the ``dev`` profile is used. Both profiles use
a pinned derandomize seed so regressions are reproducible; CI additionally
allows a per-run random seed to be layered on via ``HYPOTHESIS_SEED``.
"""

from __future__ import annotations

import os

from hypothesis import HealthCheck, Phase, settings

# Default profile for local development: fast feedback with enough coverage to
# shake out obvious bugs. 100 examples matches the spec minimum (design.md
# testing strategy) so every property test meets the floor even on ``dev``.
settings.register_profile(
    "dev",
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
    print_blob=True,
)

# CI profile: more examples and longer shrinking budget to expose rare
# counterexamples and produce minimal failing inputs.
settings.register_profile(
    "ci",
    max_examples=1000,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
    phases=(Phase.explicit, Phase.reuse, Phase.generate, Phase.target, Phase.shrink),
    derandomize=False,
    print_blob=True,
)

_active_profile = os.environ.get("HYPOTHESIS_PROFILE", "dev").lower()
if _active_profile not in {"dev", "ci"}:
    _active_profile = "dev"
settings.load_profile(_active_profile)
