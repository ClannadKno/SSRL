# -*- coding: utf-8 *-
"""Flask application bootstrap for SSRL-ESP."""
import re
import logging

from flask import Flask, jsonify
from werkzeug.exceptions import HTTPException

from config import APP_DEBUG, MAX_SUBMISSION_FILE_BYTES
from services.logging_setup import configure_utf8_logging

configure_utf8_logging()

logger = logging.getLogger(__name__)


def _resolve_secret_key():
    from services.collaboration_secret import ensure_collab_secret
    return ensure_collab_secret()


app = Flask(__name__)
app.config.update(
    SECRET_KEY=_resolve_secret_key(),
    JSON_AS_ASCII=False,
    MAX_CONTENT_LENGTH=MAX_SUBMISSION_FILE_BYTES,
    TEMPLATES_AUTO_RELOAD=APP_DEBUG,
)

# Security hardening: error sanitization

def _sanitize_error_message(msg):
    """Remove sensitive paths and internal details."""
    if not msg:
        return msg
    msg = re.sub(r'[A-Za-z]:[\\/][^\s<>"\'\\]+', "[PATH]", msg)
    msg = re.sub(r'/[^\s<>"\']+', "[PATH]", msg)
    msg = re.sub(r'[A-Za-z0-9_-]{20,}(?:\.[A-Za-z0-9_-]{2,}){2,}', "[TOKEN]", msg)
    msg = re.sub(r'[\w\.-]+@[\w\.-]+\.[A-Za-z]{2,}', "[EMAIL]", msg)
    return msg


@app.errorhandler(400)
@app.errorhandler(401)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(405)
@app.errorhandler(413)
@app.errorhandler(422)
@app.errorhandler(429)
@app.errorhandler(500)
def _handle_http_errors(exc):
    code = getattr(exc, "code", 500)
    if isinstance(exc, HTTPException):
        description = exc.description or str(exc)
    else:
        description = str(exc)
    sanitized = _sanitize_error_message(description)
    if len(sanitized) > 200:
        sanitized = sanitized[:200] + "..."
    if code >= 500:
        logger.error("Server error (sanitized): %s", sanitized)
    return jsonify({"error": sanitized}), code


@app.errorhandler(Exception)
def _handle_unhandled_exceptions(exc):
    sanitized = _sanitize_error_message(str(exc))
    if len(sanitized) > 200:
        sanitized = sanitized[:200] + "..."
    logger.error("Unhandled exception (sanitized): %s", sanitized)
    return jsonify({"error": "internal server error"}), 500


app.config["TRAP_HTTP_EXCEPTIONS"] = False
