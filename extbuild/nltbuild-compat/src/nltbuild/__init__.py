"""Compatibility namespace for the renamed funbuild package."""

import warnings

warnings.warn("nltbuild was renamed to funbuild", DeprecationWarning, stacklevel=2)

from funbuild import __path__  # noqa: E402,F401
