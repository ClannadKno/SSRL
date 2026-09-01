# -*- coding: utf-8 -*-
"""UTF-8 logging bootstrap shared by Flask and Huey processes."""

from __future__ import annotations

import logging
import os
import sys


def _repo_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def configure_utf8_logging() -> None:
    """Configure conservative UTF-8 logging once per process."""
    if getattr(logging, "_sera_utf8_logging_configured", False):
        return

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    root = logging.getLogger()
    root.setLevel(getattr(logging, os.environ.get("SERA_LOG_LEVEL", "INFO").upper(), logging.INFO))
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    has_stream = any(isinstance(handler, logging.StreamHandler) for handler in root.handlers)
    if not has_stream:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        root.addHandler(stream_handler)

    log_path = os.environ.get("SERA_LOG_FILE") or os.path.join(_repo_root(), "nqy.log")
    has_file = any(isinstance(handler, logging.FileHandler) for handler in root.handlers)
    if not has_file:
        try:
            file_handler = logging.FileHandler(log_path, encoding="utf-8")
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
        except Exception:
            logging.getLogger(__name__).warning("Failed to attach UTF-8 file logger", exc_info=True)

    logging._sera_utf8_logging_configured = True
