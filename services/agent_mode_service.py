# -*- coding: utf-8 -*-
"""Canonical experiment-session Agent mode helpers.

``agent_mode`` is the only runtime authority.  The two historical boolean
columns are retained as derived compatibility fields for older exports and
clients; they must never be combined into a dual-Agent runtime mode.
"""

VALID_AGENT_MODES = frozenset({"none", "strategy", "emotion"})
VALID_PIPELINE_MODES = frozenset({"strategy", "state_only"})
INVALID_AGENT_CONFIGURATION = "INVALID_AGENT_CONFIGURATION"


class InvalidAgentConfiguration(ValueError):
    """Raised when a session cannot be represented by one valid Agent mode."""


def validate_agent_mode(value):
    if not isinstance(value, str):
        raise InvalidAgentConfiguration(
            f"agent_mode must be one of {sorted(VALID_AGENT_MODES)}"
        )
    mode = value.strip().lower()
    if mode not in VALID_AGENT_MODES:
        raise InvalidAgentConfiguration(
            f"agent_mode must be one of {sorted(VALID_AGENT_MODES)}"
        )
    return mode


def _enabled(value):
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def flags_for_agent_mode(mode, *, research_state_monitoring_enabled=False):
    mode = validate_agent_mode(mode)
    research_enabled = _enabled(research_state_monitoring_enabled)
    return {
        "strategy_agent_enabled": mode == "strategy",
        "emotion_agent_enabled": mode == "emotion",
        "agent_intervention_enabled": mode == "strategy",
        "research_state_monitoring_enabled": research_enabled,
        "state_monitoring_enabled": (
            mode in {"strategy", "emotion"} or research_enabled
        ),
    }


def agent_mode_from_legacy_flags(strategy_enabled, emotion_enabled):
    strategy_enabled = bool(strategy_enabled)
    emotion_enabled = bool(emotion_enabled)
    if strategy_enabled and emotion_enabled:
        raise InvalidAgentConfiguration(
            f"{INVALID_AGENT_CONFIGURATION}: strategy and emotion cannot both be enabled"
        )
    if strategy_enabled:
        return "strategy"
    if emotion_enabled:
        return "emotion"
    return "none"


def resolve_session_agent_mode(session):
    """Resolve one stored session row without silently prioritizing old flags."""
    if not session:
        return "none"
    raw_mode = session.get("agent_mode")
    if raw_mode is not None and str(raw_mode).strip():
        return validate_agent_mode(raw_mode)
    return agent_mode_from_legacy_flags(
        session.get("strategy_agent_enabled", False),
        session.get("emotion_agent_enabled", False),
    )


def agent_config_from_session(session):
    """Return a safe config payload; invalid legacy dual-open rows run nothing."""
    try:
        mode = resolve_session_agent_mode(session)
    except InvalidAgentConfiguration as exc:
        flags = flags_for_agent_mode("none")
        return {
            "agent_mode": None,
            **flags,
            "configuration_error": INVALID_AGENT_CONFIGURATION,
            "configuration_error_detail": str(exc),
        }
    return {
        "agent_mode": mode,
        **flags_for_agent_mode(
            mode,
            research_state_monitoring_enabled=session.get(
                "research_state_monitoring_enabled", False
            ),
        ),
        "configuration_error": None,
    }


def pipeline_mode_from_session(session):
    """Return the state pipeline path for one session, or ``None`` if disabled."""
    config = agent_config_from_session(session)
    if config.get("configuration_error"):
        return None
    if config.get("agent_mode") == "strategy":
        return "strategy"
    if config.get("state_monitoring_enabled"):
        return "state_only"
    return None
