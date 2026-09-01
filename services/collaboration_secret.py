# -*- coding: utf-8 -*-
"""Shared collaboration secret management (enhanced).

Provides a single source of truth for the SSRL_ESP_SECRET used to
sign collaboration tickets, ensuring Flask and the ASGI collaboration
server always agree on the same secret.

Also manages COLLAB_INTERNAL_SECRET used for HTTP internal API calls
between Flask and the ASGI collaboration server.

Priority order:
    1. Environment variable SSRL_ESP_SECRET
    2. .collab_secret file in the project root
    3. Generate a new secret, write it to .collab_secret, and set the env var
"""

import hashlib
import logging
import os
import secrets

logger = logging.getLogger(__name__)

# Project root is the grandparent of services/
PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
COLLAB_SECRET_FILE = os.path.join(PROJECT_ROOT, ".collab_secret")
COLLAB_INTERNAL_FILE = os.path.join(PROJECT_ROOT, ".collab_internal_secret")


def secret_fingerprint(secret):
    """Return the first 12 hex chars of sha256(secret) for diagnostic logging.
    Returns 'NOT_SET' if secret is empty or None.
    """
    if not secret:
        return "NOT_SET"
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]


def ensure_collab_secret():
    """Get or create the shared collaboration secret.

    Returns:
        The secret string, guaranteed to be consistent for the lifetime
        of the project root on this machine (persisted to .collab_secret).
    """
    # 1. Prefer environment variable
    env_val = os.environ.get("SSRL_ESP_SECRET", "").strip()
    if env_val:
        return env_val

    # 2. Read from persisted file
    if os.path.isfile(COLLAB_SECRET_FILE):
        try:
            with open(COLLAB_SECRET_FILE, "r") as f:
                file_val = f.read().strip()
            if file_val:
                os.environ["SSRL_ESP_SECRET"] = file_val
                return file_val
        except (OSError, IOError) as e:
            logger.warning("Failed to read %s: %s", COLLAB_SECRET_FILE, e)

    # 3. Generate new secret and persist
    secret = secrets.token_urlsafe(32)
    try:
        with open(COLLAB_SECRET_FILE, "w") as f:
            f.write(secret + "\n")
        logger.info("Generated new collaboration secret -> %s", COLLAB_SECRET_FILE)
    except (OSError, IOError) as e:
        logger.warning("Failed to write %s: %s", COLLAB_SECRET_FILE, e)

    os.environ["SSRL_ESP_SECRET"] = secret
    return secret


def ensure_collab_internal_secret():
    """Get or create the persistent COLLAB_INTERNAL_SECRET.

    Unlike SSRL_ESP_SECRET which is shared across services and must be
    stable across restarts, the internal secret authenticates Flask-to-ASGI
    HTTP calls. It is persisted to .collab_internal_secret so that both
    processes running from the same project root agree on it.

    Returns:
        The secret string, persisted to .collab_internal_secret.
    """
    env_val = os.environ.get("COLLAB_INTERNAL_SECRET", "").strip()
    if env_val:
        return env_val

    if os.path.isfile(COLLAB_INTERNAL_FILE):
        try:
            with open(COLLAB_INTERNAL_FILE, "r") as f:
                file_val = f.read().strip()
            if file_val:
                os.environ["COLLAB_INTERNAL_SECRET"] = file_val
                return file_val
        except (OSError, IOError) as e:
            logger.warning("Failed to read %s: %s", COLLAB_INTERNAL_FILE, e)

    secret = secrets.token_urlsafe(24)
    try:
        with open(COLLAB_INTERNAL_FILE, "w") as f:
            f.write(secret + "\n")
        logger.info("Generated new internal secret -> %s", COLLAB_INTERNAL_FILE)
    except (OSError, IOError) as e:
        logger.warning("Failed to write %s: %s", COLLAB_INTERNAL_FILE, e)

    os.environ["COLLAB_INTERNAL_SECRET"] = secret
    return secret
