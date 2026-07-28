"""The check catalogue.

Every module here registers pure functions with :data:`~..registry.REGISTRY` at import
time; :meth:`Registry.load` imports them all.
"""

from __future__ import annotations
