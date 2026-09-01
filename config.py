# -*- coding: utf-8 -*-
"""Global configuration constants for SSRL-ESP."""
import os

def _env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name, default):
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return int(value)


def _env_float(name, default):
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return float(value)


## Batch 10: Experiment mode cleanup switches
#
# Default = experiment mode (formal experiment):
#   - Key-only login (no password, no register)
#   - No codebook export
#   - No legacy format exports
#   - Anonymous display (display_name, no real_name)
#
# Override via environment variables for development.

SSRL_EXPERIMENT_MODE = _env_bool("SSRL_EXPERIMENT_MODE", True)
"""Top-level experiment mode flag.

When True (default):
  - Password login (/login/password) is disabled
  - Student registration (/register) is disabled
  - Codebook/roster legacy export is disabled
  - Old-format exports are hidden
When False, legacy developer features may be enabled individually.
"""

SSRL_ENABLE_REGISTER = _env_bool("SSRL_ENABLE_REGISTER", False)
"""Allow student self-registration. Default False (experiment mode).
Set SSRL_ENABLE_REGISTER=1 to enable the /register page.
"""

SSRL_ENABLE_CODEBOOK_EXPORT = _env_bool("SSRL_ENABLE_CODEBOOK_EXPORT", False)
"""Allow codebook/roster CSV export. Default False (privacy).
Set SSRL_ENABLE_CODEBOOK_EXPORT=1 to re-enable codebook.csv and roster.csv.
"""

SSRL_ENABLE_LEGACY_EXPORT = _env_bool("SSRL_ENABLE_LEGACY_EXPORT", False)
"""Allow old-format CSV exports (e.g. checkins.csv).
Set SSRL_ENABLE_LEGACY_EXPORT=1 to re-enable legacy exports.
"""

SSRL_ENABLE_STATE_SUITE_AUDIT = _env_bool(
    "SSRL_ENABLE_STATE_SUITE_AUDIT",
    False,
)
"""Expose the privacy-minimised state-suite audit API to authenticated teachers.

The endpoint is disabled by default and is intended only for isolated test
servers. Flask TESTING mode also enables it for automated route tests.
"""



SSRL_ENABLE_PASSWORD_LOGIN = _env_bool("SSRL_ENABLE_PASSWORD_LOGIN", False)
"""Switch to enable the hidden /login/password development backup entry.

Set via environment variable SSRL_ENABLE_PASSWORD_LOGIN=1.
Also automatically enabled when FLASK_ENV=development.
When False, /login/password returns 404 or a disabled response.
"""

