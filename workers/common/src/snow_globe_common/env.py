"""Environment variables: ``SNOW_GLOBE_<X>`` first, then the deprecated ``LOOKOUT_<X>`` (D-080).

The product was called Lookout until 21 Nov 2026, when it was renamed Snow Globe (D-080).
Every variable the code reads is now named ``SNOW_GLOBE_<X>``. The old name ``LOOKOUT_<X>``
is still read when the new one is unset, because jobs started before the rename set it (the
capture loop's and the forecast stand-in's scripts set ``LOOKOUT_MANIFEST_DIR`` and
``LOOKOUT_FORECAST_*``), and so do old shells (``LOOKOUT_DATA_DIR``). Everything that sets a
variable (scripts, Terraform, tests) sets the new name.

A variable counts as unset when it is missing or blank (whitespace only), which is how every
reader treated an empty value before the rename. So ``SNOW_GLOBE_X=`` never hides a
``LOOKOUT_X`` that is set.

Every reader of a ``SNOW_GLOBE_*`` variable goes through :func:`env_get`. Code that cannot
import this package (bench/, docs/checks/, the shell scripts, Rust and TypeScript) applies the
same rule itself.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

__all__ = ["LEGACY_PREFIX", "PREFIX", "env_get", "env_source", "legacy_name"]

#: The prefix of every variable the code reads.
PREFIX = "SNOW_GLOBE_"
#: The prefix before the rename, still read as a fallback (D-080).
LEGACY_PREFIX = "LOOKOUT_"


def legacy_name(name: str) -> str:
    """``LOOKOUT_<X>`` for ``SNOW_GLOBE_<X>``; ``ValueError`` for a name without that prefix."""
    if not isinstance(name, str) or not name.startswith(PREFIX) or name == PREFIX:
        raise ValueError(f"{name!r} is not a {PREFIX}<X> variable name")
    return LEGACY_PREFIX + name[len(PREFIX) :]


def env_source(name: str, environ: Mapping[str, str] | None = None) -> str | None:
    """Which variable supplies ``name``: ``name`` itself, its ``LOOKOUT_`` form, or None.

    ``name`` is the new name, ``SNOW_GLOBE_<X>``. The first of the two that is set and not
    blank wins.
    """
    env = os.environ if environ is None else environ
    for candidate in (name, legacy_name(name)):
        value = env.get(candidate)
        if value is not None and value.strip():
            return candidate
    return None


def env_get(
    name: str, default: str | None = None, *, environ: Mapping[str, str] | None = None
) -> str | None:
    """The value of ``SNOW_GLOBE_<X>``, else of the deprecated ``LOOKOUT_<X>``, else ``default``.

    ``name`` is the new name. The value is returned as set (callers strip it if they want),
    and a blank value counts as unset (module docstring).
    """
    env = os.environ if environ is None else environ
    source = env_source(name, env)
    return default if source is None else env[source]
