"""Adapter-layer write lock for whole-store read-modify-write sequences.

Several kernel operations read a shared store file, mutate it in memory, and
write it back (``flag_question`` appends an entry to ``open-questions.md``;
``update_state`` rewrites ``state_current.md`` and appends to
``state_history.md``). The per-target :class:`~filelock.FileLock` in
``FilesystemStore.write_file`` only excludes writers aiming at the *same*
filename, so two concurrent local writers each read the same pre-image, append
distinct entries, and the second write overwrites the first — one entry is
silently lost.

``store_write_lock`` closes that race by serializing the whole
read-modify-write kernel call on a single lock derived from ``store_path`` (so
it inherits any ``NAURO_HOME`` override without re-resolving it). The kernels
stay lock-agnostic; the lock is held by the adapter around the kernel call.

The lock path must never collide with the ``<name>.lock`` that ``write_file``
takes for the same target, because ``flock`` is not reentrant across file
descriptors: the outer read-modify-write lock nesting the kernel's inner
``write_file`` on the same path would self-deadlock. Two resource shapes keep
the two distinct:

* **Directory-scoped resources** (``decisions/``, ``snapshots/``) take the
  bare ``<dir>/.lock`` sentinel inside the directory. ``write_file`` never
  targets that bare name, so the two never alias.
* **Root-level files** (``open-questions.md``, ``state_current.md``,
  ``state_history.md``) have no owning subdirectory, so the sentinel is a
  sibling ``<name>`` with the :data:`RMW_LOCK_SUFFIX` suffix —
  ``open-questions.md.rmwlock`` — distinct from ``write_file``'s
  ``open-questions.md.lock``.
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
    """Return the lock-file path for a read-modify-write on *resource*.

    Args:
        store_path: The project store root. The lock path is derived from it,
            so any ``NAURO_HOME`` override already baked into ``store_path`` is
            inherited without re-resolving it here.
        resource: Store-relative path of the resource being mutated — a
            root-level filename (``open-questions.md``) or a directory name
            (``decisions``).
        is_directory: When ``True``, *resource* names a directory and the lock
            is the bare ``<dir>/.lock`` sentinel inside it. When ``False``
            (the default), *resource* names a root-level file and the lock is a
            sibling with the :data:`RMW_LOCK_SUFFIX` suffix.
    """
    target = store_path / resource
    if is_directory:
        return target / DIR_LOCK_NAME
    return target.with_name(target.name + RMW_LOCK_SUFFIX)


@contextmanager
def store_write_lock(store_path: Path, resource: str, *, is_directory: bool = False):
    """Serialize a whole read-modify-write kernel call on *resource*.

    The lock spans only the kernel call that reads and rewrites the resource.
    Best-effort side effects such as snapshot capture and cloud push must stay
    outside the lock — the snapshot machinery self-serializes under its own
    lock, and holding this lock across a network push would needlessly block
    other local writers.
    """
    lock_path = rmw_lock_path(store_path, resource, is_directory=is_directory)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(lock_path)):
        yield
