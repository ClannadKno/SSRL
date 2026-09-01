# -*- coding: utf-8 -*-
"""Unified zip export builder for SSRL-ESP.

Builds a ZIP file with session/group directory structure:

  export_type_YYYY-MM-DD_HHMM.zip
    {safe_session_name}_session-{session_id}/
      第{NN}组/
        {files}
      第{NN}组/
        {files}
    manifest.json

Usage:

    builder = ZipExportBuilder("messages")
    builder.add_csv(session_info, group_info, "data.csv",
                    fieldnames, rows, meta_fn)
    builder.add_text(session_info, group_info, "doc.md", content)
    data = builder.finalize(export_type="messages")
"""

import csv
import io
import json
import zipfile
from datetime import datetime

from services.export_safety import (
    safe_path_segment,
    safe_session_dir,
    safe_group_dir,
    build_export_filename,
)


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class ZipExportBuilder:
    """Build a session/group-partitioned ZIP file."""

    def __init__(self, export_type=""):
        self.export_type = export_type
        self.buf = io.BytesIO()
        self.zf = zipfile.ZipFile(self.buf, "w", zipfile.ZIP_DEFLATED)
        self.included_files = []
        self.session_ids = set()
        self.group_ids = set()

    # -----------------------------------------------------------------
    # Public helpers
    # -----------------------------------------------------------------

    def add_csv(self, session, group, filename, fieldnames, rows,
                blind_fn=None, blind=True, meta_fn=None):
        """Add a formatted CSV file under session/group directory.

        *session*  – dict/Row with keys serving `safe_session_dir`.
        *group*    – dict/Row with keys serving `safe_group_dir`.
        *filename* – eg `messages.csv`.
        *fieldnames* – list of column names for CSV header.
        *rows*     – list of dict/Row (each must contain session_id/group_id).
        *blind_fn* – optional callable(row_dict) -> row_dict (modifies for blind).
        *blind*    – whether to apply blind_fn (default True).
        *meta_fn*  – optional callable() -> dict of global metadata to add.
        """
        sdir = safe_session_dir(session)
        gdir = safe_group_dir(group)
        path = "%s/%s/%s" % (sdir, gdir, filename)

        self.session_ids.add(session.get("session_id") or session.get("id"))
        self.group_ids.add(group.get("group_id") or group.get("id"))

        result_rows = []
        for r in rows:
            d = blind_fn(dict(r)) if blind and blind_fn else dict(r)
            if meta_fn:
                d.update(meta_fn())
            result_rows.append(d)

        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=fieldnames,
                                extrasaction="ignore")
        writer.writeheader()
        writer.writerows(result_rows)

        if path in self.included_files:
            self.zf.writestr(path + ".duplicate", output.getvalue().encode("utf-8-sig"))
            print(f"[zip_export_builder] WARNING: Duplicate CSV path: {path}")
        else:
            self.zf.writestr(path, output.getvalue().encode("utf-8-sig"))
        self.included_files.append(path)

    def add_text(self, session, group, filename, content, encoding="utf-8"):
        """Add a plain-text file under session/group directory.

        *session*  – dict/Row with keys serving `safe_session_dir`.
        *group*    – dict/Row with keys serving `safe_group_dir`.
        *filename* – eg `collaborative_document.md`.
        *content*  – string content.
        """
        sdir = safe_session_dir(session)
        gdir = safe_group_dir(group)
        path = "%s/%s/%s" % (sdir, gdir, filename)

        self.session_ids.add(session.get("session_id") or session.get("id"))
        self.group_ids.add(group.get("group_id") or group.get("id"))

        self.zf.writestr(path, content.encode(encoding))
        self.included_files.append(path)

    def add_raw(self, arcname, content, encoding="utf-8"):
        """Add a file at an arbitrary archive path.
        Batch 10: Detects and warns about duplicate paths.
        """
        if arcname in self.included_files:
            print(f"[zip_export_builder] WARNING: Duplicate path skipped: {arcname}")
            return
        self.zf.writestr(arcname, content.encode(encoding))
        self.included_files.append(arcname)

    def finalize(self, export_type=None, filters=None):
        """Close the zip and return bytes.

        Automatically adds a `manifest.json` in the root.
        """
        if export_type:
            self.export_type = export_type
        manifest = {
            "export_type": self.export_type,
            "generated_at": now_str(),
            "session_count": len(self.session_ids),
            "group_count": len(self.group_ids),
            "included_files": list(self.included_files),
            "filters": filters or {},
        }
        self.zf.writestr(
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2),
        )
        self.zf.close()
        return self.buf.getvalue()


# -----------------------------------------------------------------
# Partition helper
# -----------------------------------------------------------------

def partition_rows(rows, session_key="session_id", group_key="group_id"):
    """Partition a list of dict-rows by session_id and group_id.

    Returns `{session_id: {group_id: [row_dict, ...]}}`.

    Only rows with a non-None session_key are returned.
    """
    partitions = {}
    for r in rows:
        sid = r.get(session_key)
        gid = r.get(group_key)
        if sid is None:
            continue
        if sid not in partitions:
            partitions[sid] = {}
        if gid not in partitions[sid]:
            partitions[sid][gid] = []
        partitions[sid][gid].append(dict(r))
    return partitions


def lookup_sessions(db_module):
    """Return {session_id: row_dict} for all experiment_sessions.

    Row dict includes `id`, `session_name`, `session_no`.
    """
    sess_rows = db_module.query_all(
        "SELECT id, session_no, session_role FROM experiment_sessions"
    )
    return {r["id"]: dict(r) for r in sess_rows}


def lookup_groups(db_module):
    """Return {group_id: row_dict} for all groups.

    Row dict includes `id` and `group_code`.
    """
    grp_rows = db_module.query_all(
        "SELECT id, group_code FROM groups"
    )
    result = {}
    for r in grp_rows:
        d = dict(r)
        result[r["id"]] = d
    return result
