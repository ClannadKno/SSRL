# -*- coding: utf-8 -*-
"""
Huey task queue instance for SSRL-ESP background tasks.

Usage:
    uv run huey_consumer huey_instance.huey -k thread -w 2
"""
import logging
import os
from huey import SqliteHuey
from huey import registry as huey_registry
from config import DB_PATH, HUEY_DB_PATH, HUEY_IMMEDIATE, HUEY_SQLITE_TIMEOUT_SECONDS
from config import SERA_LLM_ENABLED, SERA_LLM_API_KEY, SERA_LLM_BASE_URL, SERA_LLM_MODEL
from services.logging_setup import configure_utf8_logging

configure_utf8_logging()

os.makedirs(os.path.dirname(HUEY_DB_PATH), exist_ok=True)
logging.getLogger(__name__).info(
    "database_paths business=%s queue=%s",
    os.path.normcase(os.path.realpath(os.path.abspath(DB_PATH))),
    os.path.normcase(os.path.realpath(os.path.abspath(HUEY_DB_PATH))),
)


def _patch_huey_message_pickle_compat():
    """Allow workers to read Huey messages serialized by nearby versions.

    Huey stores queued task metadata as a pickled namedtuple.  If a task was
    queued by a version whose Message tuple had extra trailing fields, Python
    calls the current Message.__new__ with too many positional arguments before
    Huey can apply its own compatibility handling.  Trimming surplus trailing
    fields keeps the queue readable while preserving the fields Huey 2.6 uses.
    """
    message_cls = huey_registry.Message
    fields = message_cls._fields

    def _message_new(cls, *values):
        if len(values) < len(fields):
            values = values + (None,) * (len(fields) - len(values))
        elif len(values) > len(fields):
            values = values[:len(fields)]
        return tuple.__new__(cls, values)

    message_cls.__new__ = staticmethod(_message_new)


_patch_huey_message_pickle_compat()

huey = SqliteHuey(
    name='sera',
    filename=HUEY_DB_PATH,
    results=False,
    utc=True,
    immediate=HUEY_IMMEDIATE,
    immediate_use_memory=False,
    timeout=HUEY_SQLITE_TIMEOUT_SECONDS,
)

# Import task modules so @huey.task() decorated functions are
# registered with the consumer process.  The circular import from
# agent/*_tasks → huey_instance is safe because `huey` is defined
# above before the imports.
from agent import monitoring_tasks  # noqa: F401
from agent import intervention_tasks  # noqa: F401
from agent import help_tasks  # noqa: F401
from agent import maintenance_tasks  # noqa: F401
from agent import auto_submit_tasks  # noqa: F401
from agent import emotion_tasks  # noqa: F401
from agent import state_finalization_tasks  # noqa: F401

# --- Startup diagnostic ---
_key_state = "set" if SERA_LLM_API_KEY else "empty"
print(f"[huey] LLM config: ENABLED={SERA_LLM_ENABLED}, MODEL={SERA_LLM_MODEL}, KEY={_key_state}")
_env_model = __import__("os").environ.get("SERA_LLM_MODEL", "(not set)")
_env_key = "YES" if __import__("os").environ.get("SERA_LLM_API_KEY") else "(not set)"
print(f"[huey] LLM env from os.environ: MODEL={_env_model}, KEY={_env_key}")
print(f"[huey] LLM BASE_URL from config={SERA_LLM_BASE_URL}")
print("[huey] Hint: set SERA_LLM_API_KEY / SERA_LLM_BASE_URL / SERA_LLM_MODEL env vars for the huey_consumer process")