APP_NAME = "协同学习实验平台"
DB_PATH = os.environ.get("SSRL_ESP_DB_PATH", os.path.join(os.path.dirname(__file__), "ssrl_esp.db"))
UPLOAD_DIR = os.environ.get("SSRL_ESP_UPLOAD_DIR", os.path.join(os.path.dirname(__file__), "uploads", "submissions"))
os.makedirs(UPLOAD_DIR, exist_ok=True)
ALLOWED_SUBMISSION_EXTENSIONS = {"doc", "docx"}
MAX_SUBMISSION_FILE_MB = _env_int("MAX_SUBMISSION_FILE_MB", 10)
MAX_SUBMISSION_FILE_BYTES = MAX_SUBMISSION_FILE_MB * 1024 * 1024
APP_HOST = os.environ.get("SSRL_ESP_HOST", "127.0.0.1")
APP_PORT = _env_int("SSRL_ESP_PORT", 8000)
APP_DEBUG = _env_bool("SSRL_ESP_DEBUG", False)
SSRL_ESP_SECRET = os.environ.get("SSRL_ESP_SECRET", "").strip()
CONSENT_VERSION = os.environ.get("SSRL_ESP_CONSENT_VERSION", "v1").strip() or "v1"
RESEARCH_GROUP_MIN = _env_int("RESEARCH_GROUP_MIN", 1)
RESEARCH_GROUP_MAX = _env_int("RESEARCH_GROUP_MAX", 12)
EXPERIMENT_MODE = _env_bool("EXPERIMENT_MODE", False)
USE_LLM_ANALYSIS = _env_bool("USE_LLM_ANALYSIS", _env_bool("SERA_LLM_ENABLED", True))
STATE_ANALYSIS_INTERVAL_SECONDS = _env_int("STATE_ANALYSIS_INTERVAL_SECONDS", 30)
SEMANTIC_ANALYSIS_MIN_MESSAGES = _env_int("SEMANTIC_ANALYSIS_MIN_MESSAGES", 4)
SEMANTIC_ANALYSIS_MAX_INTERVAL_SECONDS = _env_int("SEMANTIC_ANALYSIS_MAX_INTERVAL_SECONDS", 999999)
STATE_WINDOW_MINUTES = _env_int("STATE_WINDOW_MINUTES", 10)
STATE_CONFIRM_WINDOWS = _env_int("STATE_CONFIRM_WINDOWS", 1)
ENABLE_BACKGROUND_SCHEDULER = _env_bool("ENABLE_BACKGROUND_SCHEDULER", True)
ENABLE_INTERVENTION_EFFECT_TRACKING = _env_bool("ENABLE_INTERVENTION_EFFECT_TRACKING", False)
SCHEDULER_LOOP_INTERVAL_SECONDS = _env_float(
    "SCHEDULER_LOOP_INTERVAL_SECONDS",
    min(5.0, max(1.0, float(STATE_ANALYSIS_INTERVAL_SECONDS))),
)
INTERVENTION_COOLDOWN_SECONDS = _env_int("INTERVENTION_COOLDOWN_SECONDS", 0)
INTERVENTION_COOLDOWN_MESSAGES = _env_int("INTERVENTION_COOLDOWN_MESSAGES", 0)
STUDENT_HELP_COOLDOWN_SECONDS = _env_int("STUDENT_HELP_COOLDOWN_SECONDS", 45)
STUDENT_HELP_WINDOW_MINUTES = _env_int("STUDENT_HELP_WINDOW_MINUTES", 10)
STUDENT_HELP_MAX_REQUESTS_PER_WINDOW = _env_int("STUDENT_HELP_MAX_REQUESTS_PER_WINDOW", 4)
ALLOW_FORMAL_RESET_OPERATIONS = _env_bool("ALLOW_FORMAL_RESET_OPERATIONS", False)
# Database auto-repair switch:
# True   = reset deprecated demo-password hashes on each startup.
# False  = keep existing password hashes if accounts already exist.
# [DEPRECATED] Demo password reset. Default False in experiment mode.
# Will be removed in a future cleanup.
RESET_DEMO_PASSWORDS_ON_START = _env_bool("RESET_DEMO_PASSWORDS_ON_START", False)
# Demo group settings: default 4 students/group to simulate s1-s4 same-group discussion.
# In formal experiments, disable RESET_DEMO_GROUP_MEMBERS_ON_START to avoid overwriting real groups.
# [DEPRECATED] Demo group settings. Kept for compatibility; always disabled in experiment mode.
DEMO_GROUP_SIZE = _env_int("DEMO_GROUP_SIZE", 4)
RESET_DEMO_GROUP_MEMBERS_ON_START = (
    _env_bool("RESET_DEMO_GROUP_MEMBERS_ON_START", False) and False
)



# ============================================================
# Huey background task configuration
# ------------------------------------------------------------
HUEY_DB_PATH = os.environ.get(
    "HUEY_DB_PATH",
    os.path.join(os.path.dirname(__file__), "data", "tasks.db"),
)
HUEY_WORKERS = _env_int("HUEY_WORKERS", 2)
HUEY_IMMEDIATE = _env_bool("HUEY_IMMEDIATE", False)
HUEY_ENABLED = _env_bool("HUEY_ENABLED", True)
HUEY_SQLITE_TIMEOUT_SECONDS = _env_float("HUEY_SQLITE_TIMEOUT_SECONDS", 30.0)

