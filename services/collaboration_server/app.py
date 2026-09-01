# -*- coding: utf-8 -*-
"""ASGI collaboration server (Batch 2 - teacher read-only WebSocket).

Provides:
- /health endpoint for health checks
- /ws/doc-<id> WebSocket endpoint for Yjs sync
- /internal/documents/<id>/<action> for managing rooms

Uses pycrdt-websocket for the Yjs protocol and ManagedRoom
for persistence and lifecycle management.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from typing import Optional

from pycrdt.websocket import YRoom, WebsocketServer
from pycrdt.websocket.asgi_server import ASGIWebsocket

from .auth import verify_ws_connection, check_origin, is_view_permission
from .room_manager import ManagedRoom, SNAPSHOT_INTERVAL, ROOM_RECYCLE_TIMEOUT
from .persistence import load_document_state, verify_document_status

logger = logging.getLogger(__name__)

# Global room registry: document_id -> ManagedRoom
rooms: dict[int, ManagedRoom] = {}

# Background task references
_background_tasks: set = set()


async def periodic_save_loop(ws_server: WebsocketServer):
    """Background task: periodically save dirty rooms and recycle stale ones."""
    while True:
        try:
            await asyncio.sleep(SNAPSHOT_INTERVAL)
            for doc_id, mroom in list(rooms.items()):
                if mroom.can_save():
                    mroom.perform_save()
                # Recycle stale rooms
                if mroom.is_stale():
                    logger.info("Recycling stale room for doc %s", doc_id)
                    await recycle_room(doc_id, ws_server)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Periodic save error: %s", e)


async def recycle_room(document_id: int, ws_server: WebsocketServer):
    """Force-flush and remove a room and its YRoom from the server."""
    mroom = rooms.get(document_id)
    if mroom is None:
        return
    # Force save before recycle
    if mroom.dirty and mroom.ydoc is not None:
        mroom.perform_save()
    # Remove from WebsocketServer
    room_path = f"/ws/doc-{document_id}"
    if room_path in ws_server.rooms:
        del ws_server.rooms[room_path]
    # Stop the YRoom
    try:
        if mroom.yroom is not None:
            await mroom.yroom.stop()
    except Exception as e:
        logger.error("Error stopping room %s: %s", document_id, e)
    # Remove from registry
    rooms.pop(document_id, None)
    logger.info("Recycled room for doc %s", document_id)


async def flush_all_dirty_rooms():
    """Flush all dirty rooms. Called on SIGTERM."""
    logger.info("Flushing all dirty rooms...")
    for doc_id, mroom in list(rooms.items()):
        if mroom.dirty and mroom.ydoc is not None:
            saved = mroom.perform_save()
            if saved:
                logger.info("Flushed doc %s (rev %s)", doc_id, mroom.state_revision)
    logger.info("Flush complete")


def check_internal_secret(scope) -> bool:
    """Verify the X-Internal-Secret header."""
    headers = dict(scope.get("headers", []))
    secret_header = headers.get(b"x-internal-secret", b"").decode("utf-8", errors="replace")
    expected = os.environ.get("COLLAB_INTERNAL_SECRET", "")
    if not expected:
        logger.warning("COLLAB_INTERNAL_SECRET not set on server")
        return False
    return secret_header == expected


async def handle_internal_request(scope, receive, send, document_id: int, action: str) -> bool:
    """Handle internal management requests (freeze, flush, unfreeze, close, status)."""
    if not check_internal_secret(scope):
        body = json.dumps({"ok": False, "error": "unauthorized"}).encode("utf-8")
        await send({"type": "http.response.start", "status": 403, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
        return True

    mroom = rooms.get(document_id)

    if action == "freeze":
        if mroom is None:
            body = json.dumps({"ok": False, "error": "no_room", "message": "Room not active"}).encode("utf-8")
        else:
            mroom.set_frozen(True)
            body = json.dumps({"ok": True, "state": "frozen"}).encode("utf-8")
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
        return True
    elif action == "flush":
        if mroom is None:
            body = json.dumps({"ok": False, "error": "no_room", "message": "Room not active"}).encode("utf-8")
        else:
            result = mroom.perform_flush()
            if result:
                body = json.dumps({"ok": True, "state_revision": result["state_revision"], "state_size_bytes": result["state_size_bytes"]}).encode("utf-8")
            else:
                body = json.dumps({"ok": False, "error": "flush_failed"}).encode("utf-8")
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
        return True
    elif action == "unfreeze":
        if mroom is None:
            body = json.dumps({"ok": False, "error": "no_room", "message": "Room not active"}).encode("utf-8")
        else:
            mroom.set_frozen(False)
            body = json.dumps({"ok": True, "state": "unfrozen"}).encode("utf-8")
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
        return True
    elif action == "close":
        if mroom is None:
            body = json.dumps({"ok": True, "state": "already_closed"}).encode("utf-8")
        else:
            mroom.schedule_close()
            rooms.pop(document_id, None)
            body = json.dumps({"ok": True, "state": "closed"}).encode("utf-8")
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
        return True
    elif action == "status":
        if mroom is None:
            body = json.dumps({"ok": True, "active": False}).encode("utf-8")
        else:
            body = json.dumps({
                "ok": True,
                "active": True,
                "document_id": document_id,
                "frozen": mroom.frozen,
                "dirty": mroom.dirty,
                "connection_count": len(mroom.yroom.clients) if mroom.yroom else 0,
                "state_revision": mroom.state_revision,
            }).encode("utf-8")
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
        return True
    else:
        body = json.dumps({"ok": False, "error": "unknown_action"}).encode("utf-8")
        await send({"type": "http.response.start", "status": 404, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
        return True


async def handle_http(scope, receive, send) -> bool:
    """Handle HTTP connections (health, internal API)."""
    path = scope.get("path", "")

    # Internal management endpoints
    if path.startswith("/internal/documents/"):
        parts = path.split("/")
        if len(parts) >= 4:
            try:
                doc_id = int(parts[3])
                action = parts[4] if len(parts) >= 5 else ""
                return await handle_internal_request(scope, receive, send, doc_id, action)
            except (ValueError, IndexError):
                body = json.dumps({"ok": False, "error": "invalid_path"}).encode("utf-8")
                await send({"type": "http.response.start", "status": 400, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return True

    if path == "/monitor/db-stats":
        # Database monitoring endpoint (read-only)
        import sqlite3 as _sql3
        db_path = os.environ.get("SSRL_ESP_DB_PATH",
                                 os.path.join(os.path.dirname(__file__), "..", "ssrl_esp.db"))
        stats = {"rooms": {}}
        # File sizes
        for fpath in [db_path, db_path + "-wal", db_path + "-shm"]:
            fname = os.path.basename(fpath)
            try:
                stats[fname] = os.path.getsize(fpath)
            except OSError:
                stats[fname] = None
        # Per-room stats
        for doc_id, mroom in rooms.items():
            stats["rooms"][str(doc_id)] = {
                "dirty": mroom.dirty,
                "frozen": mroom.frozen,
                "state_revision": mroom.state_revision,
                "connections": len(mroom.yroom.clients) if mroom.yroom else 0,
                "save_count": getattr(mroom, "_save_count", 0),
                "idle_save_count": getattr(mroom, "_idle_save_count", 0),
                "last_saved_hash": getattr(mroom, "_last_saved_hash", "")[:12] + "..." if getattr(mroom, "_last_saved_hash", "") else None,
            }
        # DB table stats
        try:
            _conn = _sql3.connect(db_path, timeout=3)
            _conn.row_factory = _sql3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            stats["page_count"] = _conn.execute("PRAGMA page_count").fetchone()[0]
            stats["page_size"] = _conn.execute("PRAGMA page_size").fetchone()[0]
            stats["freelist_count"] = _conn.execute("PRAGMA freelist_count").fetchone()[0]
            stats["doc_count"] = _conn.execute("SELECT COUNT(*) FROM collaborative_documents").fetchone()[0]
            stats["checkpoint_count"] = _conn.execute("SELECT COUNT(*) FROM collaborative_document_checkpoints").fetchone()[0]
            stats["total_y_state_bytes"] = _conn.execute("SELECT COALESCE(SUM(LENGTH(y_state)),0) FROM collaborative_documents").fetchone()[0]
            stats["max_y_state_bytes"] = _conn.execute("SELECT COALESCE(MAX(LENGTH(y_state)),0) FROM collaborative_documents").fetchone()[0]
            stats["total_revision"] = _conn.execute("SELECT COALESCE(SUM(state_revision),0) FROM collaborative_documents").fetchone()[0]
            _conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            _conn.close()
        except Exception as _e:
            stats["db_error"] = str(_e)
        body = json.dumps({"ok": True, "stats": stats}).encode("utf-8")
        await send({
            "type": "http.response.start", "status": 200,
            "headers": [(b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode())],
        })
        await send({"type": "http.response.body", "body": body})
        return True

    if path == "/health":
        body = json.dumps({
            "status": "ok",
            "rooms": len(rooms),
            "connections": sum(len(m.yroom.clients) for m in rooms.values() if m.yroom),
            "total_save_count": sum(getattr(m, "_save_count", 0) for m in rooms.values()),
            "total_idle_save_count": sum(getattr(m, "_idle_save_count", 0) for m in rooms.values()),
        }).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({
            "type": "http.response.body",
            "body": body,
        })
        return True
    # 404 for unknown HTTP paths
    await send({
        "type": "http.response.start",
        "status": 404,
        "headers": [(b"content-type", b"text/plain")],
    })
    await send({"type": "http.response.body", "body": b"Not Found"})
    return True


async def create_managed_room(document_id: int) -> Optional[ManagedRoom]:
    """Create a ManagedRoom, loading y_state from DB.

    Args:
        document_id: The collaborative_document ID.

    Returns:
        ManagedRoom instance, or None on failure.
    """
    try:
        state_info = load_document_state(document_id)
        if state_info is None:
            logger.error("Document %s not found in DB", document_id)
            return None

        mroom = ManagedRoom(document_id, state_info.get("state_revision", 0))
        mroom.frozen = state_info.get("status", "editing") in ("submitted", "locked")

        # Load y_state into Y.Doc
        ydoc = mroom.load_y_state(state_info.get("y_state"))
        mroom.ydoc = ydoc

        # Create YRoom
        yroom = mroom.build_yroom(ydoc)
        mroom.yroom = yroom

        rooms[document_id] = mroom
        logger.info("Created room for doc %s (rev %s, %s bytes, status %s)",
                    document_id, mroom.state_revision,
                    state_info.get("state_size_bytes", 0),
                    state_info.get("status", "unknown"))
        return mroom
    except Exception as e:
        logger.error("Failed to create room for doc %s: %s", document_id, e)
        return None


def exception_logger(exception, log):
    """Exception handler that logs and discards."""
    log.error("Collaboration server exception", exc_info=exception)
    return True  # handled


class CollaborationApp:
    """Combined ASGI app for both HTTP and WebSocket (Batch 2).

    Handles WebSocket connections with permission-aware routing:
    - edit permission: delegates to WebsocketServer.serve() for full Yjs sync
    - view permission: uses custom _serve_view() that blocks document writes
    All unauthorized connections are rejected with appropriate close codes.
    """

    def __init__(self):
        self.ws_server = WebsocketServer(
            rooms_ready=True,
            auto_clean_rooms=False,
            exception_handler=exception_logger,
        )
        self._started = False

    async def _handle_websocket(self, scope, receive, send):
        """Handle WebSocket connection with permission-aware routing."""
        msg = await receive()
        if msg.get("type") != "websocket.connect":
            return

        path = scope.get("path", "")

        doc_id = None
        if path.startswith("/ws/doc-"):
            try:
                doc_id = int(path[len("/ws/doc-"):])
            except (ValueError, IndexError):
                pass

        if doc_id is None:
            await send({"type": "websocket.close", "code": 4000})
            return

        if not check_origin(scope):
            await send({"type": "websocket.close", "code": 4000})
            return

        payload = verify_ws_connection(scope)
        if payload is None:
            logger.warning("Rejected WS: bad ticket for doc %s", doc_id)
            await send({"type": "websocket.close", "code": 4000})
            return

        status_info = verify_document_status(doc_id)
        if status_info is None:
            logger.warning("Rejected WS: doc %s not found", doc_id)
            await send({"type": "websocket.close", "code": 4004})
            return

        ticket_group_id = payload.get("group_id")
        db_group_id = status_info["group_id"]
        if ticket_group_id != db_group_id:
            logger.warning("Rejected WS: group mismatch for doc %s", doc_id)
            await send({"type": "websocket.close", "code": 4003})
            return

        permission = payload.get("permission", "")
        if permission not in ("edit", "view"):
            logger.warning("Rejected WS: unknown permission %s for doc %s", permission, doc_id)
            await send({"type": "websocket.close", "code": 4003})
            return

        doc_status = status_info["status"]
        if doc_status in ("submitted", "locked"):
            if permission == "edit":
                logger.info("Rejected WS: doc %s is %s, blocking edit", doc_id, doc_status)
                if os.environ.get("COLLAB_DIAG") == "1":
                    logger.info("[collab-diagnosis] submitted/locked rejected: doc=%s status=%s", doc_id, doc_status)
                await send({"type": "websocket.close", "code": 4003})
                return
            logger.info("View connection to %s doc %s allowed", doc_status, doc_id)
            if os.environ.get("COLLAB_DIAG") == "1":
                logger.info("[collab-diagnosis] view connection accepted to %s doc=%s", doc_status, doc_id)

        mroom = rooms.get(doc_id)
        if mroom is None:
            mroom = await create_managed_room(doc_id)
            if mroom is None:
                await send({"type": "websocket.close", "code": 1011})
                return

        room_path = f"/ws/doc-{doc_id}"
        if room_path not in self.ws_server.rooms:
            self.ws_server.rooms[room_path] = mroom.yroom

        await send({"type": "websocket.accept"})
        logger.info("[collab-ws] accepted: doc=%s user=%s perm=%s", doc_id, payload.get("user_id"), permission)
        if os.environ.get("COLLAB_DIAG") == "1":
            logger.info("[collab-diagnosis] accepted: doc=%s user=%s perm=%s", doc_id, payload.get("user_id"), permission)
        websocket = ASGIWebsocket(receive, send, path)

        mroom.last_active_at = asyncio.get_event_loop().time()
        logger.info("WS accepted: doc=%s, user=%s, permission=%s",
                    doc_id, payload.get("user_id"), permission)
        if os.environ.get("COLLAB_DIAG") == "1":
            logger.info("[collab-diagnosis] accepted: doc=%s, user=%s, permission=%s", doc_id, payload.get("user_id"), permission)

        if permission == "edit":
            await self.ws_server.serve(websocket)
        else:
            await self._serve_view(mroom, websocket)

    async def _serve_view(self, mroom, channel):
        """Serve a view-only WebSocket connection.

        View client syncs via YRoom broadcast mechanism but all
        document update messages from the client are blocked.
        """
        from pycrdt import (
            YMessageType, YSyncMessageType,
            create_sync_message, handle_sync_message,
            is_awareness_disconnect_message,
        )

        yroom = mroom.yroom
        ydoc = mroom.ydoc

        yroom.clients.add(channel)

        sync_message = create_sync_message(ydoc)
        await channel.send(sync_message)

        try:
            async for message in channel:
                if not message:
                    continue
                msg_type = message[0]
                if msg_type == YMessageType.SYNC:
                    if len(message) > 1 and message[1] == YSyncMessageType.SYNC_STEP1:
                        reply = handle_sync_message(message[1:], ydoc)
                        if reply is not None:
                            await channel.send(reply)
                elif msg_type == YMessageType.AWARENESS:
                    disconnection = is_awareness_disconnect_message(message[1:])
                    for client in list(yroom.clients):
                        if disconnection and client is channel:
                            continue
                        try:
                            await client.send(message)
                        except Exception:
                            pass
        except Exception as e:
            logger.warning("View WS disconnected for doc %s: %s", mroom.document_id, e)
        finally:
            yroom.clients.discard(channel)
            logger.info("[collab-ws] view-disconnected: doc=%s", mroom.document_id)
            logger.info("View WS cleaned up for doc %s", mroom.document_id)

    async def __call__(self, scope, receive, send):
        """ASGI application callable."""
        if not self._started:
            self._started = True
            _background_tasks.add(asyncio.create_task(self.ws_server.start()))
            task = asyncio.create_task(periodic_save_loop(self.ws_server))
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)

        scope_type = scope.get("type", "")
        if scope_type == "http":
            await handle_http(scope, receive, send)
        elif scope_type == "websocket":
            await self._handle_websocket(scope, receive, send)
        elif scope_type == "lifespan":
            await self._handle_lifespan(scope, receive, send)

    async def _handle_lifespan(self, scope, receive, send):
        """Handle ASGI lifespan events."""
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                logger.info("Collaboration server starting up")
                # [collab-diagnosis] ASGI startup diagnostics
                import hashlib as _h_cd
                import os as _os_cd
                if _os_cd.environ.get("COLLAB_DIAG") == "1":
                    _s = _os_cd.environ.get("SSRL_ESP_SECRET", "")
                    _fp = _h_cd.sha256(_s.encode()).hexdigest()[:8] if _s else "NOT_SET"
                    logger.info("[collab-diagnosis] SSRL_ESP_SECRET fingerprint: %s", _fp)
                    logger.info("[collab-diagnosis] COLLAB_WS_ALLOWED_ORIGINS: %s", _os_cd.environ.get("COLLAB_WS_ALLOWED_ORIGINS", ""))
                    logger.info("[collab-diagnosis] COLLAB_TOKEN_TTL: %s", _os_cd.environ.get("COLLAB_TOKEN_TTL", "300"))
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                logger.info("Collaboration server shutting down")
                await flush_all_dirty_rooms()
                await send({"type": "lifespan.shutdown.complete"})
                return





# App instance for uvicorn
app = CollaborationApp()
