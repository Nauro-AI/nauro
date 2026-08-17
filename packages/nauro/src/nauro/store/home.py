"""The Nauro home directory and the top-level paths inside it.

This module is the only place the home path is spelled: callers resolve
``~/.nauro`` or ``$NAURO_HOME`` through ``nauro_home``. The home holds the auth
token and the whole project store, so it is kept owner-only. Nothing outside the
standard library and ``nauro.constants`` is imported, so any module can use it.
"""

import logging
import os
from pathlib import Path

from nauro.constants import (
    CONFIG_FILENAME,
    DEFAULT_NAURO_HOME,
    NAURO_HOME_ENV,
    PROJECTS_DIR,
    REGISTRY_FILENAME,
)

logger = logging.getLogger("nauro.store.home")


def nauro_home() -> Path:
    """Return the Nauro home: ``$NAURO_HOME`` when set, else ``~/.nauro``."""
    return Path(os.environ.get(NAURO_HOME_ENV, Path.home() / DEFAULT_NAURO_HOME))


def registry_file() -> Path:
    """Return the path of the project registry in the Nauro home."""
    return nauro_home() / REGISTRY_FILENAME


def projects_dir() -> Path:
    """Return the path of the project store root in the Nauro home."""
    return nauro_home() / PROJECTS_DIR


def config_file() -> Path:
    """Return the path of the user config file in the Nauro home."""
    return nauro_home() / CONFIG_FILENAME


def ensure_nauro_home() -> Path:
    """Create the Nauro home at ``0o700`` and return it.

    A home left group- or other-accessible by an older build is tightened in place.
    """
    home = nauro_home()
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        if (home.stat().st_mode & 0o077) != 0:
            home.chmod(0o700)
    except OSError as exc:
        # Best-effort tightening; a real failure surfaces at the caller's next write.
        logger.debug("Could not tighten %s to 0o700: %s", home, exc)
    return home