# Emotion reflection uses one global scanner and discussion-scoped time slots.
# The origin is group_session_discussions.started_at (the instant the final
# expected member enters); no per-session recursive Huey chain is created.
# Emotion feedback uses immutable five-minute slots. Keep this fixed so
# deployment environment overrides cannot shorten or lengthen the cadence.
EMOTION_INTERVAL_SECONDS = 300
EMOTION_SLOT_MAX_ATTEMPTS = _env_int("EMOTION_SLOT_MAX_ATTEMPTS", 2)
EMOTION_SLOT_RETRY_DELAY_SECONDS = _env_int("EMOTION_SLOT_RETRY_DELAY_SECONDS", 60)
EMOTION_SLOT_PENDING_TIMEOUT_SECONDS = _env_int("EMOTION_SLOT_PENDING_TIMEOUT_SECONDS", 180)
EMOTION_SLOT_RUNNING_TIMEOUT_SECONDS = _env_int("EMOTION_SLOT_RUNNING_TIMEOUT_SECONDS", 600)
EMOTION_SLOT_DEFER_INITIAL_SECONDS = _env_int("EMOTION_SLOT_DEFER_INITIAL_SECONDS", 5)
EMOTION_SLOT_DEFER_MAX_SECONDS = _env_int("EMOTION_SLOT_DEFER_MAX_SECONDS", 30)
EMOTION_SLOT_DEFER_MAX_ATTEMPTS = _env_int("EMOTION_SLOT_DEFER_MAX_ATTEMPTS", 6)
EMOTION_SLOT_MAX_COMPENSATION_SECONDS = _env_int(
    "EMOTION_SLOT_MAX_COMPENSATION_SECONDS", 600
)
EMOTION_SCAN_MAX_DISCUSSIONS = _env_int("EMOTION_SCAN_MAX_DISCUSSIONS", 100)
# Deprecated compatibility setting retained for historical telemetry and old
# deployment configuration only. Agent modes do not apply cross-Agent spacing.
EMOTION_STRATEGY_SPACING_SECONDS = _env_int(
    "EMOTION_STRATEGY_SPACING_SECONDS",
    _env_int("AGENT_CROSS_CHANNEL_MIN_INTERVAL_SECONDS", 60),
)
AGENT_CROSS_CHANNEL_MIN_INTERVAL_SECONDS = EMOTION_STRATEGY_SPACING_SECONDS

 
# ============================================================
# Online collaboration silence detection parameters
# ------------------------------------------------------------
# Offline classroom: silence can be detected via audio gaps; online platform should use:
# "Online but lacking" text interaction / uneven participation / broken response chains.
# Parameters below are used by agent.detector and routes.api.
# ============================================================
# Window (seconds) within which a student API call means they are still online.
ONLINE_ACTIVE_SECONDS = int(os.environ.get('ONLINE_ACTIVE_SECONDS', '180'))
# Minimum online students to treat "no speech" as a group-level silence risk.
ONLINE_SILENCE_MIN_ACTIVE_MEMBERS = int(os.environ.get('ONLINE_SILENCE_MIN_ACTIVE_MEMBERS', '2'))
# Minimum online duration without text to judge complete silence risk; avoid misjudging just after login.
ONLINE_SILENCE_NO_MSG_SECONDS = int(os.environ.get('ONLINE_SILENCE_NO_MSG_SECONDS', '180'))
# More severe continuous no-text threshold, for evidence display and post-analysis.
ONLINE_SILENCE_SEVERE_SECONDS = int(os.environ.get('ONLINE_SILENCE_SEVERE_SECONDS', '300'))
# Observation window for low-interaction silence detection.
ONLINE_LOW_INTERACTION_MINUTES = _env_int("ONLINE_LOW_INTERACTION_MINUTES", STATE_WINDOW_MINUTES)
# If messages in window <= this value, discussion is not sufficiently active.
ONLINE_LOW_INTERACTION_MSG_COUNT = int(os.environ.get('ONLINE_LOW_INTERACTION_MSG_COUNT', '2'))
# If speakers in window <= this value, participation is insufficient / response chain is broken.
ONLINE_LOW_INTERACTION_SPEAKERS = int(os.environ.get('ONLINE_LOW_INTERACTION_SPEAKERS', '1'))
# If emotion check-in is only done once at discussion end, it shouldn't be the core real-time basis.
# Only check-ins within the last few minutes serve as real-time assistance signals.
CHECKIN_VALID_WINDOW_MINUTES = int(os.environ.get('CHECKIN_VALID_WINDOW_MINUTES', '10'))
# Student side polls messages every X seconds; to avoid triggering backend analysis on each poll, silent query throttling is set here.
ONLINE_PASSIVE_ANALYSIS_INTERVAL_SECONDS = _env_int(
    "ONLINE_PASSIVE_ANALYSIS_INTERVAL_SECONDS",
    STATE_ANALYSIS_INTERVAL_SECONDS,
)




