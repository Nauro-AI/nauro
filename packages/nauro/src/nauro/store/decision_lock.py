"""Lock for local decision-number allocation.

Decision files are named ``decisions/NNN-slug.md`` where ``NNN`` is
``max(existing num) + 1``. The kernel computes that number from the store and
then writes the file, but the per-target-file lock in ``write_file`` only
mutually excludes writers aiming at the *same* filename. Two concurrent local
writers compute the same next number, slugify distinct titles, and both land —
yielding two decisions sharing a number.

``decision_write_lock`` closes that race by serializing the whole
allocate-then-write sequence on a single lock under the decisions dir. The
remote path renumbers colliding decisions on pull, but that pull-time repair
never runs for local-only projects (both pull paths gate on a cloud project),
so prevention has to live here at the adapter layer. Mirrors
``snapshot._snapshot_lock``: the lock path is derived from ``store_path`` so it
inherits any NAURO_HOME override without re-resolving it.
"""

from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock

from nauro.constants import DECISIONS_DIR


@contextmanager
def decision_write_lock(store_path: Path):
    """Exclusive file lock spanning decision-number allocation and the write.

    The lock file is ``<store_path>/decisions/.lock``. It carries no ``.md``
    suffix, so it is excluded from the ``*.md`` decision enumeration in
    ``list_decisions``. The lock path differs from ``write_file``'s
    per-file ``<file>.lock``, so the two never deadlock.
    """
    decisions_dir = store_path / DECISIONS_DIR
    lock_path = decisions_dir / ".lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(lock_path)):
        yield
