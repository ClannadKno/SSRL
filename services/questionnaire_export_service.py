# -*- coding: utf-8 -*-
"""Compatibility wrapper for the unified questionnaire research export."""

import logging

from services.research_export_service import build_research_export


logger = logging.getLogger(__name__)


def export_questionnaire_raw_zip(session_ids=None):
    """Return the full questionnaire ZIP using the shared research builder.

    ``session_ids`` is accepted only for old callers.  The current teacher
    export contract is always full-scope, so the value is intentionally ignored.
    """
    if session_ids:
        logger.warning(
            "Ignoring deprecated questionnaire session filter; exporting all sessions"
        )
    return build_research_export("questionnaires")