# ============================================================
# Formal experiment concurrency and real-time feedback parameters
# ------------------------------------------------------------
# 100 concurrent users ~= 25 groups of 4. Settings below reduce poll and write pressure.
# Ensure SERA completes normal feedback within 10-20 seconds.
# ============================================================
# Student message batch fetch: max new messages returned per poll.
MESSAGE_FETCH_LIMIT = _env_int("MESSAGE_FETCH_LIMIT", 120)
# Student-side recommended polling interval; page reads this to set JS timer.
STUDENT_MESSAGE_POLL_SECONDS = _env_float("STUDENT_MESSAGE_POLL_SECONDS", 3)
STUDENT_ALERT_POLL_SECONDS = _env_float("STUDENT_ALERT_POLL_SECONDS", 5)
STUDENT_HEARTBEAT_SECONDS = _env_float("STUDENT_HEARTBEAT_SECONDS", 30)
# Teacher dashboard refresh interval. Not too short to avoid mass DB writes from teacher pages.
TEACHER_DASHBOARD_POLL_SECONDS = _env_float("TEACHER_DASHBOARD_POLL_SECONDS", 15)
# When reading states, prefer cached if recent and not expired; don't force-rewrite group_states.
TEACHER_STATE_MAX_AGE_SECONDS = _env_int("TEACHER_STATE_MAX_AGE_SECONDS", 30)
# SERA backend analysis queue. Fixed worker count to avoid creating too many threads at peak.
AGENT_WORKER_COUNT = _env_int("AGENT_WORKER_COUNT", 4)
AGENT_ANALYSIS_QUEUE_MAXSIZE = _env_int("AGENT_ANALYSIS_QUEUE_MAXSIZE", 200)
# Same-group normal trigger min analysis interval; strong signals can enqueue but will not run in parallel.
AGENT_GROUP_MIN_ANALYSIS_INTERVAL_SECONDS = _env_float("AGENT_GROUP_MIN_ANALYSIS_INTERVAL_SECONDS", 0)
# Same-group debounce window for duplicate triggers in the queue.
AGENT_ANALYSIS_DEBOUNCE_SECONDS = _env_float("AGENT_ANALYSIS_DEBOUNCE_SECONDS", 0)
# SQLite concurrency params. WAL + busy_timeout significantly reduces "database is locked".
SQLITE_BUSY_TIMEOUT_MS = _env_int("SQLITE_BUSY_TIMEOUT_MS", 10000)
SQLITE_CACHE_SIZE_KB = _env_int("SQLITE_CACHE_SIZE_KB", 20000)

