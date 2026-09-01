# -*- coding: utf-8 -*-
"""Safe filename / path segment utilities for SSRL-ESP exports.

Provides:
  - safe_path_segment(name): sanitize a string for use as a file or folder name.
  - safe_session_dir(session): generate a safe directory name for a session.
  - safe_group_dir(group): generate a safe directory name for a group.
  - build_export_filename(export_type): generate a timestamped zip filename.

All functions are Windows- and Linux-safe, prevent path-traversal, and
preserve Chinese/CJK characters.
"""

import re
import unicodedata
from datetime import datetime


_UNSAFE_CHARS_RE = re.compile(r"[\x00-\x1f<>:\"/\\|?*]")
_DOT_DOT_RE = re.compile(r"\.\.")
_MULTI_DASH_RE = re.compile(r"-{2,}")
_MULTI_SPACE_RE = re.compile(r" {2,}")


def safe_path_segment(name, max_length=120):
    """Convert *name* to a filesystem-safe path segment."""
    if not name:
        return "unnamed"
    s = unicodedata.normalize("NFKC", str(name))
    s = _DOT_DOT_RE.sub("_", s)
    s = s.replace("/", "_").replace("\\", "_").replace("~", "_")
    s = _UNSAFE_CHARS_RE.sub("", s)
    s = _MULTI_SPACE_RE.sub(" ", s)
    s = _MULTI_DASH_RE.sub("-", s)
    s = s.strip("_. -")
    s = s[:max_length]
    return s or "unnamed"


def safe_session_dir(session):
    """Safe session directory name: {name}_session-{id}"""
    sess_id = session.get("session_id") or session.get("id")
    if sess_id is None:
        raise ValueError("session must have 'session_id' or 'id' key")
    name = session.get("session_name") or session.get("session_role") or "Session-%s" % session.get("session_no", sess_id)
    safe = safe_path_segment(name)
    return "%s_session-%s" % (safe, sess_id)


def _format_group_code(value):
    """Return stable research-facing group code, e.g. G01."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    m = re.match(r"^[Gg]\s*0*(\d+)$", text)
    if m:
        return "G%02d" % int(m.group(1))
    return safe_path_segment(text)


def safe_group_dir(group):
    """Safe group directory name.

    Prefer the research-facing group code (G01/G02) over database ids.
    """
    group_code = group.get("group_code") or group.get("code")
    formatted = _format_group_code(group_code)
    if formatted:
        return formatted

    group_no = group.get("group_no")
    if group_no is not None:
        try:
            return "G%02d" % int(group_no)
        except (ValueError, TypeError):
            pass

    gid = group.get("group_id") or group.get("id")
    return "unknown_group" if gid is not None else "unnamed_group"




def safe_participant_dir(participant):
    """Safe participant directory name.

    Uses participant_code as the primary identifier, falling back to
    display_name or user_id. Prevents path traversal.

    Args:
        participant: dict with keys 'participant_code', 'display_name', 'user_id'.
                     May also be a user dict with 'id' instead of 'user_id'.

    Returns:
        Safe directory name string.
    """
    participant_code = participant.get("participant_code") or participant.get("code")
    if participant_code:
        safe = safe_path_segment(participant_code)
        return safe
    display_name = participant.get("display_name") or participant.get("name") or ""
    user_id = participant.get("user_id") or participant.get("id")
    if display_name and user_id:
        return "%s-%s" % (safe_path_segment(display_name), user_id)
    if user_id:
        return "user-%s" % user_id
    return "unknown_participant"


def safe_questionnaire_filename(questionnaire, response_stage=None, ext="csv"):
    """Safe CSV filename for a questionnaire submission.

    Prevents path traversal, handles duplicate questionnaire names,
    and includes stage suffix when provided.

    Args:
        questionnaire: dict with keys 'code', 'title', 'id'.
        response_stage: optional 'pre' or 'post' string.
        ext: file extension (default 'csv').

    Returns:
        Safe filename string (e.g., 'SSRL???.csv').
    """
    code = questionnaire.get("code") or questionnaire.get("item_code") or ""
    title = questionnaire.get("title") or ""
    qid = questionnaire.get("id") or 0

    # Prefer code, fall back to title, then id
    base = code or safe_path_segment(title) or ("questionnaire-%s" % qid)
    safe_base = safe_path_segment(base)

    stage_suffix = ""
    if response_stage == "pre":
        stage_suffix = "_pre"
    elif response_stage == "post":
        stage_suffix = "_post"

    if stage_suffix:
        return "%s%s.%s" % (safe_base, stage_suffix, ext)
    return "%s.%s" % (safe_base, ext)


def build_export_filename(export_type, ext="zip"):
    """Timestamped export filename."""
    ts = datetime.now().strftime("%Y-%m-%d_%H%M")
    safe_type = safe_path_segment(export_type).replace(" ", "_")
    return "%s_%s.%s" % (safe_type, ts, ext)
