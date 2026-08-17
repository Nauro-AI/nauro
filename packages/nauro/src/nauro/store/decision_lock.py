"""Lock for local decision-number allocation.

A decision file is named ``decisions/NNN-slug.md`` where ``NNN`` is
``max(existing) + 1``. The kernel computes that number and then writes the file,
and the per-target lock in ``write_file`` only excludes writers aiming at the
same filename, so two concurrent local writers compute the same number, slugify
distinct titles, and both land. ``decision_write_lock`` serializes the whole
allocate-then-write sequence on one lock under the decisions dir.

Prevention has to live here: the pull-time renumber repairs collisions only for
cloud projects, and both pull paths gate on one. The lock path is derived from
``store_path``, so it inherits any ``NAURO_HOME`` override.
"""

from contextlib import contextmanager
from pathlib import Path

from nauro.constants import DECISIONS_DIR
from nauro.store.store_lock import store_write_lock


@contextmanager
def decision_write_lock(store_path: Path):
    """Exclusive file lock spanning decision-number allocation and the write.

    Locks ``decisions/.lock``: no ``.md`` suffix to enumerate, no alias of ``write_file``.
    """
    with store_write_lock(store_path, DECISIONS_DIR, is_directory=True):
        yield