# Side-by-side intelligent teaching agent (SERA) trigger parameters.
AGENT_COOLDOWN_MINUTES = INTERVENTION_COOLDOWN_SECONDS / 60
# High-priority states (e.g. bored, unwilling, conflict, stuck) allow shorter follow-up intervals to avoid missing key moments.
AGENT_ESCALATION_MINUTES = 0.5
AGENT_MIN_STUDENT_MSGS_AFTER_AGENT = INTERVENTION_COOLDOWN_MESSAGES
AGENT_CONTEXT_LIMIT = 12
AGENT_CONTEXT_MINUTES = 15
AGENT_MIN_CONFIDENCE = 0.65
# Formal research recommends moderate cooldown to avoid AI assistant over-interrupting in real classrooms.
# For coordinated testing, reduce via env vars, e.g. AGENT_COOLDOWN_SILENCE_SECONDS=30.
AGENT_STATE_COOLDOWN_SECONDS = {
    'conflict_tension': int(os.environ.get('AGENT_COOLDOWN_CONFLICT_SECONDS', '90')),
    'negative_silence': int(os.environ.get('AGENT_COOLDOWN_SILENCE_SECONDS', '120')),
    'blocked_frustration': int(os.environ.get('AGENT_COOLDOWN_FRUSTRATION_SECONDS', '120')),
    'task_detached': int(os.environ.get('AGENT_COOLDOWN_OFFTASK_SECONDS', '90')),
    'participation_imbalance': int(os.environ.get('AGENT_COOLDOWN_IMBALANCE_SECONDS', '120')),
    'coordination_disorder': int(os.environ.get('AGENT_COOLDOWN_COORDINATION_SECONDS', '120')),
    'positive_collaboration': int(os.environ.get('AGENT_COOLDOWN_POSITIVE_SECONDS', '300')),
    'unknown': int(os.environ.get('AGENT_COOLDOWN_UNKNOWN_SECONDS', '300')),
}
# v33: Quick trigger on off-topic/conflict (1-2 utterances).
AGENT_OFFTASK_QUICK_HITS = int(os.environ.get('AGENT_OFFTASK_QUICK_HITS', '1'))
AGENT_CONFLICT_QUICK_HITS = int(os.environ.get('AGENT_CONFLICT_QUICK_HITS', '1'))
# v33: Max follow-ups per sustained issue type = 3.
AGENT_MAX_FOLLOWUPS_PER_ISSUE = int(os.environ.get('AGENT_MAX_FOLLOWUPS_PER_ISSUE', '3'))
AGENT_ISSUE_WINDOW_MINUTES = int(os.environ.get('AGENT_ISSUE_WINDOW_MINUTES', '15'))
# v21: Avoid screen spam in formal experiments; allow fast second follow-up in test/real off-topic scenarios.
AGENT_FAST_FOLLOWUP_SECONDS = int(os.environ.get('AGENT_FAST_FOLLOWUP_SECONDS', '12'))
AGENT_FAST_FOLLOWUP_STUDENT_MSGS = int(os.environ.get('AGENT_FAST_FOLLOWUP_STUDENT_MSGS', '2'))
# v22: Keep checking after reply; if still off-task, continue low-frequency follow-up; allow light encouragement during active discussion.
AGENT_PERSISTENT_OFFTASK_SECONDS = int(os.environ.get('AGENT_PERSISTENT_OFFTASK_SECONDS', '10'))
AGENT_PERSISTENT_OFFTASK_STUDENT_MSGS = int(os.environ.get('AGENT_PERSISTENT_OFFTASK_STUDENT_MSGS', '2'))
AGENT_POSITIVE_COOLDOWN_SECONDS = int(os.environ.get('AGENT_POSITIVE_COOLDOWN_SECONDS', '120'))
AGENT_POSITIVE_MIN_MESSAGES = int(os.environ.get('AGENT_POSITIVE_MIN_MESSAGES', '4'))
# True  = SERA auto-sends guidance to students when triggered; teachers still see full suggestions, evidence, and logs.
# False = SERA only generates pending suggestions; teacher must manually push.
AUTO_PUSH_SERA_INTERVENTIONS = _env_bool("AUTO_PUSH_SERA_INTERVENTIONS", True)

# Optional: External LLM for context analysis and response generation.
# Usage:
# export SERA_LLM_ENABLED=1
# export SERA_LLM_API_KEY=your_api_key
# export SERA_LLM_BASE_URL=https://api.openai.com/v1/chat/completions  # or other OpenAI-compatible endpoint
# export SERA_LLM_MODEL=your_model_name
SERA_LLM_ENABLED = USE_LLM_ANALYSIS
SERA_LLM_API_KEY = os.environ.get("SERA_LLM_API_KEY", "")
SERA_LLM_BASE_URL = os.environ.get("SERA_LLM_BASE_URL", "https://api.openai.com/v1/chat/completions")
SERA_LLM_MODEL = os.environ.get("SERA_LLM_MODEL", "gpt-4o-mini")
SERA_LLM_TIMEOUT = _env_int("SERA_LLM_TIMEOUT", 18)
SERA_LLM_MAX_CONTEXT = _env_int("SERA_LLM_MAX_CONTEXT", 16)
SERA_LLM_MAX_TOKENS = _env_int("SERA_LLM_MAX_TOKENS", 1024)

