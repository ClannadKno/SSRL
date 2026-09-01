# -*- coding: utf-8 -*-
"""Deterministic lookup hashes for experiment login keys."""
import hmac
from hashlib import sha256


def _server_secret():
    from config import SSRL_ESP_SECRET

    secret = (SSRL_ESP_SECRET or "").strip()
    if secret:
        return secret

    from services.collaboration_secret import ensure_collab_secret

    return ensure_collab_secret()


def compute_login_key_lookup_hash(raw_login_key):
    """Return an HMAC used only to locate a candidate key row."""
    login_key = (raw_login_key or "").strip()
    if not login_key:
        return ""
    return hmac.new(
        _server_secret().encode("utf-8"),
        login_key.encode("utf-8"),
        sha256,
    ).hexdigest()
