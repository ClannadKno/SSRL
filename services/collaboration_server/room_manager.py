# -*- coding: utf-8 -*-
"""Room manager for collaboration server.

Manages ManagedRoom lifecycle: create, load, save, recycle.
"""
from __future__ import annotations

import time
import logging
import hashlib
from typing import Optional

from pycrdt import Doc
from pycrdt.websocket import YRoom
from ._yroom_fix import yroom_exception_handler

from .persistence import load_document_state, save_document_state

logger = logging.getLogger(__name__)

# Max document state size (1 MB)
MAX_DOC_SIZE_BYTES = 1048576

# Room recycling timeout (30 seconds after last activity when no clients)
ROOM_RECYCLE_TIMEOUT = 30.0

# Snapshot debounce interval (10 seconds)
SNAPSHOT_INTERVAL = 10.0


class ManagedRoom:
    """Wraps a YRoom with document metadata and persistence.

    Attributes:
        document_id: The collaborative_document ID.
        yroom: The underlying YRoom instance.
        ydoc: The Y.Doc inside the room.
        state_revision: Current revision number.
        dirty: True if there are unsaved changes.
        frozen: True if document is submitted/locked (read-only).
        last_active_at: Timestamp of last activity.
    """

    def __init__(self, document_id: int, state_revision: int = 0):
        self.document_id = document_id
        self.yroom: Optional[YRoom] = None
        self.ydoc: Optional[Doc] = None
        self.state_revision = state_revision
        self.dirty = False
        self.frozen = False
        self.last_active_at = time.time()
        self._last_save_attempt = 0.0
        self._last_saved_hash = ""
        self._save_count = 0
        self._idle_save_count = 0

    def create_empty_doc(self) -> Doc:
        """Create an empty Y.Doc (no pre-initialized types).

        CRITICAL: Do NOT pre-create a Y.Text or any other type at the 'content' field.
        The client (TipTap @tiptap/extension-collaboration) uses Y.XmlFragment as the
        default ProseMirror document type. Pre-creating Y.Text causes a type mismatch
        during Yjs sync, breaking collaborative editing while still allowing awareness
        (cursor/online info) to work.
        """
        return Doc()

    def load_y_state(self, y_state_bytes: Optional[bytes]) -> Doc:
        """Create a Y.Doc and apply stored state if available.

        Validates input size to prevent OOM from corrupt data.
        Catches BaseException to handle Rust PanicException from pycrdt.

        Args:
            y_state_bytes: The stored y_state BLOB, or None for empty doc.

        Returns:
            A Y.Doc with loaded state.
        """
        MAX_SAFE_Y_STATE = 10485760  # 10MB hard limit
        if y_state_bytes and len(y_state_bytes) > MAX_SAFE_Y_STATE:
            logger.error("y_state too large for doc %s: %s bytes (max %s)",
                         self.document_id, len(y_state_bytes), MAX_SAFE_Y_STATE)
            y_state_bytes = None
        doc = Doc()
        if y_state_bytes:
            try:
                from pycrdt import XmlFragment
                doc["content"] = XmlFragment()
                doc.apply_update(bytes(y_state_bytes))
                if doc["content"] is None:
                    doc = Doc()
            except BaseException as e:
                logger.error("Failed to apply y_state for doc %s: %s", self.document_id, e)
                doc = Doc()
        return doc

    def get_y_state_bytes(self) -> bytes:
        """Get the full Y.Doc state as binary bytes."""
        if self.ydoc is None:
            return b""
        return self.ydoc.get_update()

    def build_yroom(self, ydoc: Doc) -> YRoom:
        """Create a YRoom with the given Y.Doc.

        Sets up on_message callback for message filtering and dirty tracking.
        """
        room = YRoom(ready=True, ydoc=ydoc, exception_handler=yroom_exception_handler)

        # Wire on_message handler (blocks writes when frozen)
        room.on_message = self._on_ws_message

        # Track updates via ydoc observer
        ydoc.observe(self._on_doc_update)

        return room

    def _on_doc_update(self, event):
        """Called when the Y.Doc is updated. Marks room as dirty."""
        self.dirty = True
        self.last_active_at = time.time()

    def perform_flush(self):
        """Force-save unconditionally and return state info."""
        if self.ydoc is None:
            return None
        self._last_save_attempt = time.time()
        self._save_count += 1
        try:
            state = self.get_y_state_bytes()
            import hashlib
            current_hash = hashlib.sha256(state).hexdigest() if state else ""
            if current_hash and current_hash == self._last_saved_hash:
                self.dirty = False
                self._idle_save_count += 1
                logger.debug("Skipped flush for doc %s: state unchanged", self.document_id)
                return {"state_revision": self.state_revision, "state_size_bytes": len(state)}
            result = save_document_state(self.document_id, state, known_revision=self.state_revision)
            if result is not None:
                self.state_revision = result["state_revision"]
                self._last_saved_hash = current_hash
                self.dirty = False
                logger.info("Flushed doc %s rev %s (%s bytes)",
                            self.document_id, self.state_revision, result["state_size_bytes"])
                return result
            else:
                logger.error("Flush returned no result for doc %s (stale? rev=%s)",
                             self.document_id, self.state_revision)
                return None
        except Exception as e:
            logger.error("Flush failed for doc %s: %s", self.document_id, e)
            return None

    async def close_room_async(self):
        """Async room closure - stop YRoom."""
        try:
            if self.yroom is not None:
                await self.yroom.stop()
                self.yroom = None
                self.ydoc = None
                self.dirty = False
                logger.info("Closed room for doc %s", self.document_id)
        except Exception as e:
            logger.error("Close error for doc %s: %s", self.document_id, e)

    def schedule_close(self):
        """Schedule room closure from sync context."""
        import asyncio
        try:
            asyncio.create_task(self.close_room_async())
        except Exception as e:
            logger.error("Schedule close error for doc %s: %s", self.document_id, e)

    def set_frozen(self, frozen: bool):
        """Set frozen state. When frozen, all incoming Yjs messages are dropped."""
        self.frozen = frozen
        if self.yroom is not None:
            if frozen:
                self.yroom.on_message = self._on_frozen_message
            else:
                self.yroom.on_message = self._on_ws_message

    def _on_frozen_message(self, message: bytes) -> bool:
        """Message handler when frozen - drop all messages."""
        self.last_active_at = time.time()
        return True  # True = skip (block update)

    async def _on_ws_message(self, message: bytes) -> bool:
        """Called when a Yjs message is received. Blocks when frozen."""
        self.last_active_at = time.time()
        if self.frozen:
            return True  # True = skip (block update)
        return False  # False = allow

    def can_save(self) -> bool:
        """Check if the snapshot can be saved (debounce + size check)."""
        if not self.dirty or self.ydoc is None:
            return False
        now = time.time()
        if now - self._last_save_attempt < SNAPSHOT_INTERVAL:
            return False
        state = self.get_y_state_bytes()
        if len(state) > MAX_DOC_SIZE_BYTES:
            logger.error("Doc %s state exceeds max size: %s bytes",
                         self.document_id, len(state))
            return False
        return True

    def _compute_state_hash(self):
        """Compute the hash of current Y.Doc state."""
        import hashlib
        state = self.get_y_state_bytes()
        if not state:
            return ""
        return hashlib.sha256(state).hexdigest()

    def can_save(self) -> bool:
        """Check if the snapshot can be saved (debounce + size check + hash dedup)."""
        if not self.dirty or self.ydoc is None:
            return False
        now = time.time()
        if now - self._last_save_attempt < SNAPSHOT_INTERVAL:
            return False
        state = self.get_y_state_bytes()
        if len(state) > MAX_DOC_SIZE_BYTES:
            logger.error("Doc %s state exceeds max size: %s bytes",
                         self.document_id, len(state))
            return False
        # Hash dedup: if state matches last saved hash, skip
        current_hash = hashlib.sha256(state).hexdigest() if state else ""
        if current_hash and current_hash == self._last_saved_hash:
            self.dirty = False
            self._idle_save_count += 1
            logger.debug("Skipped periodic save for doc %s: state unchanged", self.document_id)
            return False
        return True



    def perform_save(self) -> bool:
        """Perform the actual snapshot save to database.

        Returns True on success. Dirty stays True on failure (requirement #23).
        """
        if not self.dirty or self.ydoc is None:
            self.dirty = False
            return True

        self._last_save_attempt = time.time()
        self._save_count += 1
        try:
            state = self.get_y_state_bytes()
            # Hash dedup: skip if state hasn't changed since last save
            current_hash = hashlib.sha256(state).hexdigest()
            if current_hash and current_hash == self._last_saved_hash:
                self.dirty = False
                self._idle_save_count += 1
                logger.debug("Skipped save for doc %s: state unchanged (hash %s...)",
                             self.document_id, current_hash[:8])
                return True
            result = save_document_state(self.document_id, state, known_revision=self.state_revision)
            if result is not None:
                self.state_revision = result["state_revision"]
                self._last_saved_hash = current_hash
                self.dirty = False
                logger.info("Saved doc %s rev %s (%s bytes)",
                            self.document_id, self.state_revision, result["state_size_bytes"])
                return True
            else:
                logger.warning("Save returned no result for doc %s (stale revision? rev=%s)",
                               self.document_id, self.state_revision)
                return False
        except Exception as e:
            logger.error("Save failed for doc %s: %s", self.document_id, e)
            # dirty stays True on failure (requirement #23)
            return False

    def is_stale(self) -> bool:
        """Check if this room is stale (no connected clients and timed out).

        Uses YRoom.clients (managed by YRoom internally) instead of
        a separate connection counter.
        """
        if self.yroom is not None and len(self.yroom.clients) > 0:
            return False
        return (time.time() - self.last_active_at) > ROOM_RECYCLE_TIMEOUT