REQUIRED_TABLES = {
    "users",
    "groups",
    "group_members",
    "client_sessions",
    "tasks",
    "messages",
    "emotion_checkins",
    "group_states",
    "interventions",
    "intervention_logs",
    "submissions",
    "agent_suggestions",
    "intervention_feedback",
    "settings",
    "state_assessments",
    "intervention_decisions",
    "questionnaires",
    "questionnaire_items",
    "questionnaire_responses",
    "manual_state_annotations",
    "process_events",
    "help_requests",
    "intervention_runs",
    "strategy_pipeline_runs",
    "strategy_definitions",
    "collaborative_documents",
   "collaborative_document_checkpoints",
    "submission_prepares",
    "experiment_participants",
    "teacher_access_keys",
    "experiment_sessions",
    "baseline_assignments",
    "audit_logs",
    "safety_signals",
    "group_session_controls",
    "intervention_uptake",
    "deliverable_scores",
    "autonomous_regulation_events",
}

# Collaboration server
COLLAB_WS_HOST = os.environ.get("COLLAB_WS_HOST", "127.0.0.1")
COLLAB_WS_PORT = _env_int("COLLAB_WS_PORT", 8001)
COLLAB_WS_EXTERNAL_URL = os.environ.get(
    "COLLAB_WS_EXTERNAL_URL", ""
).strip() or "ws://{}:{}".format(COLLAB_WS_HOST, COLLAB_WS_PORT)
# COLLAB_INTERNAL_SECRET is resolved by services.collaboration_secret.ensure_collab_internal_secret()
COLLAB_INTERNAL_SECRET = os.environ.get("COLLAB_INTERNAL_SECRET", "").strip()
COLLAB_TOKEN_TTL = _env_int("COLLAB_TOKEN_TTL", 900)  # 15 minutes (increased from 300)

# Import local overrides (API key, custom model, etc.)
try:
    from config_local import *  # noqa: F401, F403
except ImportError:
    pass
# ── LLM configuration ──
# Priority: system env var > config_local.py > hardcoded default.
# Move after config_local import so env vars win on the server.

# New pipeline monitoring feature flag
DISCUSSION_PIPELINE_V2_ENABLED = _env_bool("DISCUSSION_PIPELINE_V2_ENABLED", True)
DISCUSSION_PIPELINE_V2_SHADOW = _env_bool("DISCUSSION_PIPELINE_V2_SHADOW", False)

# New pipeline default configuration
PIPELINE_V2_MIN_NEW_MESSAGES_FOR_LLM = _env_int("PIPELINE_V2_MIN_NEW_MESSAGES_FOR_LLM", 4)
PIPELINE_V2_LLM_COOLDOWN_SECONDS = _env_int("PIPELINE_V2_LLM_COOLDOWN_SECONDS", 120)
PIPELINE_V2_ANALYZER_VERSION = "discussion_pipeline_v2_alpha"
PIPELINE_V2_SILENCE_DELAY_SECONDS = _env_int("PIPELINE_V2_SILENCE_DELAY_SECONDS", 45)
PIPELINE_V2_LLM_MAX_JSON_RETRIES = _env_int("PIPELINE_V2_LLM_MAX_JSON_RETRIES", 3)

