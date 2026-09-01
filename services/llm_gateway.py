# -*- coding: utf-8 -*-
"""Unified LLM Gateway with connection pool, retry, three profiles, and error classification.

Replaces direct urllib calls in llm_analyzer.py.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Error classification constants
# ---------------------------------------------------------------------------
AUTHENTICATION_ERROR = "authentication_error"
RATE_LIMITED = "rate_limited"
CONNECT_TIMEOUT = "connect_timeout"
READ_TIMEOUT = "read_timeout"
NETWORK_ERROR = "network_error"
UPSTREAM_5XX = "upstream_5xx"
INVALID_RESPONSE = "invalid_response"
SCHEMA_VALIDATION_ERROR = "schema_validation_error"
UNKNOWN_ERROR = "unknown_error"
TRUNCATED_RESPONSE = "truncated_response"
REASONING_BUDGET_EXHAUSTED = "reasoning_budget_exhausted"

# Status codes that ARE eligible for retry
RETRYABLE_STATUS_CODES = {429, 502, 503, 504}

# Status codes that MUST NOT be retried
NON_RETRYABLE_STATUS_CODES = {400, 401, 403}


# ---------------------------------------------------------------------------
# Env helpers (keep it simple, no config dependency to avoid import cycles)
# ---------------------------------------------------------------------------
def _env_str(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)))
    except (TypeError, ValueError):
       return default


def _env_first(keys: list[str], default: Any) -> Any:
    for key in keys:
        value = os.environ.get(key)
        if value not in (None, ""):
            return value
    return default


def _bounded_env_float(
    keys: list[str],
    default: float,
    *,
    min_value: float,
    max_value: float,
) -> float:
    raw = _env_first(keys, default)
    key_label = "/".join(keys)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid LLM float env %s=%r; using default %s", key_label, raw, default)
        return float(default)
    if value < min_value:
        logger.warning("Invalid LLM float env %s=%r below %s; using default %s", key_label, raw, min_value, default)
        return float(default)
    if value > max_value:
        logger.warning("LLM float env %s=%r exceeds %s; clamping", key_label, raw, max_value)
        return float(max_value)
    return value


def _bounded_env_int(
    keys: list[str],
    default: int,
    *,
    min_value: int,
    max_value: int,
) -> int:
    raw = _env_first(keys, default)
    key_label = "/".join(keys)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid LLM int env %s=%r; using default %s", key_label, raw, default)
        return int(default)
    if value < min_value:
        logger.warning("Invalid LLM int env %s=%r below %s; using default %s", key_label, raw, min_value, default)
        return int(default)
    if value > max_value:
        logger.warning("LLM int env %s=%r exceeds %s; clamping", key_label, raw, max_value)
        return int(max_value)
    return value


def _profile_env_keys(env_prefix: str, suffix: str, aliases: list[str] | None = None) -> list[str]:
    prefixes = list(aliases or []) + [env_prefix]
    keys: list[str] = []
    for prefix in prefixes:
        key = f"{prefix}_{suffix}"
        if key not in keys:
            keys.append(key)
    return keys


# ---------------------------------------------------------------------------
# Fallback helper: try Python config module when env vars are not set
# ---------------------------------------------------------------------------
def _py_env(key: str, config_attr: str, default: str) -> str:
    """Prefer env var, fall back to config module attribute, then default."""
    val = os.environ.get(key)
    if val:
        return val
    try:
        from config import SERA_LLM_API_KEY, SERA_LLM_BASE_URL, SERA_LLM_MODEL
        lut = {"SERA_LLM_API_KEY": SERA_LLM_API_KEY, "SERA_LLM_BASE_URL": SERA_LLM_BASE_URL, "SERA_LLM_MODEL": SERA_LLM_MODEL}
        return lut.get(config_attr) or default
    except (ImportError, AttributeError):
        return default


# ---------------------------------------------------------------------------
# Diagnostic logging toggle (env-gated, off by default)
# ---------------------------------------------------------------------------
_LLM_DIAG_ENABLED = os.environ.get("SERA_LLM_DIAG", "0") == "1"


def _log_diag(event: str, **kwargs):
    """Log LLM diagnostic event when SERA_LLM_DIAG=1.
    All keyword args are serialised as JSON.  Never raises.
    """
    if not _LLM_DIAG_ENABLED:
        return
    try:
        safe = {}
        for k, v in kwargs.items():
            if isinstance(v, str) and len(v) > 300:
                safe[k] = v[:300] + "...[truncated]"
            else:
                safe[k] = v
        logger.info("[LLM_DIAG] %s - %s", event, json.dumps(safe, ensure_ascii=False, default=str))
    except Exception:
        pass



def _detect_truncated_content(raw_text, parsed=None):
    """Detect whether JSON decode failure was caused by content truncation."""
    if not raw_text:
        return False
    if isinstance(parsed, dict):
        choices = parsed.get("choices")
        if isinstance(choices, list) and len(choices) > 0:
            c0 = choices[0]
            finish_reason = c0.get("finish_reason", "")
            if finish_reason == "length":
                return True
            msg = c0.get("message", {})
            ctext = msg.get("content", "")
            if isinstance(ctext, list):
                ctext = "".join(b.get("text", "") for b in ctext if isinstance(b, dict))
            if not ctext or not ctext.strip():
                reasoning = msg.get("reasoning_content", "")
                if not reasoning:
                    return True
            if isinstance(ctext, str):
                stripped = ctext.strip()
                if stripped.startswith("{") and not stripped.endswith("}"):
                    return True
                if stripped.startswith("[") and not stripped.endswith("]"):
                    return True
            usage = parsed.get("usage", {})
            max_tokens_setting = parsed.get("max_tokens", 500)
            reasoning_tokens = usage.get("reasoning_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
            if reasoning_tokens > 0 and max_tokens_setting > 0:
                if reasoning_tokens >= max_tokens_setting * 0.6 and completion_tokens <= max_tokens_setting * 0.4:
                    return True
    if parsed is None and raw_text:
        if '"finish_reason":"length"' in raw_text or '"finish_reason": "length"' in raw_text:
            return True
        if '"content": "' in raw_text:
            import re as _re
            m = _re.search(r'"content"\s*:\s*"({[^}]*)$', raw_text)
            if m and not raw_text.rstrip().endswith("}"):
                return True
    return False


def _extract_json_text(content: Any) -> str:
    """Return the most likely JSON object/array text without inventing fields."""
    if isinstance(content, (dict, list)):
        return json.dumps(content, ensure_ascii=False)
    text = str(content or "").strip()
    if not text:
        return text
    fence_match = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()
    if text.startswith("{") or text.startswith("["):
        return text
    starts = [idx for idx in (text.find("{"), text.find("[")) if idx >= 0]
    if not starts:
        return text
    start = min(starts)
    closing = "}" if text[start] == "{" else "]"
    end = text.rfind(closing)
    if end > start:
        return text[start : end + 1].strip()
    return text[start:].strip()


def _flatten_message_content(content: Any) -> Any:
    """Normalize OpenAI-compatible content blocks without touching reasoning."""

    if isinstance(content, list):
        return "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict)
        )
    return content


def _usage_token_counts(usage: Any) -> tuple[int, int]:
    """Return completion/reasoning counts across common provider shapes."""

    usage = usage if isinstance(usage, dict) else {}
    completion_details = usage.get("completion_tokens_details")
    completion_details = (
        completion_details if isinstance(completion_details, dict) else {}
    )

    def _non_negative_int(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    completion_tokens = _non_negative_int(usage.get("completion_tokens"))
    reasoning_tokens = _non_negative_int(
        usage.get("reasoning_tokens")
        if usage.get("reasoning_tokens") is not None
        else completion_details.get("reasoning_tokens")
    )
    return completion_tokens, reasoning_tokens


def _unsupported_compatibility_fields(
    result: "LlmResult",
    field_names: tuple[str, ...],
) -> tuple[str, ...]:
    """Identify explicitly rejected optional request fields on one HTTP 400."""

    if not field_names or getattr(result, "status_code", None) != 400:
        return ()
    detail = " ".join(
        str(value or "")
        for value in (
            getattr(result, "failure_message", None),
            getattr(result, "raw_text", None),
        )
    ).lower()
    unsupported_markers = (
        "unsupported",
        "not supported",
        "unknown",
        "unrecognized",
        "unexpected",
        "extra fields",
        "not permitted",
        "invalid parameter",
        "invalid request",
    )
    if not any(marker in detail for marker in unsupported_markers):
        return ()
    return tuple(field for field in field_names if field.lower() in detail)


def _detect_truncated_content(raw_text, parsed=None):
    """Detect whether JSON decode failure was caused by content truncation."""
    if not raw_text:
        return False
    if isinstance(parsed, dict):
        choices = parsed.get("choices")
        if isinstance(choices, list) and len(choices) > 0:
            c0 = choices[0]
            finish_reason = c0.get("finish_reason", "")
            if finish_reason == "length":
                return True
            msg = c0.get("message", {})
            ctext = msg.get("content", "")
            if isinstance(ctext, list):
                ctext = "".join(b.get("text", "") for b in ctext if isinstance(b, dict))
            if not ctext or not ctext.strip():
                reasoning = msg.get("reasoning_content", "")
                if not reasoning:
                    return True
            if isinstance(ctext, str):
                stripped = ctext.strip()
                if stripped.startswith("{") and not stripped.endswith("}"):
                    return True
                if stripped.startswith("[") and not stripped.endswith("]"):
                    return True
            usage = parsed.get("usage", {})
            max_tokens_setting = parsed.get("max_tokens", 500)
            reasoning_tokens = usage.get("reasoning_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
            if reasoning_tokens > 0 and max_tokens_setting > 0:
                if reasoning_tokens >= max_tokens_setting * 0.6 and completion_tokens <= max_tokens_setting * 0.4:
                    return True
    if parsed is None and raw_text:
        if '"finish_reason":"length"' in raw_text or '"finish_reason": "length"' in raw_text:
            return True
        if '"content": "' in raw_text:
            import re as _re
            m = _re.search(r'"content"\s*:\s*"({[^}]*)$', raw_text)
            if m and not raw_text.rstrip().endswith("}"):
                return True
    return False

# ---------------------------------------------------------------------------
# Global fallback env vars (used when a profile does not override)
# ---------------------------------------------------------------------------
_GLOBAL_MODEL = _env_str("SERA_LLM_MODEL", _py_env("SERA_LLM_MODEL", "SERA_LLM_MODEL", "gpt-4o-mini"))
_GLOBAL_BASE_URL = _env_str("SERA_LLM_BASE_URL", _py_env("SERA_LLM_BASE_URL", "SERA_LLM_BASE_URL", "https://api.openai.com/v1/chat/completions"))
_GLOBAL_API_KEY = _env_str("SERA_LLM_API_KEY", _py_env("SERA_LLM_API_KEY", "SERA_LLM_API_KEY", ""))


# ---------------------------------------------------------------------------
# Profile definitions
# ---------------------------------------------------------------------------
@dataclass
class LlmProfile:
    """Configuration for one LLM calling scenario (temperature, timeouts, retries, model)."""

    name: str
    temperature: float
    connect_timeout: float
    read_timeout: float
    max_tokens: int
    retries: int
    model: str
    base_url: str
    api_key: str

    @classmethod
    def from_env(cls, name: str, *, env_prefix: str, defaults: dict) -> "LlmProfile":
        """Build a profile from environment variables, falling back to *defaults* dict."""
        aliases = defaults.get("env_aliases") or []
        return cls(
            name=name,
            temperature=_bounded_env_float(
                _profile_env_keys(env_prefix, "TEMPERATURE", aliases),
                defaults["temperature"],
                min_value=0.0,
                max_value=2.0,
            ),
            connect_timeout=_bounded_env_float(
                _profile_env_keys(env_prefix, "CONNECT_TIMEOUT", aliases),
                defaults["connect_timeout"],
                min_value=0.1,
                max_value=30.0,
            ),
            read_timeout=_bounded_env_float(
                _profile_env_keys(env_prefix, "READ_TIMEOUT", aliases),
                defaults["read_timeout"],
                min_value=1.0,
                max_value=60.0,
            ),
            max_tokens=_bounded_env_int(
                _profile_env_keys(env_prefix, "MAX_TOKENS", aliases),
                defaults["max_tokens"],
                min_value=1,
                max_value=16000,
            ),
            retries=_bounded_env_int(
                _profile_env_keys(env_prefix, "RETRIES", aliases),
                defaults["retries"],
                min_value=0,
                max_value=2,
            ),
            model=str(
                _env_first(
                    _profile_env_keys(env_prefix, "MODEL", aliases),
                    _env_str("SERA_LLM_MODEL", defaults.get("model", _GLOBAL_MODEL)),
                )
            ),
            base_url=str(
                _env_first(
                    _profile_env_keys(env_prefix, "BASE_URL", aliases),
                    _env_str("SERA_LLM_BASE_URL", defaults.get("base_url", _GLOBAL_BASE_URL)),
                )
            ),
            api_key=str(
                _env_first(
                    _profile_env_keys(env_prefix, "API_KEY", aliases),
                    _env_str("SERA_LLM_API_KEY", defaults.get("api_key", _GLOBAL_API_KEY)),
                )
            ),
        )


# Built-in profiles
BUILTIN_PROFILES: dict[str, dict] = {
    # state_detector profile:
# - max_tokens=1200: bounded room for up to four compact state segments.
# - temperature=0.0: deterministic classification.
# - Structured state responses can legitimately take longer than the short
#   conversational profiles.  Keep the read timeout below the global 60s cap,
#   but high enough that two slow response bodies do not terminalize a frozen
#   assessment window as unclassified.
"state_detector": {
        "temperature": 0.0,
        "connect_timeout": 3,
        "read_timeout": 45,
        "max_tokens": 1200,
        # Structured/schema repair is owned by LLMStateDetector so one bad
        # answer cannot be multiplied by a second hidden gateway retry loop.
        "retries": 0,
        "env_prefix": "SERA_STATE_DETECTOR",
    },
# emotion_reflection_generator profile:
# - max_tokens=1200: short output, with room for reasoning-model overhead so a
#   mandatory fixed-slot message cannot be lost to a truncated JSON envelope.
# - temperature=0.3: slight creativity for natural emotional phrasing.
"emotion_reflection_generator": {
        "temperature": 0.3,
        "connect_timeout": 3,
        "read_timeout": 8,
        "max_tokens": 1200,
        "retries": 1,
        "env_prefix": "SERA_EMOTION_REFLECTION_GENERATOR",
    },
    # Stage-one classifier for fixed-slot group participation feedback.
    "emotion_feedback_classifier": {
        "temperature": 0.0,
        "connect_timeout": 3,
        # This structured classifier can sit just beyond the old 8s boundary
        # even when the provider ultimately returns a small, valid JSON body.
        "read_timeout": 20,
        "max_tokens": 1800,
        "retries": 1,
        "env_prefix": "SERA_EMOTION_FEEDBACK_CLASSIFIER",
    },
    "intervention_generator": {
        "temperature": 0.5,
        "connect_timeout": 3,
        "read_timeout": 10,
        "max_tokens": 400,
        "retries": 1,
        "env_prefix": "SERA_INTERVENTION_GENERATOR",
    },
    "student_help": {
        "temperature": 0.6,
        "connect_timeout": 3,
        "read_timeout": 10,
        "max_tokens": 1000,
        "retries": 2,
        "env_prefix": "SERA_STUDENT_HELP",
    },
    "strategy_review_and_generation": {
        # Stage 3 emits only selected_strategy_id and intervention_text. Keep
        # the default completion budget aligned with that compact contract so
        # a short response cannot consume the long state-analysis budget.
        "temperature": 0.2,
        "connect_timeout": 5,
        "read_timeout": 20,
        "max_tokens": 600,
        "retries": 1,
        "env_prefix": "SERA_STRATEGY_REVIEW_AND_GENERATION",
        "env_aliases": ["SERA_STRATEGY_REVIEW"],
    },
}


# ---------------------------------------------------------------------------
# Unified result type
# ---------------------------------------------------------------------------
@dataclass
class LlmResult:
    """Unified result returned by every LLMGateway call."""

    success: bool
    output: Any  # parsed JSON dict or plain text string
    raw_text: Optional[str] = None
    model_name: str = ""
    profile_name: str = ""
    latency_ms: int = 0
    attempt_count: int = 1
    token_usage: Optional[dict] = None
    finish_reason: Optional[str] = None
    failure_type: Optional[str] = None
    failure_message: Optional[str] = None
    status_code: Optional[int] = None
    retryable: Optional[bool] = None
    fallback_required: bool = False
    gateway_retry_count: int = 0
    compatibility_fallback_count: int = 0
    final_content_only: bool = False

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "output": self.output,
            "raw_text": self.raw_text,
            "model_name": self.model_name,
            "profile_name": self.profile_name,
            "latency_ms": self.latency_ms,
            "attempt_count": self.attempt_count,
            "token_usage": self.token_usage,
            "finish_reason": self.finish_reason,
            "failure_type": self.failure_type,
            "failure_message": self.failure_message,
            "status_code": self.status_code,
            "retryable": self.retryable,
            "fallback_required": self.fallback_required,
            "gateway_retry_count": self.gateway_retry_count,
            "compatibility_fallback_count": self.compatibility_fallback_count,
            "final_content_only": self.final_content_only,
        }


def _classify_error(exc: Exception, status_code: Optional[int] = None) -> str:
    if status_code == 401 or status_code == 403:
        return AUTHENTICATION_ERROR
    if status_code == 429:
        return RATE_LIMITED
    if status_code in {502, 503, 504}:
        return UPSTREAM_5XX
    if isinstance(exc, httpx.ConnectTimeout):
        return CONNECT_TIMEOUT
    if isinstance(exc, httpx.ReadTimeout):
        return READ_TIMEOUT
    if isinstance(exc, httpx.TimeoutException):
        return CONNECT_TIMEOUT
    if isinstance(exc, httpx.NetworkError):
        return NETWORK_ERROR
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in NON_RETRYABLE_STATUS_CODES:
            return AUTHENTICATION_ERROR if code in (401, 403) else INVALID_RESPONSE
        if code in RETRYABLE_STATUS_CODES:
            return UPSTREAM_5XX
        return INVALID_RESPONSE
    if isinstance(exc, json.JSONDecodeError):
        return INVALID_RESPONSE
    return UNKNOWN_ERROR


def _should_retry(exc: Exception, status_code: Optional[int] = None, failure_type: Optional[str] = None) -> bool:
    """Return True if the error is considered retryable."""
    if failure_type in {
        CONNECT_TIMEOUT,
        READ_TIMEOUT,
        NETWORK_ERROR,
        RATE_LIMITED,
        UPSTREAM_5XX,
        TRUNCATED_RESPONSE,
    }:
        return True
    if failure_type == AUTHENTICATION_ERROR:
        return False
    if status_code is not None:
        if status_code in NON_RETRYABLE_STATUS_CODES:
            return False
        if status_code in RETRYABLE_STATUS_CODES:
            return True
    if isinstance(exc, (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.TimeoutException, httpx.NetworkError)):
        return True
    if isinstance(exc, json.JSONDecodeError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in NON_RETRYABLE_STATUS_CODES:
            return False
        if code in RETRYABLE_STATUS_CODES:
            return True
    return False


# ---------------------------------------------------------------------------
# Main Gateway
# ---------------------------------------------------------------------------
class LLMGateway:
    """Manages per-profile httpx.Clients and provides a unified `call` method.

    Usage::

        gateway = LLMGateway()
        result = gateway.call("state_detector", payload_dict)
        gateway.close()

    Or use as a context manager::

        with LLMGateway() as gw:
            result = gw.call("student_help", payload)
    """

    def __init__(
        self,
        profiles: Optional[dict[str, dict]] = None,
        max_connections: int = 20,
        max_keepalive: int = 10,
    ):
        self.profiles: dict[str, LlmProfile] = {}
        self.clients: dict[str, httpx.Client] = {}
        self._closed = False

        profile_defs = profiles or BUILTIN_PROFILES

        for name, cfg in profile_defs.items():
            profile = LlmProfile.from_env(name, env_prefix=cfg["env_prefix"], defaults=cfg)
            self.profiles[name] = profile
            print(f"[SERA DEBUG][gateway] Profile '{name}': model={profile.model}, has_key={bool(profile.api_key)}, base_url={profile.base_url[:50]}.., timeout_c={profile.connect_timeout}s, timeout_r={profile.read_timeout}s, retries={profile.retries}")

            limits = httpx.Limits(
                max_connections=max_connections,
                max_keepalive_connections=max_keepalive,
            )
            timeout = httpx.Timeout(
                connect=profile.connect_timeout,
                read=profile.read_timeout,
                write=30.0,
                pool=30.0,
            )

            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {profile.api_key}",
            }
            client = httpx.Client(
                base_url=profile.base_url,
                headers=headers,
                limits=limits,
                timeout=timeout,
            )
            self.clients[name] = client

    # ------------------------------------------------------------------
    # Core call with retry
    # ------------------------------------------------------------------
    def call(
        self,
        profile_name: str,
        payload: dict,
        response_type: str = "json",
        *,
        max_attempts_override: Optional[int] = None,
    ) -> LlmResult:
        print(f"[SERA DEBUG][gateway.call] ENTER: profile={profile_name}, response_type={response_type}, payload_model={payload.get('model', '?')}, msg_count={len(payload.get('messages',[]))}")
        profile = self.profiles.get(profile_name)
        if profile is None:
            print(f"[SERA DEBUG][gateway.call] ERROR: unknown profile {profile_name}")
            return LlmResult(
                success=False,
                output=None,
                failure_type=UNKNOWN_ERROR,
                failure_message=f"Unknown profile: {profile_name}",
                retryable=False,
                fallback_required=True,
            )

        client = self.clients.get(profile_name)
        if client is None:
            print(f"[SERA DEBUG][gateway.call] ERROR: no httpx client for profile {profile_name}")
            return LlmResult(
                success=False,
                output=None,
                model_name=profile.model,
                profile_name=profile_name,
                failure_type=UNKNOWN_ERROR,
                failure_message=f"No httpx.Client for profile: {profile_name}",
                retryable=False,
                fallback_required=True,
            )

        body = dict(payload)
        final_content_only = bool(body.pop("_sera_final_content_only", False))
        raw_compatibility_fields = body.pop(
            "_sera_compatibility_fallback_fields",
            (),
        )
        compatibility_fields = tuple(
            str(field)
            for field in (raw_compatibility_fields or ())
            if str(field)
        )
        raw_external_call_budget = body.pop("_sera_external_call_budget", None)
        body.setdefault("model", profile.model)
        body.setdefault("temperature", profile.temperature)
        body.setdefault("max_tokens", profile.max_tokens)
        print(f"[SERA DEBUG][gateway.call] prepared body: model={body.get('model')}, temperature={body.get('temperature')}, max_tokens={body.get('max_tokens')}, has_response_format={'response_format' in body}")

        if max_attempts_override is None:
            max_attempts = max(1, profile.retries + 1)
        else:
            max_attempts = max(1, int(max_attempts_override))
        total_attempt_budget = max_attempts + (1 if compatibility_fields else 0)
        if raw_external_call_budget is not None:
            try:
                total_attempt_budget = min(
                    total_attempt_budget,
                    max(1, int(raw_external_call_budget)),
                )
            except (TypeError, ValueError):
                pass

        last_result: Optional[LlmResult] = None
        http_attempt_count = 0
        gateway_retry_count = 0
        compatibility_fallback_count = 0

        while http_attempt_count < total_attempt_budget:
            http_attempt_count += 1
            if http_attempt_count > 1:
                print(
                    "[SERA DEBUG][gateway.call] HTTP attempt "
                    f"{http_attempt_count}/{total_attempt_budget}"
                )
            result = self._send_once(
                client,
                profile,
                body,
                response_type,
                http_attempt_count,
                final_content_only=final_content_only,
            )
            result.attempt_count = http_attempt_count
            result.gateway_retry_count = gateway_retry_count
            result.compatibility_fallback_count = compatibility_fallback_count
            result.final_content_only = final_content_only
            last_result = result

            if result.success:
                print(f"[SERA DEBUG][gateway.call] SUCCESS on attempt {http_attempt_count}: latency={result.latency_ms}ms, token_usage={result.token_usage}")
                return result

            print(f"[SERA DEBUG][gateway.call] attempt {http_attempt_count} failed: type={result.failure_type}, msg={result.failure_message}")

            rejected_fields = _unsupported_compatibility_fields(
                result,
                compatibility_fields,
            )
            if (
                rejected_fields
                and compatibility_fallback_count == 0
                and http_attempt_count < total_attempt_budget
            ):
                for field in rejected_fields:
                    body.pop(field, None)
                compatibility_fallback_count = 1
                result.compatibility_fallback_count = compatibility_fallback_count
                logger.info(
                    "LLM compatibility fallback for profile '%s': removed %s",
                    profile_name,
                    ",".join(rejected_fields),
                )
                continue

            retryable = result.retryable
            if retryable is None:
                retryable = _should_retry(
                    Exception(result.failure_message or ""),
                    result.status_code or self._status_code_from_failure(result.failure_type),
                    failure_type=result.failure_type,
                )
            if not retryable:
                print(f"[SERA DEBUG][gateway.call] non-retryable error, stopping")
                return result

            if (
                gateway_retry_count >= max_attempts - 1
                or http_attempt_count >= total_attempt_budget
            ):
                break

            gateway_retry_count += 1
            wait = 0.5 * (2 ** (gateway_retry_count - 1))
            print(f"[SERA DEBUG][gateway.call] will retry after {wait:.1f}s")
            logger.info(
                "LLM retry attempt %d/%d for profile '%s' after %.1fs",
                gateway_retry_count + 1,
                max_attempts,
                profile_name,
                wait,
            )
            time.sleep(wait)

        if last_result is not None:
            last_result.fallback_required = True
            last_result.gateway_retry_count = gateway_retry_count
            last_result.compatibility_fallback_count = compatibility_fallback_count
            print(f"[SERA DEBUG][gateway.call] All attempts exhausted, fallback_required=True")
            return last_result

        print(f"[SERA DEBUG][gateway.call] No result at all, returning fallback")
        return LlmResult(
            success=False,
            output=None,
            model_name=profile.model,
            profile_name=profile_name,
            failure_type=UNKNOWN_ERROR,
            retryable=False,
            fallback_required=True,
        )

    # ------------------------------------------------------------------
    # Single HTTP call
    # ------------------------------------------------------------------
    def _send_once(
        self,
        client: httpx.Client,
        profile: LlmProfile,
        body: dict,
        response_type: str,
        attempt: int,
        *,
        final_content_only: bool = False,
    ) -> LlmResult:
        start = time.perf_counter()
        latency_ms: int = 0
        raw_text: Optional[str] = None
        output: Any = None
        failure_type: Optional[str] = None
        failure_message: Optional[str] = None
        token_usage: Optional[dict] = None
        finish_reason: Optional[str] = None
        has_auth = bool(profile.api_key)
        url_preview = profile.base_url[:60]
        status_code: Optional[int] = None
        retryable: Optional[bool] = None
        print(f"[SERA DEBUG][gateway._send_once] POST {url_preview}.. attempt={attempt}, has_api_key={has_auth}, model={body.get('model')}")

        try:
            resp = client.post(profile.base_url, json=body)
            status_code = resp.status_code
            latency_ms = int((time.perf_counter() - start) * 1000)
            print(f"[SERA DEBUG][gateway._send_once] RESPONSE: status={resp.status_code}, latency={latency_ms}ms")

            resp.raise_for_status()
            raw_text = resp.text
            print(f"[SERA DEBUG][gateway._send_once] raw_text length={len(raw_text)}")

            try:
                parsed = resp.json()
                if isinstance(parsed, dict) and "usage" in parsed:
                    token_usage = parsed["usage"]
                    print(f"[SERA DEBUG][gateway._send_once] token_usage={token_usage}")
            except Exception:
                parsed = None

            # ---- diagnostic logging (env-controlled) ----
            if parsed is not None and isinstance(parsed, dict) and parsed.get("choices"):
                choice0 = parsed["choices"][0]
                finish_reason = choice0.get("finish_reason")
                msg = choice0.get("message", {})
                c_content = msg.get("content", "")
                usage = parsed.get("usage", {})
                _log_diag(
                    "response_received",
                    profile=profile.name,
                    model=body.get("model"),
                    max_tokens=body.get("max_tokens"),
                    temperature=body.get("temperature"),
                    has_response_format="response_format" in body,
                    http_status=200,
                    raw_text_len=len(raw_text) if raw_text else 0,
                    finish_reason=finish_reason,
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                    reasoning_tokens=usage.get("reasoning_tokens"),
                    content_len=len(c_content) if isinstance(c_content, str) else 0,
                    content_starts_with_brace=isinstance(c_content, str) and c_content.strip().startswith("{"),
                    content_ends_with_brace=isinstance(c_content, str) and c_content.strip().endswith("}"),
                )
            # ---- end diagnostic ----

            if (
                final_content_only
                and isinstance(parsed, dict)
                and parsed.get("choices")
            ):
                final_message = parsed["choices"][0].get("message", {})
                final_content = _flatten_message_content(
                    final_message.get("content", "")
                )
                if not final_content or not str(final_content).strip():
                    completion_tokens, reasoning_tokens = _usage_token_counts(
                        token_usage
                    )
                    reasoning_exhausted = bool(
                        completion_tokens > 0
                        and reasoning_tokens > 0
                        and reasoning_tokens == completion_tokens
                    )
                    return LlmResult(
                        success=False,
                        output=None,
                        raw_text=raw_text,
                        model_name=profile.model,
                        profile_name=profile.name,
                        latency_ms=latency_ms,
                        attempt_count=attempt,
                        token_usage=token_usage,
                        finish_reason=finish_reason,
                        failure_type=(
                            REASONING_BUDGET_EXHAUSTED
                            if reasoning_exhausted
                            else INVALID_RESPONSE
                        ),
                        failure_message=(
                            "final content empty after reasoning consumed the "
                            "completion budget"
                            if reasoning_exhausted
                            else "final content is empty"
                        ),
                        status_code=status_code,
                        retryable=False,
                        final_content_only=True,
                    )

            if response_type == "json":
                if parsed is None:
                    parsed = json.loads(raw_text)
                if isinstance(parsed, dict) and parsed.get("choices"):
                    finish_reason = parsed["choices"][0].get("finish_reason")
                    msg = parsed["choices"][0].get("message", {})
                    content = _flatten_message_content(msg.get("content", ""))
                    if (
                        not final_content_only
                        and (not content or not str(content).strip())
                    ):
                        reasoning = msg.get("reasoning_content", "")
                        if reasoning:
                            content = reasoning
                    if isinstance(content, dict):
                        output = content
                    else:
                        output = json.loads(_extract_json_text(content))
                else:
                    output = parsed
            else:
                if parsed is None:
                    parsed = json.loads(raw_text) if raw_text else {}
                if isinstance(parsed, dict) and parsed.get("choices"):
                    finish_reason = parsed["choices"][0].get("finish_reason")
                    msg = parsed["choices"][0].get("message", {})
                    content = _flatten_message_content(msg.get("content", ""))
                    if (
                        not final_content_only
                        and (not content or not str(content).strip())
                    ):
                        reasoning = msg.get("reasoning_content", "")
                        if reasoning:
                            content = reasoning
                    output = str(content)
                else:
                    output = str(parsed)

            return LlmResult(
                success=True,
                output=output,
                raw_text=raw_text,
                model_name=profile.model,
                profile_name=profile.name,
                latency_ms=latency_ms,
                attempt_count=attempt,
                token_usage=token_usage,
                finish_reason=finish_reason,
                status_code=status_code,
                retryable=False,
            )

        except httpx.TimeoutException as exc:
            latency_ms = int((time.perf_counter() - start) * 1000)
            failure_type = _classify_error(exc)
            failure_message = f"{exc.__class__.__name__}: {exc}"
            retryable = _should_retry(exc, failure_type=failure_type)
            print(f"[SERA DEBUG][gateway._send_once] TIMEOUT: type={failure_type}, msg={failure_message}")
            logger.warning("LLM timeout [%s] attempt %d: %s", profile.name, attempt, exc)
        except httpx.HTTPStatusError as exc:
            latency_ms = int((time.perf_counter() - start) * 1000)
            status_code = exc.response.status_code
            try:
                raw_text = exc.response.text
                print(f"[SERA DEBUG][gateway._send_once] HTTP ERROR body length={len(raw_text)}")
            except Exception:
                pass
            failure_type = _classify_error(exc, status_code)
            failure_message = f"HTTP {status_code}: {exc}"
            retryable = _should_retry(exc, status_code=status_code, failure_type=failure_type)
            print(f"[SERA DEBUG][gateway._send_once] HTTP ERROR: status={status_code}, type={failure_type}, msg={failure_message}")
            logger.warning("LLM HTTP error [%s] attempt %d: %s", profile.name, attempt, failure_message)
        except httpx.NetworkError as exc:
            latency_ms = int((time.perf_counter() - start) * 1000)
            failure_type = _classify_error(exc)
            failure_message = f"{exc.__class__.__name__}: {exc}"
            retryable = _should_retry(exc, failure_type=failure_type)
            print(f"[SERA DEBUG][gateway._send_once] NETWORK ERROR: type={failure_type}, msg={failure_message}")
            logger.warning("LLM network error [%s] attempt %d: %s", profile.name, attempt, exc)
        except json.JSONDecodeError as exc:
            latency_ms = int((time.perf_counter() - start) * 1000)
            # Differentiate truncation vs. other JSON decode errors
            _parsed_val = locals().get("parsed")
            if _detect_truncated_content(raw_text, _parsed_val):
                failure_type = TRUNCATED_RESPONSE
                failure_message = f"JSON decode error (content truncated): {exc}"
                retryable = True
                print(f"[SERA DEBUG][gateway._send_once] TRUNCATED JSON: {exc}")
                logger.warning("LLM truncated JSON response [%s] attempt %d: %s", profile.name, attempt, exc)
                _log_diag(
                    "truncated_response",
                    profile=profile.name,
                    model=body.get("model"),
                    max_tokens=body.get("max_tokens"),
                    finish_reason=_parsed_val.get("choices", [{}])[0].get("finish_reason") if isinstance(_parsed_val, dict) else None,
                    raw_text_len=len(raw_text) if raw_text else 0,
                    error_message=str(exc),
                )
            else:
                failure_type = _classify_error(exc)
                failure_message = f"JSON decode error: {exc}"
                retryable = True
                print(f"[SERA DEBUG][gateway._send_once] JSON DECODE ERROR: {exc}")
                if raw_text:
                    print(f"[SERA DEBUG][gateway._send_once] raw_text length={len(raw_text)}")
                logger.warning("LLM JSON decode error [%s] attempt %d: %s", profile.name, attempt, exc)
                _log_diag(
                    "json_decode_error",
                    profile=profile.name,
                    model=body.get("model"),
                    max_tokens=body.get("max_tokens"),
                    temperature=body.get("temperature"),
                    has_response_format="response_format" in body,
                    http_status=200,
                    raw_text_len=len(raw_text) if raw_text else 0,
                    error_message=str(exc),
                    error_pos=exc.pos if hasattr(exc, "pos") else None,
                )
        except Exception as exc:
            latency_ms = int((time.perf_counter() - start) * 1000)
            failure_type = _classify_error(exc)
            failure_message = f"{exc.__class__.__name__}: {exc}"
            retryable = _should_retry(exc, failure_type=failure_type)
            print(f"[SERA DEBUG][gateway._send_once] UNEXPECTED ERROR: {exc}")
            logger.error("LLM unexpected error [%s] attempt %d: %s", profile.name, attempt, exc, exc_info=True)

        return LlmResult(
            success=False,
            output=None,
            raw_text=raw_text,
            model_name=profile.model,
            profile_name=profile.name,
            latency_ms=latency_ms,
            attempt_count=attempt,
            token_usage=token_usage,
            finish_reason=finish_reason,
            failure_type=failure_type,
            failure_message=failure_message,
            status_code=status_code,
            retryable=retryable,
            final_content_only=final_content_only,
        )

    @staticmethod
    def _status_code_from_failure(failure_type: Optional[str]) -> Optional[int]:
        mapping = {
            AUTHENTICATION_ERROR: 401,
            RATE_LIMITED: 429,
            UPSTREAM_5XX: 502,
        }
        return mapping.get(failure_type) if failure_type else None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for name, client in self.clients.items():
            try:
                client.close()
            except Exception:
                logger.exception("Error closing LLM client for profile '%s'", name)

    def __enter__(self) -> "LLMGateway":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Module-level singleton helpers (for use across services)
# ---------------------------------------------------------------------------
_gateway: Optional[LLMGateway] = None


def get_gateway() -> LLMGateway:
    global _gateway
    if _gateway is None:
        _gateway = LLMGateway()
    return _gateway


def close_gateway() -> None:
    global _gateway
    if _gateway is not None:
        _gateway.close()
        _gateway = None


__all__ = [
    "LLMGateway",
    "LLMProfile",
    "LlmProfile",
    "LlmResult",
    "get_gateway",
    "close_gateway",
    "AUTHENTICATION_ERROR",
    "RATE_LIMITED",
    "CONNECT_TIMEOUT",
    "READ_TIMEOUT",
    "NETWORK_ERROR",
    "UPSTREAM_5XX",
    "INVALID_RESPONSE",
    "SCHEMA_VALIDATION_ERROR",
    "UNKNOWN_ERROR",
    "BUILTIN_PROFILES",
]
