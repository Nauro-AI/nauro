"""Adapter-layer write lock for whole-store read-modify-write sequences.

A kernel that reads a store file, mutates it in memory and writes it back is not
protected by the per-target lock in ``FilesystemStore.write_file``, which only
excludes writers aiming at the same filename: two local writers read one
pre-image and the second write drops the first entry. ``store_write_lock``
serializes the whole kernel call on one lock derived from ``store_path``, so it
inherits any ``NAURO_HOME`` override, and the kernels stay lock-agnostic.

The lock path must never collide with the ``<name>.lock`` that ``write_file``
takes for the same target: ``flock`` is not reentrant across descriptors, so the
outer lock nesting the inner write would self-deadlock. A directory-scoped
resource takes the bare ``<dir>/.lock`` sentinel inside it, which ``write_file``
never targets; a root-level file takes a sibling with ``RMW_LOCK_SUFFIX``.
"""

from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock

# Sentinel suffix for root-level file locks. Deliberately distinct from the
# ``.lock`` suffix ``write_file`` appends, so the adapter read-modify-write
# lock and the kernel's per-target write lock never alias the same path.
RMW_LOCK_SUFFIX = ".rmwlock"

# Sentinel filename for directory-scoped resources, placed inside the
# directory. Carries no ``.md`` suffix, so it is excluded from the ``*.md``
# enumeration that lists decisions and snapshots.
DIR_LOCK_NAME = ".lock"


def rmw_lock_path(store_path: Path, resource: str, *, is_directory: bool = False) -> Path:
    """Return the lock-file path for a read-modify-write on ``resource``.

    ``is_directory`` picks the bare ``<dir>/.lock`` over a sibling ``RMW_LOCK_SUFFIX`` file.
    """
    target = store_path / resource
    if is_directory:
        return target / DIR_LOCK_NAME
    return target.with_name(target.name + RMW_LOCK_SUFFIX)


@contextmanager
def store_write_lock(store_path: Path, resource: str, *, is_directory: bool = False):
    """Serialize a whole read-modify-write kernel call on ``resource``.

    Best-effort side effects, snapshot capture and cloud push, stay outside the lock.
    """
    lock_path = rmw_lock_path(store_path, resource, is_directory=is_directory)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(lock_path)):
        yield
