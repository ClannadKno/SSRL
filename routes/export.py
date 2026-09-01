# -*- coding: utf-8 -*-
"""Teacher research export routes.

All supported routes return full, non-blinded ZIP archives.  Questionnaire files
are aggregated under each session; other research files remain session/group
partitioned.  Query-string filters are intentionally ignored by the contract.
"""

from flask import Response, redirect

from auth import login_required
from core import app
from services.research_export_service import RESEARCH_EXPORT_KEYS, build_research_export


LEGACY_EXPORT_REDIRECTS = {
    "messages.csv": "messages",
    "detector_outputs.csv": "state-assessments",
    "strategy_pipeline_runs.csv": "strategy-pipeline",
    "interventions.csv": "interventions",
    "participation_summary.csv": "participation",
    "emotion_snapshots.csv": "emotion-checkins",
    "help_requests.csv": "help-requests",
    "deliverables.csv": "deliverables",
    "survey_responses.csv": "questionnaires",
}

REMOVED_EXPORTS = {
    "intervention_uptake.csv",
    "unified-events.csv",
    "audit_logs.csv",
    "strategy_reviews.csv",
    "process_events.csv",
    "ssrl_events.csv",
    "autonomous_regulation_events.csv",
    "checkins.csv",
    "roster.csv",
}


def _zip_response(result):
    path_structure = (result.get("manifest") or {}).get(
        "path_structure", "sessions/session/group"
    )
    return Response(
        result["zip_data"],
        mimetype="application/zip",
        headers={
            "Content-Disposition": "attachment; filename=" + result["filename"],
            "X-Export-Version": "4.0",
            "X-Export-Structure": path_structure,
            "X-Export-Mode": "full_nonblinded",
        },
    )


@app.route("/export/all")
@login_required("teacher")
def export_all_zip():
    return _zip_response(build_research_export("all"))


@app.route("/export/<export_name>")
@login_required("teacher")
def export_csv(export_name):
    """Return a supported research ZIP or redirect a retained legacy URL."""
    if export_name in RESEARCH_EXPORT_KEYS:
        return _zip_response(build_research_export(export_name))
    if export_name in LEGACY_EXPORT_REDIRECTS:
        return redirect("/export/" + LEGACY_EXPORT_REDIRECTS[export_name], code=308)
    if export_name in REMOVED_EXPORTS:
        return "Not Found", 404
    return "Not Found", 404


@app.route("/api/teacher/questionnaire-raw-export")
@login_required("teacher")
def api_teacher_questionnaire_raw_export():
    """Compatibility redirect; the unified builder now owns questionnaires."""
    return redirect("/export/questionnaires", code=308)