# Incremental state-assessment structured output.  A model response gets at
# most one schema-repair retry; the gateway may still retry transient network
# failures according to its profile.
STATE_LLM_SCHEMA_MAX_ATTEMPTS = min(
    2, max(1, _env_int("STATE_LLM_SCHEMA_MAX_ATTEMPTS", 2))
)
STATE_LLM_MAX_SEGMENTS = min(4, max(1, _env_int("STATE_LLM_MAX_SEGMENTS", 4)))
STATE_LLM_MAX_EVIDENCE_PER_SEGMENT = min(
    3, max(1, _env_int("STATE_LLM_MAX_EVIDENCE_PER_SEGMENT", 3))
)
STATE_LLM_INTERVENTION_MESSAGE_MAX_CHARS = max(
    20, _env_int("STATE_LLM_INTERVENTION_MESSAGE_MAX_CHARS", 120)
)
STATE_LLM_OUTPUT_MAX_TOKENS = max(
    400, _env_int("STATE_LLM_OUTPUT_MAX_TOKENS", 1800)
)
# A schema-repair request includes the prior invalid output and may consume
# more reasoning tokens than the initial classification. Keep the ordinary
# call bounded while giving the single repair attempt enough room to emit the
# same compact JSON contract instead of failing solely at the token boundary.
STATE_LLM_REPAIR_OUTPUT_MAX_TOKENS = max(
    STATE_LLM_OUTPUT_MAX_TOKENS,
    _env_int("STATE_LLM_REPAIR_OUTPUT_MAX_TOKENS", 3200),
)
PIPELINE_V2_LLM_PERIODIC_MESSAGE_INTERVAL = _env_int("PIPELINE_V2_LLM_PERIODIC_MESSAGE_INTERVAL", 6)
PIPELINE_V2_LLM_PERIODIC_TIME_SECONDS = _env_int("PIPELINE_V2_LLM_PERIODIC_TIME_SECONDS", 180)
# Minimum confidence for automatic strategy intervention.  The old env var is
# kept as a compatibility alias, but state-LLM gating uses STATE_LLM_* below.
PIPELINE_V2_MIN_INTERVENTION_CONFIDENCE = _env_float(
    "PIPELINE_V2_MIN_INTERVENTION_CONFIDENCE",
    _env_float("PIPELINE_V2_LLM_MIN_INTERVENTION_CONFIDENCE", 0.4),
)
PIPELINE_V2_LLM_MIN_INTERVENTION_CONFIDENCE = PIPELINE_V2_MIN_INTERVENTION_CONFIDENCE
STATE_LLM_ENABLED = _env_bool("STATE_LLM_ENABLED", True)
STATE_LLM_GATE_MIN_RULE_SCORE = _env_float("STATE_LLM_GATE_MIN_RULE_SCORE", 0.25)
STATE_LLM_FORCE_AFTER_NEW_MESSAGES = _env_int("STATE_LLM_FORCE_AFTER_NEW_MESSAGES", 4)
STATE_LLM_MIN_NEW_MESSAGES = _env_int("STATE_LLM_MIN_NEW_MESSAGES", 2)
# Incremental assessment scheduling.  These names are intentionally separate
# from the legacy monitor-run gate above: they control when a fixed candidate
# window is claimed, not how the detector interprets that window.
STATE_LLM_MESSAGE_THRESHOLD = _env_int("STATE_LLM_MESSAGE_THRESHOLD", 4)
STATE_LLM_TIME_THRESHOLD_SECONDS = _env_int("STATE_LLM_TIME_THRESHOLD_SECONDS", 180)
STATE_LLM_MIN_INTERVAL_SECONDS = _env_int("STATE_LLM_MIN_INTERVAL_SECONDS", 30)
STATE_LLM_MAX_CANDIDATE_MESSAGES = _env_int("STATE_LLM_MAX_CANDIDATE_MESSAGES", 8)
STATE_LLM_CONTEXT_MESSAGES = _env_int("STATE_LLM_CONTEXT_MESSAGES", 3)
STATE_LLM_FAILURE_MAX_ATTEMPTS = _env_int("STATE_LLM_FAILURE_MAX_ATTEMPTS", 2)
STATE_LLM_FAILURE_BACKOFF_SECONDS = _env_int("STATE_LLM_FAILURE_BACKOFF_SECONDS", 30)
# A post-intervention observation may span more than one successful assessment,
# but old unclassified messages must not remain "observing" forever.
OBSERVATION_MAX_ASSESSMENT_ROUNDS = max(
    1, _env_int("OBSERVATION_MAX_ASSESSMENT_ROUNDS", 2)
)
LEGACY_GROUP_ANALYZE_ENABLED = _env_bool("LEGACY_GROUP_ANALYZE_ENABLED", False)
LEGACY_STRATEGY_DIRECT_PUBLISH_ENABLED = _env_bool(
    "LEGACY_STRATEGY_DIRECT_PUBLISH_ENABLED", False
)
"""Allow retired strategy paths to publish student-visible agent messages.

Default False keeps strategy publishing on the three-stage decision gate and
unified publisher. Enable only for historical development diagnostics.
"""
SINGLE_SPEAKER_SILENCE_MIN_ACTIVE = _env_int("SINGLE_SPEAKER_SILENCE_MIN_ACTIVE", 3)
SINGLE_SPEAKER_SILENCE_MIN_MESSAGES = _env_int("SINGLE_SPEAKER_SILENCE_MIN_MESSAGES", 4)
SINGLE_SPEAKER_SILENCE_SHARE = _env_float("SINGLE_SPEAKER_SILENCE_SHARE", 0.75)
SINGLE_SPEAKER_SILENCE_SECONDS = _env_int("SINGLE_SPEAKER_SILENCE_SECONDS", ONLINE_SILENCE_NO_MSG_SECONDS)

