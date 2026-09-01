# -*- coding: utf-8 -*-
"""Canonical discussion scope resolution for new state-chain records.

The resolver deliberately keeps legacy rows unresolved unless a caller
explicitly enables legacy fallback.  In particular, ``session_no=1`` is never
silently treated as ``session_id=1``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any, Optional


def _as_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class DiscussionScope:
    group_id: int
    session_id: Optional[int] = None
    session_no: Optional[int] = None
    task_id: Optional[int] = None
    discussion_id: Optional[int] = None
    resolved_from: str = "unresolved"
    is_legacy_fallback: bool = False
    fallback_reason: Optional[str] = None

    @property
    def is_complete(self) -> bool:
        return all(
            value is not None
            for value in (
                self.group_id,
                self.session_id,
                self.session_no,
                self.task_id,
                self.discussion_id,
            )
        )

    def as_dict(self) -> dict:
        return asdict(self)


def _scope_from_joined_row(
    row,
    *,
    group_id: int,
    resolved_from: str,
    legacy: bool = False,
    fallback_reason: str = None,
) -> DiscussionScope:
    data = dict(row or {})
    return DiscussionScope(
        group_id=int(group_id),
        session_id=_as_int(data.get("session_id")),
        session_no=_as_int(data.get("session_no")),
        task_id=_as_int(data.get("task_id")),
        discussion_id=_as_int(data.get("discussion_id")),
        resolved_from=resolved_from,
        is_legacy_fallback=bool(legacy),
        fallback_reason=fallback_reason,
    )


def _session_scope(
    conn,
    *,
    group_id: int,
    session_id: int,
    discussion_id: int = None,
    resolved_from: str = "session",
) -> Optional[DiscussionScope]:
    params = [group_id, session_id]
    discussion_clause = ""
    if discussion_id is not None:
        discussion_clause = " AND gsd.id=?"
        params.append(discussion_id)
    row = conn.execute(
        f"""
        SELECT es.id AS session_id, es.session_no, es.task_id,
               gsd.id AS discussion_id
        FROM experiment_sessions AS es
        LEFT JOIN group_session_discussions AS gsd
          ON gsd.session_id=es.id AND gsd.group_id=?
        WHERE es.id=?{discussion_clause}
        ORDER BY CASE gsd.status WHEN 'running' THEN 0 WHEN 'waiting' THEN 1 ELSE 2 END,
                 gsd.id DESC
        LIMIT 1
        """,
        tuple(params),
    ).fetchone()
    if not row:
        return None
    return _scope_from_joined_row(
        row,
        group_id=group_id,
        resolved_from=resolved_from,
    )


def resolve_discussion_scope(
    conn,
    *,
    group_id: int,
    message_id: int = None,
    sequence: int = None,
    session_id: int = None,
    session_no: int = None,
    task_id: int = None,
    discussion_id: int = None,
    allow_legacy_fallback: bool = False,
) -> DiscussionScope:
    """Resolve one deterministic scope using message → session → runtime.

    ``legacy_settings`` is consulted only when ``allow_legacy_fallback=True``;
    legacy resolution is always marked and never mutates old rows.
    """
    group_id = _as_int(group_id)
    if group_id is None:
        raise ValueError("invalid_group_id")
    message_id = _as_int(message_id)
    sequence = _as_int(sequence)
    session_id = _as_int(session_id)
    session_no = _as_int(session_no)
    task_id = _as_int(task_id)
    discussion_id = _as_int(discussion_id)

    if message_id is not None or sequence is not None:
        clauses = ["m.group_id=?"]
        params = [group_id]
        if message_id is not None:
            clauses.append("m.id=?")
            params.append(message_id)
        else:
            clauses.append("m.sequence=?")
            params.append(sequence)
        row = conn.execute(
            f"""
            SELECT m.session_id, m.session_no, m.task_id, m.discussion_id,
                   gsd.id AS runtime_discussion_id,
                   es.session_no AS canonical_session_no,
                   es.task_id AS canonical_task_id
            FROM messages AS m
            LEFT JOIN experiment_sessions AS es ON es.id=m.session_id
            LEFT JOIN group_session_discussions AS gsd
              ON gsd.session_id=m.session_id AND gsd.group_id=m.group_id
            WHERE {' AND '.join(clauses)}
            ORDER BY CASE gsd.status WHEN 'running' THEN 0 WHEN 'waiting' THEN 1 ELSE 2 END,
                     gsd.id DESC
            LIMIT 1
            """,
            tuple(params),
        ).fetchone()
        if row:
            data = dict(row)
            data["discussion_id"] = (
                data.get("discussion_id") or data.get("runtime_discussion_id")
            )
            data["session_no"] = (
                data.get("canonical_session_no") or data.get("session_no")
            )
            data["task_id"] = data.get("canonical_task_id") or data.get("task_id")
            message_scope = _scope_from_joined_row(
                data,
                group_id=group_id,
                resolved_from="message",
            )
            if message_scope.is_complete:
                return message_scope
            missing = [
                name
                for name in (
                    "session_id",
                    "session_no",
                    "task_id",
                    "discussion_id",
                )
                if getattr(message_scope, name) is None
            ]
            reason = "legacy_message_missing_scope:" + ",".join(missing)
            if allow_legacy_fallback:
                recovered = resolve_discussion_scope(
                    conn,
                    group_id=group_id,
                    session_id=message_scope.session_id,
                    session_no=message_scope.session_no,
                    task_id=message_scope.task_id,
                    discussion_id=message_scope.discussion_id,
                    allow_legacy_fallback=True,
                )
                if recovered.session_id is not None:
                    return replace(
                        recovered,
                        is_legacy_fallback=True,
                        fallback_reason=reason,
                    )
            return replace(
                message_scope,
                is_legacy_fallback=True,
                fallback_reason=reason,
            )

    if discussion_id is not None:
        row = conn.execute(
            """
            SELECT gsd.session_id, es.session_no, es.task_id,
                   gsd.id AS discussion_id
            FROM group_session_discussions AS gsd
            JOIN experiment_sessions AS es ON es.id=gsd.session_id
            WHERE gsd.id=? AND gsd.group_id=?
            """,
            (discussion_id, group_id),
        ).fetchone()
        if row:
            scope = _scope_from_joined_row(
                row,
                group_id=group_id,
                resolved_from="session",
            )
            if session_id is not None and scope.session_id != session_id:
                raise ValueError("discussion_scope_mismatch")
            return scope

    if session_id is not None:
        scope = _session_scope(
            conn,
            group_id=group_id,
            session_id=session_id,
            discussion_id=discussion_id,
            resolved_from="session",
        )
        if scope:
            return scope

    if session_no is not None or task_id is not None:
        clauses = []
        params = []
        if session_no is not None:
            clauses.append("es.session_no=?")
            params.append(session_no)
        if task_id is not None:
            clauses.append("es.task_id=?")
            params.append(task_id)
        rows = conn.execute(
            f"""
            SELECT es.id AS session_id, es.session_no, es.task_id,
                   gsd.id AS discussion_id
            FROM experiment_sessions AS es
            LEFT JOIN group_session_discussions AS gsd
              ON gsd.session_id=es.id AND gsd.group_id=?
            WHERE {' AND '.join(clauses)}
            ORDER BY CASE es.status WHEN 'running' THEN 0 ELSE 1 END,
                     CASE gsd.status WHEN 'running' THEN 0 WHEN 'waiting' THEN 1 ELSE 2 END,
                     es.id DESC, gsd.id DESC
            """,
            (group_id, *params),
        ).fetchall()
        session_ids = {int(row["session_id"]) for row in rows}
        if len(session_ids) == 1 and rows:
            return _scope_from_joined_row(
                rows[0],
                group_id=group_id,
                resolved_from="session",
            )

    row = conn.execute(
        """
        SELECT es.id AS session_id, es.session_no, es.task_id,
               gsd.id AS discussion_id
        FROM group_session_discussions AS gsd
        JOIN experiment_sessions AS es ON es.id=gsd.session_id
        WHERE gsd.group_id=?
          AND gsd.status IN ('running','waiting')
          AND es.status='running'
        ORDER BY CASE gsd.status WHEN 'running' THEN 0 ELSE 1 END,
                 gsd.id DESC
        LIMIT 1
        """,
        (group_id,),
    ).fetchone()
    if row:
        return _scope_from_joined_row(
            row,
            group_id=group_id,
            resolved_from="runtime",
        )

    row = conn.execute(
        """
        SELECT es.id AS session_id, es.session_no, es.task_id,
               gsd.id AS discussion_id
        FROM experiment_sessions AS es
        LEFT JOIN group_session_discussions AS gsd
          ON gsd.session_id=es.id AND gsd.group_id=?
        WHERE es.status='running'
        ORDER BY es.id DESC, gsd.id DESC
        LIMIT 1
        """,
        (group_id,),
    ).fetchone()
    if row:
        return _scope_from_joined_row(
            row,
            group_id=group_id,
            resolved_from="runtime",
        )

    if allow_legacy_fallback:
        settings = {
            row["key"]: row["value"]
            for row in conn.execute(
                """
                SELECT key, value FROM settings
                WHERE key IN ('current_session_id','current_session_no','current_task_id')
                """
            ).fetchall()
        }
        legacy_session_id = _as_int(settings.get("current_session_id"))
        if legacy_session_id is not None:
            scope = _session_scope(
                conn,
                group_id=group_id,
                session_id=legacy_session_id,
                resolved_from="legacy_settings",
            )
            if scope:
                return DiscussionScope(
                    **{
                        **scope.as_dict(),
                        "resolved_from": "legacy_settings",
                        "is_legacy_fallback": True,
                        "fallback_reason": "explicit_legacy_settings_enabled",
                    }
                )
        return DiscussionScope(
            group_id=group_id,
            session_no=_as_int(settings.get("current_session_no")),
            task_id=_as_int(settings.get("current_task_id")),
            resolved_from="legacy_settings",
            is_legacy_fallback=True,
            fallback_reason="legacy_settings_missing_canonical_session",
        )

    return DiscussionScope(
        group_id=group_id,
        session_id=session_id,
        session_no=session_no,
        task_id=task_id,
        discussion_id=discussion_id,
        resolved_from="unresolved",
        fallback_reason="canonical_scope_not_found",
    )


def legacy_scope_metadata(row: Any) -> dict:
    """Return explicit compatibility metadata for an old/incomplete row."""
    data = dict(row or {})
    missing = [
        name
        for name in ("session_id", "session_no", "task_id", "discussion_id")
        if data.get(name) is None
    ]
    legacy = bool(data.get("legacy_scope_fallback")) or bool(missing)
    reason = data.get("scope_fallback_reason")
    if legacy and not reason:
        reason = "missing_scope_fields:" + ",".join(missing)
    return {
        "legacy_scope_fallback": legacy,
        "fallback_reason": reason,
        "scope_resolved_from": data.get("scope_resolved_from")
        or ("legacy_row" if legacy else "persisted"),
    }
