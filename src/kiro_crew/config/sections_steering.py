"""The ``steering`` section.

An owner of the :mod:`kiro_crew.config.sections` facade; it never imports the facade.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kiro_crew.config.fields import _meta


@dataclass
class SteeringConfig:
    """Where global (operator-wide) steering is read from."""

    global_dir: str | None = field(
        default=None,
        metadata=_meta(
            "Global Steering Dir",
            "Directory of global steering documents. Unset = ~/.kiro/steering. "
            "A path (~ is expanded) reads that directory instead. Empty disables "
            "global steering. Project steering is unaffected.",
        ),
    )