# ============================================================
# V2 auto-intervention pipeline feature flags
# ------------------------------------------------------------
AUTO_INTERVENTION_V2_ENABLED = _env_bool("AUTO_INTERVENTION_V2_ENABLED", True)
AUTO_INTERVENTION_V2_DRY_RUN = _env_bool("AUTO_INTERVENTION_V2_DRY_RUN", False)
INTERVENTION_V2_LOCK_SECONDS = _env_int("INTERVENTION_V2_LOCK_SECONDS", 75)
# Batch-1 latency evidence supports a 75s initial lease.  A live authoritative
# pipeline renews it every 20s and may never hold the room for more than 180s.
# THREE_STAGE_ROOM_LOCK_SECONDS remains a legacy environment-variable fallback
# and a Python alias so rolling deployments keep their previous configuration.
THREE_STAGE_LOCK_INITIAL_TTL_SECONDS = max(
    1,
    _env_int(
        "THREE_STAGE_LOCK_INITIAL_TTL_SECONDS",
        _env_int("THREE_STAGE_ROOM_LOCK_SECONDS", INTERVENTION_V2_LOCK_SECONDS),
    ),
)
_THREE_STAGE_HEARTBEAT_REQUESTED_SECONDS = max(
    1,
    _env_int("THREE_STAGE_LOCK_HEARTBEAT_SECONDS", 20),
)
THREE_STAGE_LOCK_HEARTBEAT_SECONDS = min(
    _THREE_STAGE_HEARTBEAT_REQUESTED_SECONDS,
    max(1, (THREE_STAGE_LOCK_INITIAL_TTL_SECONDS - 1) // 3),
)
THREE_STAGE_LOCK_MAX_TOTAL_SECONDS = max(
    THREE_STAGE_LOCK_INITIAL_TTL_SECONDS,
    _env_int("THREE_STAGE_LOCK_MAX_TOTAL_SECONDS", 180),
)
THREE_STAGE_ROOM_LOCK_SECONDS = THREE_STAGE_LOCK_INITIAL_TTL_SECONDS
INTERVENTION_V2_MAX_DELTA_STALE = _env_int("INTERVENTION_V2_MAX_DELTA_STALE", 20)
INTERVENTION_V2_MAX_CANDIDATE_STRATEGIES = _env_int("INTERVENTION_V2_MAX_CANDIDATE_STRATEGIES", 3)
STRATEGY_COOLDOWN_SECONDS = _env_int(
    "STRATEGY_COOLDOWN_SECONDS",
    _env_int("INTERVENTION_V2_COOLDOWN_SECONDS", 120),
)
INTERVENTION_V2_COOLDOWN_SECONDS = STRATEGY_COOLDOWN_SECONDS
INTERVENTION_V2_MAX_MESSAGES_FOR_CONTEXT = _env_int("INTERVENTION_V2_MAX_MESSAGES_FOR_CONTEXT", 20)
INTERVENTION_V2_MIN_MESSAGES_FOR_CONTEXT = _env_int("INTERVENTION_V2_MIN_MESSAGES_FOR_CONTEXT", 10)
INTERVENTION_V2_RECHECK_DELAY_SECONDS = _env_int("INTERVENTION_V2_RECHECK_DELAY_SECONDS", 30)

# Diagnostic debug flag
SSRL_AGENT_DEBUG = _env_bool("SSRL_AGENT_DEBUG", False)
