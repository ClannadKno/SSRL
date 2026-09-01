# -*- coding: utf-8 -*-
"""
Database schema migrations.

This module is imported by db.init_db() to backfill columns that were
added after the initial CREATE TABLE, enabling existing databases to
catch up without a full rebuild.
"""
import sqlite3


def _ensure_unique_session_no_index(conn):
    """Create the session_no unique index when existing data is clean."""
    duplicates = conn.execute("""
        SELECT session_no, COUNT(*) AS count
        FROM experiment_sessions
        GROUP BY session_no
        HAVING COUNT(*) > 1
        LIMIT 1
    """).fetchone()
    if duplicates:
        return
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_experiment_sessions_session_no_unique
        ON experiment_sessions(session_no)
    """)


def register_migration(version, name, callback):
    """Register a numbered migration. Currently a no-op for compatibility."""
    pass



def run_pending_migrations(conn):
    """Apply schema changes that are safe for existing databases.

    Each _ensure_column() call is idempotent --- it checks whether the
    column already exists before executing the ALTER TABLE.
    """
    _migration_login_key_lookup(conn)

    # ---- questionnaire_responses ----
    _ensure_column(conn, "questionnaire_responses", "session_no", "INTEGER DEFAULT 0")
    _ensure_column(conn, "questionnaire_responses", "task_id", "INTEGER DEFAULT 0")
    _ensure_column(conn, "questionnaire_responses", "response_batch_id", "TEXT")
    # ---- questionnaire_items ----
    _ensure_column(conn, "questionnaire_items", "item_order", "INTEGER DEFAULT 1")
    # ---- submissions ----
    _ensure_column(conn, "submissions", "scored_by", "INTEGER")
    _ensure_column(conn, "submissions", "submitted_by", "TEXT")
    _ensure_column(conn, "submissions", "submit_reason", "TEXT")
    _ensure_column(conn, "submissions", "submission_source", "TEXT")
    _ensure_column(conn, "submissions", "timeout_at", "TEXT")
    # ---- users ----
    _ensure_column(conn, "users", "guardian_consent", "INTEGER DEFAULT 0")
    _ensure_column(conn, "users", "consent_ack", "INTEGER DEFAULT 0")
    _ensure_column(conn, "users", "consent_version", "TEXT")
    _ensure_column(conn, "users", "consented_at", "TEXT")
    # ---- state_assessments (v2 schema) ----
    _ensure_column(conn, "state_assessments", "rule_state_code", "TEXT")
    _ensure_column(conn, "state_assessments", "llm_state_code", "TEXT")
    _ensure_column(conn, "state_assessments", "fused_state_code", "TEXT")
    _ensure_column(conn, "state_assessments", "fused_state_label", "TEXT")
    _ensure_column(conn, "state_assessments", "assessment_status", "TEXT")
    _ensure_column(conn, "state_assessments", "confidence", "REAL")
    _ensure_column(conn, "state_assessments", "risk_level", "INTEGER")
    _ensure_column(conn, "state_assessments", "risk_label", "TEXT")
    _ensure_column(conn, "state_assessments", "should_intervene", "INTEGER DEFAULT 0")
    _ensure_column(conn, "state_assessments", "self_regulation_detected", "INTEGER DEFAULT 0")
    _ensure_column(conn, "state_assessments", "evidence_summary", "TEXT")
    _ensure_column(conn, "state_assessments", "rule_assessment_json", "TEXT")
    _ensure_column(conn, "state_assessments", "llm_assessment_json", "TEXT")
    _ensure_column(conn, "state_assessments", "context_json", "TEXT")
    _ensure_column(conn, "state_assessments", "feature_json", "TEXT")
    _ensure_column(conn, "state_assessments", "rule_version", "TEXT")
    _ensure_column(conn, "state_assessments", "detector_version", "TEXT")
    _ensure_column(conn, "state_assessments", "model_name", "TEXT")
    _ensure_column(conn, "state_assessments", "prompt_version", "TEXT")
    _ensure_column(conn, "state_assessments", "request_started_at", "TEXT")
    _ensure_column(conn, "state_assessments", "request_finished_at", "TEXT")
    _ensure_column(conn, "state_assessments", "latency_ms", "INTEGER")
    _ensure_column(conn, "state_assessments", "fallback_used", "INTEGER DEFAULT 0")
    _ensure_column(conn, "state_assessments", "error_message", "TEXT")
    # ---- monitor_runs strategy review audit fields ----
    _ensure_column(conn, "monitor_runs", "context_from_sequence", "INTEGER")
    _ensure_column(conn, "monitor_runs", "context_to_sequence", "INTEGER")
    _ensure_column(conn, "monitor_runs", "input_message_sequences_json", "TEXT")
    _ensure_column(conn, "monitor_runs", "evidence_sequences_json", "TEXT")
    _ensure_column(conn, "monitor_runs", "review_decision", "TEXT")
    _ensure_column(conn, "monitor_runs", "review_final_state", "TEXT")
    _ensure_column(conn, "monitor_runs", "review_confidence", "REAL")
    _ensure_column(conn, "monitor_runs", "review_reason", "TEXT")
    _ensure_column(conn, "monitor_runs", "selected_strategy_id", "TEXT")
    _ensure_column(conn, "monitor_runs", "generated_message", "TEXT")
    _ensure_column(conn, "monitor_runs", "prompt_version", "TEXT")
    _ensure_column(conn, "monitor_runs", "review_started_at", "TEXT")
    _ensure_column(conn, "monitor_runs", "review_completed_at", "TEXT")
    _ensure_column(conn, "monitor_runs", "review_error", "TEXT")
    _ensure_column(conn, "monitor_runs", "state_assessment_id", "INTEGER")
    _ensure_column(conn, "monitor_runs", "session_id", "INTEGER")
    _ensure_column(conn, "monitor_runs", "task_id", "INTEGER")
    _ensure_column(conn, "monitor_runs", "decision", "TEXT")
    _ensure_column(conn, "monitor_runs", "teacher_reason", "TEXT")
    _ensure_column(conn, "monitor_runs", "message_id", "INTEGER")
    _ensure_column(conn, "monitor_runs", "lock_acquired", "INTEGER DEFAULT 0")
    _ensure_column(conn, "monitor_runs", "cooldown_result", "TEXT")
    # ---- state_assessments (confirmation) ----
    _ensure_column(conn, "state_assessments", "confirmed_windows", "INTEGER DEFAULT 0")
    _ensure_column(conn, "state_assessments", "confirmation_status", "TEXT")
    # ---- group_states (v2 schema) ----
    _ensure_column(conn, "group_states", "risk_label", "TEXT")
    _ensure_column(conn, "group_states", "state_assessment_id", "INTEGER")
    _ensure_column(conn, "group_states", "assessment_status", "TEXT")
    _ensure_column(conn, "group_states", "confirmed_windows", "INTEGER DEFAULT 0")
    _ensure_column(conn, "group_states", "confirmation_status", "TEXT")
    _ensure_column(conn, "group_states", "llm_state_code", "TEXT")
    _ensure_column(conn, "group_states", "fusion_json", "TEXT")
    # ---- intervention_decisions (v2 schema) ----
    _ensure_column(conn, "intervention_decisions", "should_intervene", "INTEGER DEFAULT 0")
    _ensure_column(conn, "intervention_decisions", "decision_reason", "TEXT")
    _ensure_column(conn, "intervention_decisions", "suppressed_reason", "TEXT")
    _ensure_column(conn, "intervention_decisions", "target", "TEXT")
    _ensure_column(conn, "intervention_decisions", "priority", "INTEGER")
    _ensure_column(conn, "intervention_decisions", "strategy_category", "TEXT")
    _ensure_column(conn, "intervention_decisions", "selected_strategy_id", "TEXT")
    _ensure_column(conn, "intervention_decisions", "condition", "TEXT")
    _ensure_column(conn, "intervention_decisions", "decision_version", "TEXT")


    _ensure_column(conn, "submissions", "score", "REAL")
    _ensure_column(conn, "submissions", "score_note", "TEXT")
    _ensure_column(conn, "submissions", "score_dimensions_json", "TEXT")
    _ensure_column(conn, "submissions", "scored_at", "TEXT")

    # ---- emotion_checkins columns for CSV export ----
    _ensure_column(conn, "emotion_checkins", "positivity", "INTEGER DEFAULT 0")
    _ensure_column(conn, "emotion_checkins", "engagement", "INTEGER DEFAULT 0")
    _ensure_column(conn, "emotion_checkins", "atmosphere", "INTEGER DEFAULT 0")
    _ensure_column(conn, "emotion_checkins", "expression_willingness", "INTEGER DEFAULT 0")
    _ensure_column(conn, "emotion_checkins", "note", "TEXT")

        # ---- batch 3: y_state_hash column for dedup ----
    _ensure_column(conn, "collaborative_documents", "y_state_hash", "TEXT")

# ---- batch 4: help request async columns & indexes ----
    _ensure_column(conn, "help_requests", "request_text", "TEXT")
    # ---- intervention_logs v2 schema columns ----
    _ensure_column(conn, "intervention_logs", "suggestion_id", "INTEGER")
    _ensure_column(conn, "intervention_logs", "decision_id", "INTEGER")
    _ensure_column(conn, "intervention_logs", "strategy_id", "TEXT")
    _ensure_column(conn, "intervention_logs", "template_id", "TEXT")
    _ensure_column(conn, "intervention_logs", "sub_category", "TEXT")
    _ensure_column(conn, "intervention_logs", "strategy_type", "TEXT")
    _ensure_column(conn, "intervention_logs", "strategy_version", "TEXT")
    _ensure_column(conn, "intervention_logs", "model_name", "TEXT")
    _ensure_column(conn, "intervention_logs", "prompt_version", "TEXT")
    _ensure_column(conn, "intervention_logs", "message_id", "INTEGER")
    _ensure_column(conn, "intervention_logs", "help_request_id", "INTEGER")
    _ensure_column(conn, "intervention_logs", "state_assessment_id", "INTEGER")
    _ensure_column(conn, "intervention_logs", "monitor_run_id", "INTEGER")
    _ensure_column(conn, "intervention_logs", "intervention_run_id", "INTEGER")
    _ensure_column(conn, "intervention_logs", "agent_type", "TEXT")
    # ---- agent_suggestions v2 columns ----
    _ensure_column(conn, "agent_suggestions", "decided_at", "TEXT")
    _ensure_column(conn, "agent_suggestions", "decided_by_user_id", "INTEGER")
    _ensure_column(conn, "agent_suggestions", "decision_note", "TEXT")
    _ensure_column(conn, "agent_suggestions", "decided_at", "TEXT")
    # ---- process_events columns ----
    _ensure_column(conn, "process_events", "actor_role", "TEXT")
    _ensure_column(conn, "process_events", "participant_code", "TEXT")
    _ensure_column(conn, "process_events", "group_code", "TEXT")
    _ensure_column(conn, "process_events", "condition", "TEXT")
    _ensure_column(conn, "process_events", "session_no", "INTEGER")
    _ensure_column(conn, "process_events", "task_id", "INTEGER")
    _ensure_column(conn, "process_events", "payload_json", "TEXT")
    # ---- process_events unique index for ON CONFLICT ----
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_process_events_event_key ON process_events(event_key)")
    _ensure_column(conn, "help_requests", "intent", "TEXT")
    _ensure_column(conn, "help_requests", "response_message", "TEXT")
    _ensure_column(conn, "help_requests", "fallback_used", "INTEGER DEFAULT 0")
    _ensure_column(conn, "help_requests", "response_message_id", "INTEGER")
    _ensure_column(conn, "agent_suggestions", "help_request_id", "INTEGER")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_suggestions_help_request_id ON agent_suggestions(help_request_id) WHERE help_request_id IS NOT NULL")
    # ---- batch 1: help_requests and intervention_runs columns ----
    _ensure_column(conn, "help_requests", "source_message_id", "INTEGER")
    _ensure_column(conn, "help_requests", "help_request_message_sequence", "INTEGER")
    _ensure_column(conn, "help_requests", "handled_at", "TEXT")
    _ensure_column(conn, "help_requests", "handling_status", "TEXT")
    _ensure_column(conn, "help_requests", "covered_until_sequence", "INTEGER")
    # Issue-aware help coverage.  Existing rows are intentionally not
    # backfilled; the shared guard derives compatibility data at read time.
    _ensure_column(conn, "help_requests", "handled_state_code", "TEXT")
    _ensure_column(conn, "help_requests", "handled_segment_id", "INTEGER")
    _ensure_column(conn, "help_requests", "handled_evidence_start_sequence", "INTEGER")
    _ensure_column(conn, "help_requests", "handled_evidence_end_sequence", "INTEGER")
    _ensure_column(conn, "intervention_runs", "detected_state", "TEXT")
    _ensure_column(conn, "intervention_runs", "confidence", "REAL")
    _ensure_column(conn, "intervention_runs", "strategy_id", "INTEGER")
    _ensure_column(conn, "intervention_runs", "strategy_version", "TEXT")
    _ensure_column(conn, "intervention_runs", "fallback_used", "INTEGER DEFAULT 0")
    _ensure_column(conn, "intervention_runs", "model_profile", "TEXT")
    _ensure_column(conn, "intervention_runs", "latency_ms", "INTEGER")
    _ensure_column(conn, "intervention_runs", "context_from_sequence", "INTEGER")
    _ensure_column(conn, "intervention_runs", "context_to_sequence", "INTEGER")
    _ensure_column(conn, "intervention_runs", "input_message_sequences_json", "TEXT")
    _ensure_column(conn, "intervention_runs", "evidence_sequences_json", "TEXT")
    _ensure_column(conn, "intervention_runs", "selected_strategy_id", "TEXT")
    _ensure_column(conn, "intervention_runs", "prompt_version", "TEXT")
    _ensure_column(conn, "intervention_runs", "help_request_id", "INTEGER")
    _ensure_column(conn, "intervention_runs", "assessment_batch_id", "INTEGER")
    _ensure_column(conn, "intervention_runs", "target_segment_id", "INTEGER")
    _ensure_column(conn, "intervention_runs", "reason_code", "TEXT")
    _ensure_column(conn, "intervention_runs", "discussion_id", "INTEGER")
    _ensure_column(conn, "intervention_runs", "active_segment_index", "INTEGER")
    _ensure_column(conn, "intervention_runs", "guard_result", "TEXT")
    _ensure_column(conn, "intervention_runs", "guard_reason", "TEXT")
    _ensure_column(conn, "intervention_runs", "retry_count", "INTEGER DEFAULT 0")
    _ensure_column(conn, "intervention_runs", "raw_response", "TEXT")
    _ensure_column(conn, "intervention_runs", "started_at", "TEXT")
    _ensure_column(conn, "intervention_runs", "agent_type", "TEXT DEFAULT 'strategy'")
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_intervention_runs_assessment_batch
        ON intervention_runs(assessment_batch_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_intervention_runs_target_segment
        ON intervention_runs(target_segment_id)
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_intervention_runs_target_segment_strategy
        ON intervention_runs(
            group_id,
            COALESCE(session_id, 0),
            COALESCE(discussion_id, 0),
            target_segment_id,
            COALESCE(agent_type, 'strategy')
        )
        WHERE target_segment_id IS NOT NULL
          AND status IN (
              'PENDING','RUNNING','REVALIDATING','LOCKED',
              'GENERATING','VALIDATING','PUBLISHED','FALLBACK'
          )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_help_requests_guard_scope
        ON help_requests(
            group_id, session_id, help_request_message_sequence,
            handling_status, status
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_help_requests_handled_issue
        ON help_requests(
            group_id, session_id, handled_state_code, handled_segment_id,
            handled_evidence_start_sequence, handled_evidence_end_sequence
        )
    """)
    conn.execute("""
        UPDATE help_requests
        SET help_request_message_sequence=(
                SELECT m.sequence
                FROM messages AS m
                WHERE m.id=help_requests.source_message_id
            )
        WHERE help_request_message_sequence IS NULL
          AND source_message_id IS NOT NULL
    """)
    conn.execute("""
        UPDATE help_requests
        SET handling_status=CASE
                WHEN UPPER(COALESCE(status, '')) IN ('COMPLETED','COMPLETED_WITH_FALLBACK')
                    THEN 'handled'
                WHEN UPPER(COALESCE(status, ''))='FAILED' THEN 'failed'
                WHEN UPPER(COALESCE(status, '')) IN ('RUNNING','PROCESSING') THEN 'running'
                ELSE 'queued'
            END,
            handled_at=CASE
                WHEN UPPER(COALESCE(status, '')) IN ('COMPLETED','COMPLETED_WITH_FALLBACK','FAILED')
                    THEN COALESCE(handled_at, completed_at)
                ELSE handled_at
            END,
            covered_until_sequence=CASE
                WHEN UPPER(COALESCE(status, '')) IN ('COMPLETED','COMPLETED_WITH_FALLBACK')
                    THEN COALESCE(covered_until_sequence, help_request_message_sequence)
                ELSE covered_until_sequence
            END
        WHERE handling_status IS NULL
           OR help_request_message_sequence IS NOT NULL
    """)
    _ensure_column(conn, "messages", "trigger_source", "TEXT")
    # ---- Batch 12: emotion tick index ----
    _ensure_column(conn, "intervention_runs", "tick_index", "INTEGER")
    # ---- batch 1: unique indexes on messages ----
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_group_sequence ON messages(group_id, sequence)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_user_client_msg_id ON messages(group_id, user_id, client_message_id)")
    # ---- batch 6: V2 intervention pipeline columns ----
    _migration_v004_batch6_intervention_v2(conn)
    # ---- batch 4: submission_prepares table ----
    conn.execute("""CREATE TABLE IF NOT EXISTS submission_prepares (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id INTEGER NOT NULL REFERENCES collaborative_documents(id),
        freeze_id TEXT NOT NULL,
        state_revision INTEGER NOT NULL DEFAULT 0,
        committed INTEGER NOT NULL DEFAULT 0,
        created_by INTEGER NOT NULL REFERENCES users(id),
        created_at TEXT NOT NULL
    )""")
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_submission_prepares_doc
        ON submission_prepares(document_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_submission_prepares_freeze
        ON submission_prepares(freeze_id)
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_help_requests_group_intent ON help_requests(group_id, intent)")
    # ---- batch 1: collaborative document tables ----
    _migration_batch1_collab_tables(conn)
    _migration_teacher_console_v1(conn)
    # ---- discussion group timer: per-group discussion runtime ----
    _migration_group_discussion_runtime(conn)
    _migration_emotion_reflection_slots(conn)
    _migration_incremental_state_assessment_tables(conn)
    _migration_collaboration_state_segments(conn)
    _migration_collaboration_state_finalizations(conn)
    _migration_three_stage_pipeline_schema(conn)
    _migration_three_stage_latency_events(conn)

    # ---- batch 7: T5 agent audit + T8 safety pause columns ----
    _ensure_column(conn, "intervention_uptake", "reason", "TEXT")
    _ensure_column(conn, "group_session_controls", "resumed_by", "INTEGER")
    _ensure_column(conn, "safety_signals", "session_id", "INTEGER")
    _ensure_column(conn, "safety_signals", "group_session_control_id", "INTEGER")
    _ensure_column(conn, "messages", "intervention_uptake_id", "INTEGER")
    # ---- batch 7: phase isolation for emotion_checkins and help_requests ----
    _ensure_column(conn, "emotion_checkins", "session_no", "INTEGER")
    _ensure_column(conn, "help_requests", "task_id", "INTEGER")
    _ensure_column(conn, "help_requests", "session_no", "INTEGER")
    _ensure_column(conn, "help_requests", "session_id", "INTEGER")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_emotion_checkins_group_session ON emotion_checkins(group_id, session_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_emotion_checkins_group_task_session ON emotion_checkins(group_id, task_id, session_no)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_help_requests_group_session ON help_requests(group_id, session_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_help_requests_group_task_session ON help_requests(group_id, task_id, session_no)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_group_session ON messages(group_id, session_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_group_task_session ON messages(group_id, task_id, session_no)")
    # Record completed migration version
    conn.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
        ("schema_version", "1"),
    )
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_suggestions_help_request_id ON agent_suggestions(help_request_id) WHERE help_request_id IS NOT NULL")
    conn.commit()

    # ---- Batch 6: Fixed questionnaire, publications, and submissions ----
    _ensure_column(conn, "questionnaires", "is_fixed", "INTEGER DEFAULT 0")
    _ensure_column(conn, "questionnaire_items", "section_no", "INTEGER DEFAULT 1")
    _ensure_column(conn, "questionnaire_items", "section_title", "TEXT DEFAULT ''")
    _ensure_column(conn, "questionnaire_items", "question_type", "TEXT DEFAULT 'likert_5'")
    _ensure_column(conn, "questionnaire_items", "dimension_key", "TEXT DEFAULT ''")
    _ensure_column(conn, "questionnaire_items", "reverse_scored", "INTEGER DEFAULT 0")
    _ensure_column(conn, "questionnaire_items", "min_value", "INTEGER DEFAULT 1")
    _ensure_column(conn, "questionnaire_items", "max_value", "INTEGER DEFAULT 5")
    _ensure_column(conn, "questionnaire_items", "score_map_json", "TEXT")
    _ensure_column(conn, "questionnaire_items", "options_json", "TEXT")
    _ensure_column(conn, "questionnaire_items", "scale_labels_json", "TEXT")
    _ensure_column(conn, "questionnaire_items", "include_in_score", "INTEGER DEFAULT 1")
    _ensure_column(conn, "questionnaire_items", "help_text", "TEXT DEFAULT ''")
    _ensure_column(conn, "questionnaires", "version", "TEXT DEFAULT 'v1'")
    _ensure_column(conn, "questionnaires", "instruction_pre", "TEXT DEFAULT ''")
    _ensure_column(conn, "questionnaires", "instruction_post", "TEXT DEFAULT ''")
    _ensure_column(conn, "questionnaires", "scoring_method", "TEXT DEFAULT 'mean'")
    _ensure_column(conn, "questionnaires", "metadata_json", "TEXT")
    _ensure_column(conn, "questionnaire_responses", "submission_id", "INTEGER")
    _ensure_column(conn, "questionnaire_responses", "session_id", "INTEGER")
    _ensure_column(conn, "questionnaire_responses", "response_option_key", "TEXT")
    conn.execute("""CREATE TABLE IF NOT EXISTS questionnaire_sections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        questionnaire_id INTEGER NOT NULL,
        section_key TEXT DEFAULT '',
        title TEXT DEFAULT '',
        description TEXT DEFAULT '',
        sort_order INTEGER DEFAULT 0,
        created_at TEXT,
        FOREIGN KEY(questionnaire_id) REFERENCES questionnaires(id)
    )""")
    # questionnaire_publications table
    conn.execute("""CREATE TABLE IF NOT EXISTS questionnaire_publications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        questionnaire_id INTEGER NOT NULL,
        session_id INTEGER NOT NULL,
        session_no INTEGER NOT NULL,
        response_stage TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'enabled',
        created_at TEXT NOT NULL,
        FOREIGN KEY(questionnaire_id) REFERENCES questionnaires(id),
        FOREIGN KEY(session_id) REFERENCES experiment_sessions(id)
    )""")
    _ensure_column(conn, "questionnaire_publications", "group_id", "INTEGER")
    _ensure_column(conn, "questionnaire_publications", "user_id", "INTEGER")
    _ensure_column(conn, "questionnaire_publications", "questionnaire_set_id", "INTEGER")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_qsections_q_sort ON questionnaire_sections(questionnaire_id, sort_order)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_qsections_q_key ON questionnaire_sections(questionnaire_id, section_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_qsubmissions_session_group ON questionnaire_submissions(session_id, group_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_qitems_q_section_sort ON questionnaire_items(questionnaire_id, section_no, sort_order)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_qresponses_submission ON questionnaire_responses(submission_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_qresponses_q_u_session ON questionnaire_responses(questionnaire_id, user_id, session_id)")
    conn.execute("DROP INDEX IF EXISTS idx_qp_unique")
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_qp_unique
        ON questionnaire_publications(
            questionnaire_id,
            session_id,
            response_stage,
            COALESCE(group_id, 0),
            COALESCE(user_id, 0)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_qp_session
        ON questionnaire_publications(session_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_qp_questionnaire
        ON questionnaire_publications(questionnaire_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_qp_group
        ON questionnaire_publications(group_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_qp_user
        ON questionnaire_publications(user_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_qp_questionnaire_set
        ON questionnaire_publications(questionnaire_set_id)
    """)
    # ---- questionnaire sets: reusable questionnaire bundles ----
    conn.execute("""CREATE TABLE IF NOT EXISTS questionnaire_sets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        description TEXT DEFAULT '',
        active INTEGER NOT NULL DEFAULT 1,
        created_by INTEGER,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS questionnaire_set_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        set_id INTEGER NOT NULL,
        questionnaire_id INTEGER NOT NULL,
        sort_order INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        FOREIGN KEY(set_id) REFERENCES questionnaire_sets(id),
        FOREIGN KEY(questionnaire_id) REFERENCES questionnaires(id)
    )""")
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_qset_items_set_order
        ON questionnaire_set_items(set_id, sort_order, id)
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_qset_items_unique
        ON questionnaire_set_items(set_id, questionnaire_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_qsets_active
        ON questionnaire_sets(active)
    """)
    # questionnaire_submissions table
    conn.execute("""CREATE TABLE IF NOT EXISTS questionnaire_submissions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        questionnaire_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        group_id INTEGER NOT NULL,
        session_id INTEGER,
        session_no INTEGER DEFAULT 0,
        response_stage TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'submitted',
        submitted_at TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(questionnaire_id) REFERENCES questionnaires(id),
        FOREIGN KEY(user_id) REFERENCES users(id),
        FOREIGN KEY(group_id) REFERENCES groups(id)
    )""")
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_qs_unique
        ON questionnaire_submissions(questionnaire_id, user_id, session_id, response_stage)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_qs_questionnaire
        ON questionnaire_submissions(questionnaire_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_qs_session
        ON questionnaire_submissions(session_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_qs_group
        ON questionnaire_submissions(group_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_qr_submission
        ON questionnaire_responses(submission_id)
    """)
    conn.commit()

    # ---- learning_tasks extended columns ----
    _ensure_column(conn, "learning_tasks", "task_goal", "TEXT DEFAULT ''")
    _ensure_column(conn, "learning_tasks", "output_requirement", "TEXT DEFAULT ''")
    _ensure_column(conn, "learning_tasks", "time_limit_minutes", "INTEGER DEFAULT 30")
    _ensure_column(conn, "learning_tasks", "expected_dimensions_json", "TEXT DEFAULT '[]'")
    _ensure_column(conn, "learning_tasks", "key_concepts_json", "TEXT DEFAULT '[]'")
    _ensure_column(conn, "learning_tasks", "common_misconceptions_json", "TEXT DEFAULT '[]'")
    _ensure_column(conn, "learning_tasks", "acceptable_paths_json", "TEXT DEFAULT '[]'")
    _ensure_column(conn, "learning_tasks", "updated_at", "TEXT")

    # ---- Batch 1: experiment identity tables ----
    from db import ensure_experiment_identity_tables
    ensure_experiment_identity_tables(conn)
    _migration_agent_dual_switch(conn)
    _migration_unified_agent_mode(conn)
    _migration_emotion_reflection_run_audit(conn)
    _migration_unified_discussion_scope(conn)
    conn.commit()


def _migration_group_discussion_runtime(conn):
    """Create per-group discussion timer runtime tables.

    These tables separate the teacher-controlled experiment session from the
    discussion phase timer. One row in group_session_discussions represents one
    group's discussion phase inside one experiment session.
    """
    conn.execute("""CREATE TABLE IF NOT EXISTS group_session_discussions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL,
        group_id INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'waiting'
            CHECK(status IN ('waiting','running','timed_out','submitted','closed')),
        expected_student_ids_json TEXT NOT NULL DEFAULT '[]',
        ready_student_ids_json TEXT NOT NULL DEFAULT '[]',
        expected_student_count INTEGER NOT NULL DEFAULT 0,
        ready_student_count INTEGER NOT NULL DEFAULT 0,
        started_at TEXT,
        deadline TEXT,
        submitted_at TEXT,
        auto_submitted_at TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(session_id) REFERENCES experiment_sessions(id) ON DELETE CASCADE,
        FOREIGN KEY(group_id) REFERENCES groups(id) ON DELETE CASCADE
    )""")
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_group_session_discussions_unique
        ON group_session_discussions(session_id, group_id)
    """)
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_group_session_discussions_status_deadline
        ON group_session_discussions(status, deadline)
    """)
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_group_session_discussions_session
        ON group_session_discussions(session_id)
    """)
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_group_session_discussions_group
        ON group_session_discussions(group_id)
    """)

    conn.execute("""CREATE TABLE IF NOT EXISTS group_discussion_entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_discussion_id INTEGER NOT NULL,
        student_id INTEGER NOT NULL,
        entered_at TEXT NOT NULL,
        ready_at TEXT,
        left_at TEXT,
        last_seen_at TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(group_discussion_id) REFERENCES group_session_discussions(id) ON DELETE CASCADE,
        FOREIGN KEY(student_id) REFERENCES users(id) ON DELETE CASCADE
    )""")
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_group_discussion_entries_unique
        ON group_discussion_entries(group_discussion_id, student_id)
    """)
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_group_discussion_entries_discussion
        ON group_discussion_entries(group_discussion_id)
    """)
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_group_discussion_entries_student
        ON group_discussion_entries(student_id)
    """)


def _create_emotion_reflection_slots_table(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS emotion_reflection_slots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER NOT NULL,
        session_id INTEGER NOT NULL,
        discussion_id INTEGER NOT NULL,
        slot_index INTEGER NOT NULL CHECK(slot_index >= 1),
        scheduled_at TEXT NOT NULL,
        prompt_version TEXT NOT NULL DEFAULT 'emotion_slot_windows_v1',
        previous_window_start TEXT,
        previous_window_end TEXT,
        current_window_start TEXT,
        current_window_end TEXT,
        window_frozen_at TEXT,
        previous_metrics_json TEXT,
        current_metrics_json TEXT,
        previous_message_ids_json TEXT,
        current_message_ids_json TEXT,
        input_message_ids_json TEXT,
        previous_messages_json TEXT,
        current_messages_json TEXT,
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK(status IN (
                'pending','running','deferred','sent','suppressed','failed',
                'expired','superseded','skipped'
            )),
        started_at TEXT,
        completed_at TEXT,
        message_id INTEGER,
        intervention_run_id INTEGER,
        skip_reason TEXT,
        retry_count INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 2,
        enqueued_at TEXT,
        next_retry_at TEXT,
        last_error TEXT,
        -- Deprecated cross-Agent coordination columns are retained only for
        -- existing databases and audit history.
        defer_count INTEGER NOT NULL DEFAULT 0,
        defer_deadline_at TEXT,
        generation_student_sequence INTEGER,
        superseded_by_slot_id INTEGER,
        coordination_strategy_run_id INTEGER,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(group_id) REFERENCES groups(id) ON DELETE CASCADE,
        FOREIGN KEY(session_id) REFERENCES experiment_sessions(id) ON DELETE CASCADE,
        FOREIGN KEY(discussion_id) REFERENCES group_session_discussions(id) ON DELETE CASCADE,
        FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE SET NULL,
        FOREIGN KEY(intervention_run_id) REFERENCES intervention_runs(id) ON DELETE SET NULL
    )""")


def _upgrade_emotion_reflection_slot_statuses(conn):
    """Expand the legacy CHECK constraint without rewriting business values."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='emotion_reflection_slots'"
    ).fetchone()
    if not row:
        _create_emotion_reflection_slots_table(conn)
        return
    table_sql = str(row[0] or "").lower()
    if "'deferred'" in table_sql and "'superseded'" in table_sql:
        return

    managed_indexes = (
        "idx_emotion_slots_scope_slot",
        "idx_emotion_slots_one_active",
        "idx_emotion_slots_due",
        "idx_emotion_slots_discussion",
        "idx_emotion_slots_message",
        "idx_emotion_slots_intervention",
    )
    custom_schema = [
        item[0]
        for item in conn.execute(
            """
            SELECT sql
            FROM sqlite_master
            WHERE tbl_name='emotion_reflection_slots'
              AND type IN ('index','trigger')
              AND sql IS NOT NULL
              AND name NOT IN (?,?,?,?,?,?)
            ORDER BY type, name
            """,
            managed_indexes,
        ).fetchall()
    ]
    for index_name in managed_indexes:
        conn.execute(f"DROP INDEX IF EXISTS {index_name}")
    conn.execute(
        "ALTER TABLE emotion_reflection_slots RENAME TO emotion_reflection_slots_legacy_status"
    )
    _create_emotion_reflection_slots_table(conn)
    legacy_columns = (
        "id, group_id, session_id, discussion_id, slot_index, scheduled_at, "
        "status, started_at, completed_at, message_id, intervention_run_id, "
        "skip_reason, retry_count, max_attempts, enqueued_at, next_retry_at, "
        "last_error, created_at, updated_at"
    )
    conn.execute(
        f"INSERT INTO emotion_reflection_slots({legacy_columns}) "
        f"SELECT {legacy_columns} FROM emotion_reflection_slots_legacy_status"
    )
    conn.execute("DROP TABLE emotion_reflection_slots_legacy_status")
    for statement in custom_schema:
        conn.execute(statement)


def _migration_emotion_reflection_slots(conn):
    """Create the idempotent, discussion-scoped emotion time-slot ledger."""
    # Historical emotion messages already point to their intervention run, but
    # older publishers did not fill the reverse run.message_id link.  Repair it
    # idempotently before new slots rely on a complete bidirectional audit.
    conn.execute("""
        UPDATE intervention_runs
           SET message_id=(
               SELECT m.id
               FROM messages AS m
               WHERE m.intervention_run_id=intervention_runs.id
               ORDER BY m.id ASC LIMIT 1
           )
         WHERE COALESCE(agent_type, '')='emotion'
           AND message_id IS NULL
           AND EXISTS(
               SELECT 1 FROM messages AS m
               WHERE m.intervention_run_id=intervention_runs.id
           )
    """)
    _upgrade_emotion_reflection_slot_statuses(conn)
    # Deprecated compatibility columns. New runtime code must not use them for
    # cross-Agent coordination; agent_mode makes visible Agents exclusive.
    _ensure_column(conn, "emotion_reflection_slots", "defer_count", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "emotion_reflection_slots", "defer_deadline_at", "TEXT")
    _ensure_column(conn, "emotion_reflection_slots", "generation_student_sequence", "INTEGER")
    _ensure_column(conn, "emotion_reflection_slots", "superseded_by_slot_id", "INTEGER")
    _ensure_column(conn, "emotion_reflection_slots", "coordination_strategy_run_id", "INTEGER")
    _ensure_column(
        conn,
        "emotion_reflection_slots",
        "prompt_version",
        "TEXT NOT NULL DEFAULT 'emotion_slot_windows_v1'",
    )
    _ensure_column(conn, "emotion_reflection_slots", "previous_window_start", "TEXT")
    _ensure_column(conn, "emotion_reflection_slots", "previous_window_end", "TEXT")
    _ensure_column(conn, "emotion_reflection_slots", "current_window_start", "TEXT")
    _ensure_column(conn, "emotion_reflection_slots", "current_window_end", "TEXT")
    _ensure_column(conn, "emotion_reflection_slots", "window_frozen_at", "TEXT")
    _ensure_column(conn, "emotion_reflection_slots", "previous_metrics_json", "TEXT")
    _ensure_column(conn, "emotion_reflection_slots", "current_metrics_json", "TEXT")
    _ensure_column(conn, "emotion_reflection_slots", "previous_message_ids_json", "TEXT")
    _ensure_column(conn, "emotion_reflection_slots", "current_message_ids_json", "TEXT")
    _ensure_column(conn, "emotion_reflection_slots", "input_message_ids_json", "TEXT")
    _ensure_column(conn, "emotion_reflection_slots", "previous_messages_json", "TEXT")
    _ensure_column(conn, "emotion_reflection_slots", "current_messages_json", "TEXT")
    conn.execute("DROP INDEX IF EXISTS idx_emotion_slots_scope_slot")
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_emotion_slots_scope_slot
        ON emotion_reflection_slots(
            group_id, session_id, discussion_id, slot_index, prompt_version
        )
    """)
    conn.execute("DROP INDEX IF EXISTS idx_emotion_slots_one_active")
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_emotion_slots_one_active
        ON emotion_reflection_slots(discussion_id)
        WHERE status IN ('pending','running','deferred')
    """)
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_emotion_slots_due
        ON emotion_reflection_slots(status, next_retry_at, enqueued_at)
    """)
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_emotion_slots_discussion
        ON emotion_reflection_slots(discussion_id, slot_index)
    """)
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_emotion_slots_message
        ON emotion_reflection_slots(message_id)
    """)
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_emotion_slots_intervention
        ON emotion_reflection_slots(intervention_run_id)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS emotion_feedback_assessments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slot_id INTEGER NOT NULL UNIQUE,
            group_id INTEGER NOT NULL,
            session_id INTEGER NOT NULL,
            discussion_id INTEGER NOT NULL,
            slot_index INTEGER NOT NULL,
            prompt_version TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'prepared',
            model_name TEXT,
            emotion_feedback_state TEXT,
            confidence REAL,
            comparison_summary TEXT,
            current_window_summary TEXT,
            previous_window_summary TEXT,
            previous_metrics_json TEXT NOT NULL DEFAULT '{}',
            current_metrics_json TEXT NOT NULL DEFAULT '{}',
            input_message_ids_json TEXT NOT NULL DEFAULT '[]',
            evidence_message_ids_json TEXT NOT NULL DEFAULT '[]',
            raw_response_json TEXT,
            failure_reason TEXT,
            validation_status TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            started_at TEXT,
            completed_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(slot_id) REFERENCES emotion_reflection_slots(id) ON DELETE CASCADE,
            FOREIGN KEY(group_id) REFERENCES groups(id) ON DELETE CASCADE,
            FOREIGN KEY(session_id) REFERENCES experiment_sessions(id) ON DELETE CASCADE,
            FOREIGN KEY(discussion_id) REFERENCES group_session_discussions(id) ON DELETE CASCADE
        )
    """)
    _ensure_column(conn, "emotion_feedback_assessments", "validation_status", "TEXT")
    _ensure_column(
        conn,
        "emotion_feedback_assessments",
        "attempt_count",
        "INTEGER NOT NULL DEFAULT 0",
    )
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_emotion_assessment_state_insert
        BEFORE INSERT ON emotion_feedback_assessments
        WHEN NEW.emotion_feedback_state IS NOT NULL
         AND NEW.emotion_feedback_state NOT IN (
             'GROUP_EXCELLENT',
             'GROUP_IMPROVING',
             'GROUP_DECLINING',
             'GROUP_LOW_PARTICIPATION',
             'GROUP_SUSTAINED_EXCELLENT'
         )
        BEGIN
            SELECT RAISE(ABORT, 'invalid emotion_feedback_state');
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_emotion_assessment_state_update
        BEFORE UPDATE OF emotion_feedback_state ON emotion_feedback_assessments
        WHEN NEW.emotion_feedback_state IS NOT NULL
         AND NEW.emotion_feedback_state NOT IN (
             'GROUP_EXCELLENT',
             'GROUP_IMPROVING',
             'GROUP_DECLINING',
             'GROUP_LOW_PARTICIPATION',
             'GROUP_SUSTAINED_EXCELLENT'
         )
        BEGIN
            SELECT RAISE(ABORT, 'invalid emotion_feedback_state');
        END
    """)
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_emotion_assessments_scope
        ON emotion_feedback_assessments(
            group_id, session_id, discussion_id, slot_index
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS emotion_feedback_generations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slot_id INTEGER NOT NULL,
            assessment_id INTEGER,
            attempt_no INTEGER NOT NULL DEFAULT 1,
            prompt_version TEXT NOT NULL,
            emotion_feedback_state TEXT,
            reference_template_ids_json TEXT NOT NULL DEFAULT '[]',
            model_name TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            input_snapshot_json TEXT,
            raw_response_json TEXT,
            final_text TEXT,
            fallback_used INTEGER NOT NULL DEFAULT 0,
            validation_status TEXT,
            failure_reason TEXT,
            published_message_id INTEGER,
            started_at TEXT,
            completed_at TEXT,
            published_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(slot_id) REFERENCES emotion_reflection_slots(id) ON DELETE CASCADE,
            FOREIGN KEY(assessment_id) REFERENCES emotion_feedback_assessments(id) ON DELETE SET NULL,
            FOREIGN KEY(published_message_id) REFERENCES messages(id) ON DELETE SET NULL,
            UNIQUE(slot_id, attempt_no)
        )
    """)
    _ensure_column(
        conn, "emotion_feedback_generations", "emotion_feedback_state", "TEXT"
    )
    _ensure_column(
        conn,
        "emotion_feedback_generations",
        "reference_template_ids_json",
        "TEXT NOT NULL DEFAULT '[]'",
    )
    _ensure_column(conn, "emotion_feedback_generations", "published_at", "TEXT")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_emotion_generations_assessment
        ON emotion_feedback_generations(assessment_id, attempt_no)
    """)


def _migration_incremental_state_assessment_tables(conn):
    """Create discussion-scoped incremental assessment batches and cursors."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS state_assessment_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            session_id INTEGER NOT NULL,
            discussion_id INTEGER NOT NULL,
            candidate_start_sequence INTEGER NOT NULL,
            candidate_end_sequence INTEGER NOT NULL,
            context_start_sequence INTEGER,
            context_end_sequence INTEGER,
            trigger_type TEXT NOT NULL,
            trigger_sequence INTEGER,
            window_key TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','running','succeeded','failed','superseded')),
            rerun_requested INTEGER NOT NULL DEFAULT 0
                CHECK(rerun_requested IN (0,1)),
            request_priority INTEGER NOT NULL DEFAULT 0,
            last_trigger_sequence INTEGER,
            continuation_trigger_type TEXT,
            continuation_trigger_sequence INTEGER,
            continuation_request_priority INTEGER NOT NULL DEFAULT 0,
            continuation_candidate_start_sequence INTEGER,
            continuation_candidate_end_sequence INTEGER,
            continuation_replacement_of_pipeline_run_id INTEGER,
            continuation_replacement_reason TEXT,
            continuation_replacement_trigger_message_id INTEGER,
            continuation_replacement_cutoff_sequence INTEGER,
            replacement_of_pipeline_run_id INTEGER,
            replacement_reason TEXT,
            replacement_trigger_message_id INTEGER,
            replacement_cutoff_sequence INTEGER,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 2,
            next_retry_at TEXT,
            enqueued_at TEXT,
            model TEXT,
            prompt_version TEXT,
            raw_response TEXT,
            parsed_response TEXT,
            error_code TEXT,
            error_detail TEXT,
            student_sequences_json TEXT,
            terminal_status TEXT,
            terminal_at TEXT,
            fallback_action TEXT,
            fallback_segment_count INTEGER NOT NULL DEFAULT 0,
            started_at TEXT,
            completed_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            CHECK(candidate_start_sequence <= candidate_end_sequence),
            UNIQUE(
                group_id, session_id, discussion_id,
                candidate_start_sequence, candidate_end_sequence
            ),
            FOREIGN KEY(group_id) REFERENCES groups(id),
            FOREIGN KEY(session_id) REFERENCES experiment_sessions(id),
            FOREIGN KEY(discussion_id) REFERENCES group_session_discussions(id)
        )
    """)
    _ensure_column(conn, "state_assessment_batches", "request_priority", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "state_assessment_batches", "last_trigger_sequence", "INTEGER")
    _ensure_column(conn, "state_assessment_batches", "continuation_trigger_type", "TEXT")
    _ensure_column(conn, "state_assessment_batches", "continuation_trigger_sequence", "INTEGER")
    _ensure_column(
        conn,
        "state_assessment_batches",
        "continuation_request_priority",
        "INTEGER NOT NULL DEFAULT 0",
    )
    _ensure_column(
        conn,
        "state_assessment_batches",
        "continuation_candidate_start_sequence",
        "INTEGER",
    )
    _ensure_column(
        conn,
        "state_assessment_batches",
        "continuation_candidate_end_sequence",
        "INTEGER",
    )
    _ensure_column(
        conn,
        "state_assessment_batches",
        "continuation_replacement_of_pipeline_run_id",
        "INTEGER",
    )
    _ensure_column(
        conn,
        "state_assessment_batches",
        "continuation_replacement_reason",
        "TEXT",
    )
    _ensure_column(
        conn,
        "state_assessment_batches",
        "continuation_replacement_trigger_message_id",
        "INTEGER",
    )
    _ensure_column(
        conn,
        "state_assessment_batches",
        "continuation_replacement_cutoff_sequence",
        "INTEGER",
    )
    _ensure_column(conn, "state_assessment_batches", "replacement_of_pipeline_run_id", "INTEGER")
    _ensure_column(conn, "state_assessment_batches", "replacement_reason", "TEXT")
    _ensure_column(conn, "state_assessment_batches", "replacement_trigger_message_id", "INTEGER")
    _ensure_column(conn, "state_assessment_batches", "replacement_cutoff_sequence", "INTEGER")
    _ensure_column(conn, "state_assessment_batches", "attempt_count", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "state_assessment_batches", "max_attempts", "INTEGER NOT NULL DEFAULT 2")
    _ensure_column(conn, "state_assessment_batches", "next_retry_at", "TEXT")
    _ensure_column(conn, "state_assessment_batches", "enqueued_at", "TEXT")
    _ensure_column(conn, "state_assessment_batches", "student_sequences_json", "TEXT")
    _ensure_column(conn, "state_assessment_batches", "terminal_status", "TEXT")
    _ensure_column(conn, "state_assessment_batches", "terminal_at", "TEXT")
    _ensure_column(conn, "state_assessment_batches", "fallback_action", "TEXT")
    _ensure_column(
        conn,
        "state_assessment_batches",
        "fallback_segment_count",
        "INTEGER NOT NULL DEFAULT 0",
    )
    _ensure_column(conn, "state_assessment_batches", "updated_at", "TEXT")
    # Older development databases could contain more than one active row from
    # before the single-flight constraint existed.  Keep the oldest claim and
    # supersede the rest before creating the partial unique index.
    conn.execute("""
        UPDATE state_assessment_batches
        SET status='superseded', completed_at=COALESCE(completed_at, CURRENT_TIMESTAMP),
            updated_at=CURRENT_TIMESTAMP
        WHERE status IN ('pending','running')
          AND id NOT IN (
              SELECT MIN(id)
              FROM state_assessment_batches
              WHERE status IN ('pending','running')
              GROUP BY group_id, session_id, discussion_id
          )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_state_assessment_batches_active
        ON state_assessment_batches(group_id, session_id, discussion_id, status)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_state_assessment_batches_recent_success
        ON state_assessment_batches(
            group_id, session_id, discussion_id, status, completed_at DESC, id DESC
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_state_assessment_batches_window_key
        ON state_assessment_batches(window_key)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_state_assessment_batches_status
        ON state_assessment_batches(status)
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_state_assessment_batches_one_active
        ON state_assessment_batches(group_id, session_id, discussion_id)
        WHERE status IN ('pending','running')
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_state_assessment_batches_retry
        ON state_assessment_batches(status, next_retry_at, attempt_count)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_state_assessment_batches_terminal
        ON state_assessment_batches(
            group_id, session_id, discussion_id, terminal_status, candidate_end_sequence
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_state_assessment_batches_replacement
        ON state_assessment_batches(replacement_of_pipeline_run_id, status)
        WHERE replacement_of_pipeline_run_id IS NOT NULL
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS discussion_assessment_cursors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            session_id INTEGER NOT NULL,
            discussion_id INTEGER NOT NULL,
            last_finalized_student_sequence INTEGER NOT NULL DEFAULT 0,
            last_scheduled_student_sequence INTEGER NOT NULL DEFAULT 0,
            last_assessment_requested_at TEXT,
            last_assessment_completed_at TEXT,
            last_scheduling_completed_at TEXT,
            last_intervention_sequence INTEGER,
            observation_started_sequence INTEGER,
            observation_status TEXT NOT NULL DEFAULT 'inactive'
                CHECK(observation_status IN ('inactive','observing')),
            updated_at TEXT NOT NULL,
            UNIQUE(group_id, session_id, discussion_id),
            FOREIGN KEY(group_id) REFERENCES groups(id),
            FOREIGN KEY(session_id) REFERENCES experiment_sessions(id),
            FOREIGN KEY(discussion_id) REFERENCES group_session_discussions(id)
        )
    """)
    _ensure_column(
        conn,
        "discussion_assessment_cursors",
        "last_scheduled_student_sequence",
        "INTEGER NOT NULL DEFAULT 0",
    )
    _ensure_column(
        conn,
        "discussion_assessment_cursors",
        "last_scheduling_completed_at",
        "TEXT",
    )
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_discussion_assessment_cursors_observation
        ON discussion_assessment_cursors(
            group_id, session_id, discussion_id, observation_status
        )
    """)


def _migration_collaboration_state_segments(conn):
    """Create normalized teacher-facing collaboration state segments."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS collaboration_state_segments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            session_id INTEGER,
            session_no INTEGER,
            task_id INTEGER,
            state_code TEXT NOT NULL,
            segment_kind TEXT NOT NULL CHECK(segment_kind IN ('message_range','time_range')),
            start_message_id INTEGER,
            end_message_id INTEGER,
            assessment_batch_id INTEGER,
            start_sequence INTEGER,
            end_sequence INTEGER,
            start_at TEXT,
            end_at TEXT,
            trigger_sequence INTEGER,
            raw_silence_started_at TEXT,
            threshold_reached_at TEXT,
            detected_at TEXT,
            last_observed_at TEXT,
            silent_seconds_at_detection INTEGER,
            is_active INTEGER NOT NULL DEFAULT 0 CHECK(is_active IN (0,1)),
            resolution_reason TEXT,
            silence_event_key TEXT,
            intervention_scheduled_at TEXT,
            intervention_run_id INTEGER,
            intervention_published_at TEXT,
            intervention_disposition TEXT,
            evidence_message_ids_json TEXT NOT NULL DEFAULT '[]',
            evidence_sequences TEXT NOT NULL DEFAULT '[]',
            confidence REAL,
            fallback_reason TEXT,
            source TEXT NOT NULL CHECK(source IN (
                'strategy_llm','silence_rule','session_finalizer','state_monitor',
                'rule','llm','legacy'
            )),
            assessment_status TEXT,
            segment_order INTEGER,
            is_active_at_batch_end INTEGER CHECK(
                is_active_at_batch_end IS NULL OR is_active_at_batch_end IN (0,1)
            ),
            trigger_type TEXT,
            source_run_id INTEGER,
            assessment_id INTEGER,
            analysis_anchor_message_id INTEGER,
            analysis_window_start_message_id INTEGER,
            analysis_window_end_message_id INTEGER,
            previous_student_message_id INTEGER,
            next_student_message_id INTEGER,
            gap_seconds INTEGER,
            prompt_version TEXT,
            is_finalized INTEGER NOT NULL DEFAULT 0 CHECK(is_finalized IN (0,1)),
            dedupe_key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK(confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),
            CHECK(
                (
                    segment_kind='message_range'
                    AND start_message_id IS NOT NULL
                    AND end_message_id IS NOT NULL
                    AND start_at IS NULL
                    AND end_at IS NULL
                    AND previous_student_message_id IS NULL
                    AND next_student_message_id IS NULL
                    AND gap_seconds IS NULL
                )
                OR
                (
                    segment_kind='time_range'
                    AND start_message_id IS NULL
                    AND end_message_id IS NULL
                    AND start_at IS NOT NULL
                    AND (
                        (is_active=1 AND end_at IS NULL)
                        OR
                        (is_active=0 AND end_at IS NOT NULL)
                    )
                )
            ),
            FOREIGN KEY(group_id) REFERENCES groups(id),
            FOREIGN KEY(assessment_batch_id) REFERENCES state_assessment_batches(id)
        )
    """)
    _ensure_column(conn, "collaboration_state_segments", "assessment_batch_id", "INTEGER")
    _ensure_column(conn, "collaboration_state_segments", "start_sequence", "INTEGER")
    _ensure_column(conn, "collaboration_state_segments", "end_sequence", "INTEGER")
    _ensure_column(
        conn,
        "collaboration_state_segments",
        "evidence_sequences",
        "TEXT NOT NULL DEFAULT '[]'",
    )
    _ensure_column(conn, "collaboration_state_segments", "fallback_reason", "TEXT")
    _ensure_column(conn, "collaboration_state_segments", "assessment_status", "TEXT")
    _ensure_column(conn, "collaboration_state_segments", "segment_order", "INTEGER")
    _ensure_column(conn, "collaboration_state_segments", "is_active_at_batch_end", "INTEGER")
    _ensure_column(conn, "collaboration_state_segments", "trigger_type", "TEXT")
    _ensure_column(conn, "collaboration_state_segments", "trigger_sequence", "INTEGER")
    _ensure_column(conn, "collaboration_state_segments", "raw_silence_started_at", "TEXT")
    _ensure_column(conn, "collaboration_state_segments", "threshold_reached_at", "TEXT")
    _ensure_column(conn, "collaboration_state_segments", "detected_at", "TEXT")
    _ensure_column(conn, "collaboration_state_segments", "last_observed_at", "TEXT")
    _ensure_column(conn, "collaboration_state_segments", "silent_seconds_at_detection", "INTEGER")
    _ensure_column(
        conn,
        "collaboration_state_segments",
        "is_active",
        "INTEGER NOT NULL DEFAULT 0",
    )
    _ensure_column(conn, "collaboration_state_segments", "resolution_reason", "TEXT")
    _ensure_column(conn, "collaboration_state_segments", "silence_event_key", "TEXT")
    _ensure_column(conn, "collaboration_state_segments", "intervention_scheduled_at", "TEXT")
    _ensure_column(conn, "collaboration_state_segments", "intervention_run_id", "INTEGER")
    _ensure_column(conn, "collaboration_state_segments", "intervention_published_at", "TEXT")
    _ensure_column(conn, "collaboration_state_segments", "intervention_disposition", "TEXT")
    _ensure_collaboration_state_segments_source_check(conn)
    # Legacy names are misleading: these columns contain messages.sequence.
    # Backfill explicit aliases without changing source provenance or old reads.
    conn.execute("""
        UPDATE collaboration_state_segments
        SET start_sequence=COALESCE(start_sequence, start_message_id),
            end_sequence=COALESCE(end_sequence, end_message_id),
            evidence_sequences=CASE
                WHEN evidence_sequences IS NULL OR evidence_sequences='[]'
                    THEN COALESCE(evidence_message_ids_json, '[]')
                ELSE evidence_sequences
            END,
            assessment_status=COALESCE(
                assessment_status,
                CASE WHEN is_finalized=1 THEN 'confirmed' ELSE 'candidate' END
            )
        WHERE segment_kind='message_range'
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_collab_state_segments_group_session
        ON collaboration_state_segments(group_id, session_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_collab_state_segments_group_session_no
        ON collaboration_state_segments(group_id, session_no)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_collab_state_segments_state
        ON collaboration_state_segments(state_code)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_collab_state_segments_message_range
        ON collaboration_state_segments(start_message_id, end_message_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_collab_state_segments_time_range
        ON collaboration_state_segments(start_at, end_at)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_collab_state_segments_source_run
        ON collaboration_state_segments(source_run_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_collab_state_segments_anchor
        ON collaboration_state_segments(group_id, session_id, analysis_anchor_message_id, is_finalized)
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_collab_state_segments_dedupe
        ON collaboration_state_segments(dedupe_key)
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_collab_state_segments_silence_event
        ON collaboration_state_segments(silence_event_key)
        WHERE silence_event_key IS NOT NULL
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_collab_state_segments_batch_order
        ON collaboration_state_segments(assessment_batch_id, segment_order)
        WHERE assessment_batch_id IS NOT NULL AND segment_order IS NOT NULL
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_collab_state_segments_sequence_range
        ON collaboration_state_segments(group_id, session_id, start_sequence, end_sequence)
    """)


def _ensure_collaboration_state_segments_source_check(conn):
    """Allow the state monitor to own teacher-facing segment rows.

    SQLite cannot alter a CHECK constraint in place, so rebuild only when an
    existing table still has the old source enum. The column set is unchanged.
    """
    row = conn.execute(
        """
        SELECT sql
        FROM sqlite_master
        WHERE type='table' AND name='collaboration_state_segments'
        """
    ).fetchone()
    table_sql = row[0] if row else ""
    required_sql_fragments = (
        "'state_monitor'",
        "'llm'",
        "assessment_batch_id",
        "start_sequence",
        "evidence_sequences",
        "trigger_sequence",
        "last_observed_at",
        "is_active=1 AND end_at IS NULL",
    )
    if all(fragment in (table_sql or "") for fragment in required_sql_fragments):
        return

    columns = [
        "id",
        "group_id",
        "session_id",
        "session_no",
        "task_id",
        "state_code",
        "segment_kind",
        "start_message_id",
        "end_message_id",
        "assessment_batch_id",
        "start_sequence",
        "end_sequence",
        "start_at",
        "end_at",
        "trigger_sequence",
        "raw_silence_started_at",
        "threshold_reached_at",
        "detected_at",
        "last_observed_at",
        "silent_seconds_at_detection",
        "is_active",
        "resolution_reason",
        "silence_event_key",
        "intervention_scheduled_at",
        "intervention_run_id",
        "intervention_published_at",
        "intervention_disposition",
        "evidence_message_ids_json",
        "evidence_sequences",
        "confidence",
        "source",
        "assessment_status",
        "segment_order",
        "is_active_at_batch_end",
        "trigger_type",
        "source_run_id",
        "assessment_id",
        "analysis_anchor_message_id",
        "analysis_window_start_message_id",
        "analysis_window_end_message_id",
        "previous_student_message_id",
        "next_student_message_id",
        "gap_seconds",
        "prompt_version",
        "is_finalized",
        "dedupe_key",
        "created_at",
        "updated_at",
    ]
    column_sql = ",".join(columns)
    conn.execute("ALTER TABLE collaboration_state_segments RENAME TO collaboration_state_segments_old_source_check")
    conn.execute("""
        CREATE TABLE collaboration_state_segments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            session_id INTEGER,
            session_no INTEGER,
            task_id INTEGER,
            state_code TEXT NOT NULL,
            segment_kind TEXT NOT NULL CHECK(segment_kind IN ('message_range','time_range')),
            start_message_id INTEGER,
            end_message_id INTEGER,
            assessment_batch_id INTEGER,
            start_sequence INTEGER,
            end_sequence INTEGER,
            start_at TEXT,
            end_at TEXT,
            trigger_sequence INTEGER,
            raw_silence_started_at TEXT,
            threshold_reached_at TEXT,
            detected_at TEXT,
            last_observed_at TEXT,
            silent_seconds_at_detection INTEGER,
            is_active INTEGER NOT NULL DEFAULT 0 CHECK(is_active IN (0,1)),
            resolution_reason TEXT,
            silence_event_key TEXT,
            intervention_scheduled_at TEXT,
            intervention_run_id INTEGER,
            intervention_published_at TEXT,
            intervention_disposition TEXT,
            evidence_message_ids_json TEXT NOT NULL DEFAULT '[]',
            evidence_sequences TEXT NOT NULL DEFAULT '[]',
            confidence REAL,
            source TEXT NOT NULL CHECK(source IN (
                'strategy_llm','silence_rule','session_finalizer','state_monitor',
                'rule','llm','legacy'
            )),
            assessment_status TEXT,
            segment_order INTEGER,
            is_active_at_batch_end INTEGER CHECK(
                is_active_at_batch_end IS NULL OR is_active_at_batch_end IN (0,1)
            ),
            trigger_type TEXT,
            source_run_id INTEGER,
            assessment_id INTEGER,
            analysis_anchor_message_id INTEGER,
            analysis_window_start_message_id INTEGER,
            analysis_window_end_message_id INTEGER,
            previous_student_message_id INTEGER,
            next_student_message_id INTEGER,
            gap_seconds INTEGER,
            prompt_version TEXT,
            is_finalized INTEGER NOT NULL DEFAULT 0 CHECK(is_finalized IN (0,1)),
            dedupe_key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK(confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),
            CHECK(
                (
                    segment_kind='message_range'
                    AND start_message_id IS NOT NULL
                    AND end_message_id IS NOT NULL
                    AND start_at IS NULL
                    AND end_at IS NULL
                    AND previous_student_message_id IS NULL
                    AND next_student_message_id IS NULL
                    AND gap_seconds IS NULL
                )
                OR
                (
                    segment_kind='time_range'
                    AND start_message_id IS NULL
                    AND end_message_id IS NULL
                    AND start_at IS NOT NULL
                    AND (
                        (is_active=1 AND end_at IS NULL)
                        OR
                        (is_active=0 AND end_at IS NOT NULL)
                    )
                )
            ),
            FOREIGN KEY(group_id) REFERENCES groups(id),
            FOREIGN KEY(assessment_batch_id) REFERENCES state_assessment_batches(id)
        )
    """)
    conn.execute(
        f"""
        INSERT INTO collaboration_state_segments({column_sql})
        SELECT {column_sql}
        FROM collaboration_state_segments_old_source_check
        """
    )
    conn.execute("DROP TABLE collaboration_state_segments_old_source_check")


def _migration_collaboration_state_finalizations(conn):
    """Create audit records for end-of-discussion state finalization."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS collaboration_state_finalizations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            session_id INTEGER,
            session_no INTEGER,
            task_id INTEGER,
            status TEXT NOT NULL CHECK(status IN ('pending','running','succeeded','failed')),
            reason TEXT,
            analysis_start_message_id INTEGER,
            analysis_end_message_id INTEGER,
            source_run_id INTEGER,
            started_at TEXT,
            completed_at TEXT,
            error TEXT,
            retry_count INTEGER NOT NULL DEFAULT 0,
            dedupe_key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(group_id) REFERENCES groups(id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_collab_state_finalizations_group_session
        ON collaboration_state_finalizations(group_id, session_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_collab_state_finalizations_status
        ON collaboration_state_finalizations(status)
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_collab_state_finalizations_dedupe
        ON collaboration_state_finalizations(dedupe_key)
    """)


def _migration_three_stage_pipeline_schema(conn):
    """Create additive three-stage audit tables and compatibility columns."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_pipeline_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_uuid TEXT NOT NULL UNIQUE,
            pipeline_mode TEXT NOT NULL DEFAULT 'strategy'
                CHECK(pipeline_mode IN ('strategy','state_only')),

            group_id INTEGER NOT NULL,
            session_id INTEGER,
            session_no INTEGER,
            discussion_id INTEGER,
            task_id INTEGER,

            trigger_source TEXT,
            trigger_event_id TEXT,
            trigger_message_id INTEGER,
            trigger_priority INTEGER,

            input_start_sequence INTEGER,
            input_end_sequence INTEGER,
            input_cutoff_student_sequence INTEGER,
            latest_sequence_at_publish INTEGER,
            trigger_level_state TEXT,
            latest_state TEXT,
            latest_should_intervene INTEGER CHECK(
                latest_should_intervene IS NULL OR latest_should_intervene IN (0,1)
            ),
            latest_state_pipeline_run_id INTEGER,

            stage1_status TEXT,
            stage1_started_at TEXT,
            stage1_completed_at TEXT,
            coarse_decision TEXT,
            coarse_state_code TEXT,
            coarse_risk_group TEXT,
            coarse_should_escalate INTEGER CHECK(
                coarse_should_escalate IS NULL OR coarse_should_escalate IN (0,1)
            ),
            coarse_confidence REAL,
            coarse_rule_scores_json TEXT,
            coarse_quantitative_features_json TEXT,
            coarse_evidence_message_ids_json TEXT NOT NULL DEFAULT '[]',
            coarse_reason_codes_json TEXT NOT NULL DEFAULT '[]',

            stage2_status TEXT,
            stage2_started_at TEXT,
            stage2_completed_at TEXT,
            raw_sub_state_code TEXT,
            canonical_sub_state_code TEXT,
            sub_category TEXT,
            secondary_sub_state_tags_json TEXT NOT NULL DEFAULT '[]',
            sub_state_confidence REAL,
            sub_state_reason TEXT,
            sub_state_start_sequence INTEGER,
            sub_state_end_sequence INTEGER,
            sub_state_evidence_message_ids_json TEXT NOT NULL DEFAULT '[]',
            evidence_message_ids_json TEXT NOT NULL DEFAULT '[]',
            all_state_segments_json TEXT NOT NULL DEFAULT '[]',
            detected_self_regulation INTEGER CHECK(
                detected_self_regulation IS NULL OR detected_self_regulation IN (0,1)
            ),
            should_intervene INTEGER CHECK(
                should_intervene IS NULL OR should_intervene IN (0,1)
            ),
            inhibition_strategy_id TEXT,
            inhibition_reason TEXT,
            fresh_detected_self_regulation INTEGER CHECK(
                fresh_detected_self_regulation IS NULL
                OR fresh_detected_self_regulation IN (0,1)
            ),
            suppression_type TEXT,
            suppression_strategy_id TEXT,
            suppression_evidence_message_ids_json TEXT NOT NULL DEFAULT '[]',
            suppression_source_batch_id INTEGER,
            suppression_source_segment_id INTEGER,
            suppression_decision_reason TEXT,
            suppression_decision_at TEXT,
            state_model_name TEXT,
            state_model_version TEXT,
            state_prompt_version TEXT,
            state_raw_response_json TEXT,

            stage3_status TEXT,
            stage3_started_at TEXT,
            stage3_completed_at TEXT,
            strategy_candidate_ids_json TEXT NOT NULL DEFAULT '[]',
            strategy_pool_json TEXT NOT NULL DEFAULT '[]',
            selected_strategy_id TEXT,
            selected_strategy_name TEXT,
            selected_strategy_type TEXT,
            selected_strategy_json TEXT,
            supporting_strategy_ids_json TEXT NOT NULL DEFAULT '[]',
            matched_trigger_features_json TEXT NOT NULL DEFAULT '[]',
            inapplicable_candidate_ids_json TEXT NOT NULL DEFAULT '[]',
            strategy_selection_reason TEXT,
            strategy_application_plan TEXT,
            strategy_library_version TEXT,
            strategy_library_hash TEXT,
            strategy_source TEXT,
            strategy_model_name TEXT,
            strategy_model_version TEXT,
            strategy_prompt_version TEXT,
            strategy_raw_response_json TEXT,

            generated_intervention_text TEXT,
            validated_intervention_text TEXT,
            text_validation_result_json TEXT,

            room_lock_token TEXT,
            room_lock_acquired_at TEXT,
            room_lock_released_at TEXT,

            publish_status TEXT,
            published_message_id INTEGER,
            published_at TEXT,

            observation_status TEXT,
            observation_started_at TEXT,
            observation_first_response_sequence INTEGER,
            observation_first_response_seconds REAL,
            observation_window_start_sequence INTEGER,
            observation_window_end_sequence INTEGER,
            observation_completed_at TEXT,
            observation_result TEXT,
            observation_assessment_run_id INTEGER,
            observation_assessment_batch_id INTEGER,
            observation_reintervention_run_id INTEGER,
            observation_previous_sub_state_code TEXT,
            observation_current_sub_state_code TEXT,
            observation_details_json TEXT NOT NULL DEFAULT '{}',

            final_status TEXT,
            skip_reason TEXT,
            failure_code TEXT,
            failure_detail TEXT,

            parent_run_id INTEGER,
            superseded_by_run_id INTEGER,
            replaced_by_pipeline_run_id INTEGER,
            replacement_reason TEXT,
            replacement_trigger_message_id INTEGER,
            replacement_cutoff_sequence INTEGER,
            idempotency_key TEXT,

            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

            CHECK(coarse_confidence IS NULL OR (coarse_confidence >= 0.0 AND coarse_confidence <= 1.0)),
            CHECK(sub_state_confidence IS NULL OR (sub_state_confidence >= 0.0 AND sub_state_confidence <= 1.0)),
            CHECK(input_start_sequence IS NULL OR input_end_sequence IS NULL OR input_start_sequence <= input_end_sequence),
            CHECK(sub_state_start_sequence IS NULL OR sub_state_end_sequence IS NULL OR sub_state_start_sequence <= sub_state_end_sequence),
            CHECK(
                UPPER(COALESCE(stage2_status, '')) != 'SUCCEEDED'
                OR TRIM(COALESCE(canonical_sub_state_code, '')) != ''
            ),
            CHECK(
                should_intervene IS NULL
                OR should_intervene != 1
                OR (
                    TRIM(COALESCE(sub_state_evidence_message_ids_json, '')) != ''
                    AND sub_state_evidence_message_ids_json != '[]'
                )
            ),
            CHECK(
                UPPER(COALESCE(stage3_status, '')) != 'SUCCEEDED'
                OR TRIM(COALESCE(selected_strategy_id, '')) != ''
            ),
            CHECK(
                UPPER(COALESCE(publish_status, '')) != 'PUBLISHED'
                OR (
                    TRIM(COALESCE(canonical_sub_state_code, '')) != ''
                    AND TRIM(COALESCE(selected_strategy_id, '')) != ''
                    AND TRIM(COALESCE(validated_intervention_text, '')) != ''
                    AND published_message_id IS NOT NULL
                )
            ),
            CHECK(
                COALESCE(inhibition_strategy_id, selected_strategy_id, '') NOT LIKE 'OI-%'
                OR (
                    COALESCE(should_intervene, 0) = 0
                    AND UPPER(COALESCE(publish_status, '')) != 'PUBLISHED'
                )
            ),
            CHECK(
                should_intervene IS NULL
                OR should_intervene != 0
                OR (
                    TRIM(COALESCE(generated_intervention_text, '')) = ''
                    AND TRIM(COALESCE(validated_intervention_text, '')) = ''
                    AND published_message_id IS NULL
                    AND UPPER(COALESCE(publish_status, '')) != 'PUBLISHED'
                )
            ),
            FOREIGN KEY(group_id) REFERENCES groups(id),
            FOREIGN KEY(session_id) REFERENCES experiment_sessions(id),
            FOREIGN KEY(discussion_id) REFERENCES group_session_discussions(id),
            FOREIGN KEY(parent_run_id) REFERENCES strategy_pipeline_runs(id),
            FOREIGN KEY(superseded_by_run_id) REFERENCES strategy_pipeline_runs(id)
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_strategy_pipeline_runs_idempotency
        ON strategy_pipeline_runs(idempotency_key)
        WHERE idempotency_key IS NOT NULL
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_strategy_pipeline_runs_scope_status
        ON strategy_pipeline_runs(group_id, session_id, discussion_id, final_status)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_strategy_pipeline_runs_cutoff
        ON strategy_pipeline_runs(group_id, session_id, discussion_id, input_cutoff_student_sequence)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_strategy_pipeline_runs_stage2_state
        ON strategy_pipeline_runs(canonical_sub_state_code, stage2_status)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_strategy_pipeline_runs_strategy
        ON strategy_pipeline_runs(selected_strategy_id, publish_status)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_strategy_pipeline_runs_state_strategy_audit
        ON strategy_pipeline_runs(sub_category, selected_strategy_id, publish_status)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_strategy_pipeline_runs_trigger
        ON strategy_pipeline_runs(trigger_source, trigger_message_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_strategy_pipeline_runs_superseded
        ON strategy_pipeline_runs(parent_run_id, superseded_by_run_id)
    """)
    _ensure_column(conn, "strategy_pipeline_runs", "observation_status", "TEXT")
    _ensure_column(
        conn,
        "strategy_pipeline_runs",
        "pipeline_mode",
        "TEXT NOT NULL DEFAULT 'strategy' CHECK(pipeline_mode IN ('strategy','state_only'))",
    )
    _ensure_column(conn, "strategy_pipeline_runs", "trigger_level_state", "TEXT")
    _ensure_column(conn, "strategy_pipeline_runs", "latest_state", "TEXT")
    _ensure_column(
        conn,
        "strategy_pipeline_runs",
        "latest_should_intervene",
        "INTEGER CHECK(latest_should_intervene IS NULL OR latest_should_intervene IN (0,1))",
    )
    _ensure_column(conn, "strategy_pipeline_runs", "latest_state_pipeline_run_id", "INTEGER")
    _ensure_column(conn, "strategy_pipeline_runs", "replaced_by_pipeline_run_id", "INTEGER")
    _ensure_column(conn, "strategy_pipeline_runs", "replacement_reason", "TEXT")
    _ensure_column(conn, "strategy_pipeline_runs", "replacement_trigger_message_id", "INTEGER")
    _ensure_column(conn, "strategy_pipeline_runs", "replacement_cutoff_sequence", "INTEGER")
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_strategy_pipeline_runs_replacement
        ON strategy_pipeline_runs(replaced_by_pipeline_run_id, parent_run_id)
    """)
    _ensure_column(
        conn,
        "strategy_pipeline_runs",
        "fresh_detected_self_regulation",
        "INTEGER CHECK(fresh_detected_self_regulation IS NULL OR fresh_detected_self_regulation IN (0,1))",
    )
    _ensure_column(conn, "strategy_pipeline_runs", "suppression_type", "TEXT")
    _ensure_column(conn, "strategy_pipeline_runs", "suppression_strategy_id", "TEXT")
    _ensure_column(
        conn,
        "strategy_pipeline_runs",
        "suppression_evidence_message_ids_json",
        "TEXT NOT NULL DEFAULT '[]'",
    )
    _ensure_column(conn, "strategy_pipeline_runs", "suppression_source_batch_id", "INTEGER")
    _ensure_column(conn, "strategy_pipeline_runs", "suppression_source_segment_id", "INTEGER")
    _ensure_column(conn, "strategy_pipeline_runs", "suppression_decision_reason", "TEXT")
    _ensure_column(conn, "strategy_pipeline_runs", "suppression_decision_at", "TEXT")
    _ensure_column(conn, "strategy_pipeline_runs", "sub_category", "TEXT")
    _ensure_column(
        conn,
        "strategy_pipeline_runs",
        "evidence_message_ids_json",
        "TEXT NOT NULL DEFAULT '[]'",
    )
    _ensure_column(
        conn,
        "strategy_pipeline_runs",
        "strategy_pool_json",
        "TEXT NOT NULL DEFAULT '[]'",
    )
    _ensure_column(conn, "strategy_pipeline_runs", "selected_strategy_json", "TEXT")
    _ensure_column(conn, "strategy_pipeline_runs", "strategy_source", "TEXT")
    _ensure_column(conn, "strategy_pipeline_runs", "observation_started_at", "TEXT")
    _ensure_column(
        conn,
        "strategy_pipeline_runs",
        "observation_first_response_sequence",
        "INTEGER",
    )
    _ensure_column(
        conn,
        "strategy_pipeline_runs",
        "observation_first_response_seconds",
        "REAL",
    )
    _ensure_column(
        conn,
        "strategy_pipeline_runs",
        "observation_window_start_sequence",
        "INTEGER",
    )
    _ensure_column(
        conn,
        "strategy_pipeline_runs",
        "observation_window_end_sequence",
        "INTEGER",
    )
    _ensure_column(conn, "strategy_pipeline_runs", "observation_completed_at", "TEXT")
    _ensure_column(conn, "strategy_pipeline_runs", "observation_result", "TEXT")
    _ensure_column(
        conn,
        "strategy_pipeline_runs",
        "observation_assessment_run_id",
        "INTEGER",
    )
    _ensure_column(
        conn,
        "strategy_pipeline_runs",
        "observation_assessment_batch_id",
        "INTEGER",
    )
    _ensure_column(
        conn,
        "strategy_pipeline_runs",
        "observation_reintervention_run_id",
        "INTEGER",
    )
    _ensure_column(
        conn,
        "strategy_pipeline_runs",
        "observation_previous_sub_state_code",
        "TEXT",
    )
    _ensure_column(
        conn,
        "strategy_pipeline_runs",
        "observation_current_sub_state_code",
        "TEXT",
    )
    _ensure_column(
        conn,
        "strategy_pipeline_runs",
        "observation_details_json",
        "TEXT NOT NULL DEFAULT '{}'",
    )
    _ensure_column(
        conn,
        "strategy_pipeline_runs",
        "matched_trigger_features_json",
        "TEXT NOT NULL DEFAULT '[]'",
    )
    _ensure_column(
        conn,
        "strategy_pipeline_runs",
        "inapplicable_candidate_ids_json",
        "TEXT NOT NULL DEFAULT '[]'",
    )
    _ensure_column(conn, "strategy_pipeline_runs", "strategy_application_plan", "TEXT")
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_strategy_pipeline_runs_observation
        ON strategy_pipeline_runs(group_id, session_id, discussion_id, observation_status)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_strategy_pipeline_runs_suppression
        ON strategy_pipeline_runs(suppression_type, suppression_strategy_id, suppression_decision_at)
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_definitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_id TEXT NOT NULL,
            strategy_name TEXT NOT NULL,
            strategy_type TEXT NOT NULL,
            applicable_sub_states_json TEXT NOT NULL DEFAULT '[]',
            trigger_features_json TEXT NOT NULL DEFAULT '[]',
            template_examples_json TEXT NOT NULL DEFAULT '[]',
            cognitive_load TEXT,
            expected_effect TEXT,
            inappropriate_conditions_json TEXT NOT NULL DEFAULT '[]',
            should_intervene INTEGER NOT NULL DEFAULT 1 CHECK(should_intervene IN (0,1)),
            is_exclusive INTEGER NOT NULL DEFAULT 0 CHECK(is_exclusive IN (0,1)),
            priority INTEGER NOT NULL DEFAULT 0,
            version TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(strategy_id, version),
            CHECK(strategy_id NOT LIKE 'OI-%' OR (should_intervene=0 AND is_exclusive=1))
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_strategy_definitions_active
        ON strategy_definitions(is_active, strategy_id, version)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_strategy_definitions_type_priority
        ON strategy_definitions(strategy_type, priority)
    """)

    _ensure_column(conn, "collaboration_state_segments", "coarse_state_code", "TEXT")
    _ensure_column(conn, "collaboration_state_segments", "raw_sub_state_code", "TEXT")
    _ensure_column(conn, "collaboration_state_segments", "canonical_sub_state_code", "TEXT")
    _ensure_column(
        conn,
        "collaboration_state_segments",
        "secondary_tags_json",
        "TEXT NOT NULL DEFAULT '[]'",
    )
    _ensure_column(conn, "collaboration_state_segments", "sub_state_confidence", "REAL")
    _ensure_column(conn, "collaboration_state_segments", "strategy_pipeline_run_id", "INTEGER")
    _ensure_column(
        conn,
        "collaboration_state_segments",
        "should_intervene",
        "INTEGER CHECK(should_intervene IS NULL OR should_intervene IN (0,1))",
    )
    _ensure_column(conn, "collaboration_state_segments", "selected_strategy_id", "TEXT")
    _ensure_column(conn, "collaboration_state_segments", "strategy_library_version", "TEXT")
    _ensure_column(conn, "collaboration_state_segments", "source_stage", "TEXT")
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_collab_state_segments_three_stage_run
        ON collaboration_state_segments(strategy_pipeline_run_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_collab_state_segments_canonical_sub_state
        ON collaboration_state_segments(canonical_sub_state_code)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_collab_state_segments_selected_strategy
        ON collaboration_state_segments(selected_strategy_id)
    """)

    _ensure_column(conn, "intervention_runs", "strategy_pipeline_run_id", "INTEGER")
    _ensure_column(conn, "intervention_runs", "canonical_sub_state_code", "TEXT")
    _ensure_column(conn, "intervention_runs", "sub_category", "TEXT")
    _ensure_column(
        conn,
        "intervention_runs",
        "strategy_candidate_ids_json",
        "TEXT NOT NULL DEFAULT '[]'",
    )
    _ensure_column(
        conn,
        "intervention_runs",
        "strategy_pool_json",
        "TEXT NOT NULL DEFAULT '[]'",
    )
    _ensure_column(conn, "intervention_runs", "strategy_source", "TEXT")
    _ensure_column(conn, "intervention_runs", "strategy_selection_reason", "TEXT")
    _ensure_column(
        conn,
        "intervention_runs",
        "evidence_message_ids_json",
        "TEXT NOT NULL DEFAULT '[]'",
    )
    _ensure_column(conn, "intervention_runs", "input_cutoff_student_sequence", "INTEGER")
    _ensure_column(conn, "intervention_runs", "generated_text", "TEXT")
    _ensure_column(conn, "intervention_runs", "validated_text", "TEXT")
    _ensure_column(conn, "intervention_runs", "publish_status", "TEXT")
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_intervention_runs_three_stage_run
        ON intervention_runs(strategy_pipeline_run_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_intervention_runs_canonical_sub_state
        ON intervention_runs(canonical_sub_state_code)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_intervention_runs_publish_status
        ON intervention_runs(publish_status)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_intervention_runs_state_strategy_audit
        ON intervention_runs(sub_category, selected_strategy_id, publish_status)
    """)

    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_intervention_runs_three_stage_insert
        BEFORE INSERT ON intervention_runs
        FOR EACH ROW
        WHEN NEW.strategy_pipeline_run_id IS NOT NULL
        BEGIN
            SELECT CASE
                WHEN UPPER(COALESCE(NEW.publish_status, NEW.status, ''))='PUBLISHED'
                     AND TRIM(COALESCE(NEW.selected_strategy_id, ''))=''
                THEN RAISE(ABORT, 'three-stage publish requires selected_strategy_id')
            END;
            SELECT CASE
                WHEN UPPER(COALESCE(NEW.publish_status, NEW.status, ''))='PUBLISHED'
                     AND TRIM(COALESCE(NEW.canonical_sub_state_code, ''))=''
                THEN RAISE(ABORT, 'three-stage publish requires canonical_sub_state_code')
            END;
            SELECT CASE
                WHEN UPPER(COALESCE(NEW.publish_status, NEW.status, ''))='PUBLISHED'
                     AND COALESCE(NEW.selected_strategy_id, '') LIKE 'OI-%'
                THEN RAISE(ABORT, 'OI strategy runs cannot publish')
            END;
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_intervention_runs_three_stage_update
        BEFORE UPDATE OF strategy_pipeline_run_id, publish_status, status,
                         selected_strategy_id, canonical_sub_state_code
        ON intervention_runs
        FOR EACH ROW
        WHEN NEW.strategy_pipeline_run_id IS NOT NULL
        BEGIN
            SELECT CASE
                WHEN UPPER(COALESCE(NEW.publish_status, NEW.status, ''))='PUBLISHED'
                     AND TRIM(COALESCE(NEW.selected_strategy_id, ''))=''
                THEN RAISE(ABORT, 'three-stage publish requires selected_strategy_id')
            END;
            SELECT CASE
                WHEN UPPER(COALESCE(NEW.publish_status, NEW.status, ''))='PUBLISHED'
                     AND TRIM(COALESCE(NEW.canonical_sub_state_code, ''))=''
                THEN RAISE(ABORT, 'three-stage publish requires canonical_sub_state_code')
            END;
            SELECT CASE
                WHEN UPPER(COALESCE(NEW.publish_status, NEW.status, ''))='PUBLISHED'
                     AND COALESCE(NEW.selected_strategy_id, '') LIKE 'OI-%'
                THEN RAISE(ABORT, 'OI strategy runs cannot publish')
            END;
        END
    """)


def _migration_three_stage_latency_events(conn):
    """Create additive high-resolution timing events without changing behavior."""
    _ensure_column(conn, "strategy_pipeline_runs", "assessment_batch_id", "INTEGER")
    _ensure_column(
        conn,
        "strategy_pipeline_runs",
        "assessment_owner_pipeline_run_id",
        "INTEGER",
    )
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_strategy_pipeline_runs_assessment_batch
        ON strategy_pipeline_runs(assessment_batch_id)
        WHERE assessment_batch_id IS NOT NULL
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_strategy_pipeline_runs_assessment_owner
        ON strategy_pipeline_runs(
            assessment_batch_id, assessment_owner_pipeline_run_id
        )
        WHERE assessment_batch_id IS NOT NULL
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_pipeline_latency_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER,
            session_id INTEGER,
            discussion_id INTEGER,
            task_id INTEGER,
            pipeline_run_id INTEGER,
            assessment_batch_id INTEGER,
            cutoff_sequence INTEGER,
            lock_owner INTEGER,
            lock_token_hash TEXT,
            call_id TEXT,
            attempt INTEGER,
            stage TEXT NOT NULL,
            event TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            elapsed_ms REAL NOT NULL DEFAULT 0 CHECK(elapsed_ms >= 0),
            details_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY(pipeline_run_id) REFERENCES strategy_pipeline_runs(id),
            FOREIGN KEY(assessment_batch_id) REFERENCES state_assessment_batches(id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_pipeline_latency_events_pipeline
        ON strategy_pipeline_latency_events(pipeline_run_id, occurred_at, id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_pipeline_latency_events_batch
        ON strategy_pipeline_latency_events(assessment_batch_id, occurred_at, id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_pipeline_latency_events_event
        ON strategy_pipeline_latency_events(event, occurred_at)
    """)


def _ensure_column(conn, table, column, col_type):

    existing = {
        row[1]
        for row in conn.execute(
            f"PRAGMA table_info('{table}')"
        ).fetchall()
    }
    if column not in existing:
        conn.execute("ALTER TABLE {} ADD COLUMN {} {}".format(table, column, col_type))


def _migration_login_key_lookup(conn):
    """Add indexed HMAC lookup fields for experiment key login."""
    _ensure_column(conn, "experiment_participants", "key_lookup_hash", "TEXT")
    _ensure_column(conn, "teacher_access_keys", "key_lookup_hash", "TEXT")
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_exp_participants_lookup_active
        ON experiment_participants(key_lookup_hash)
        WHERE is_active=1 AND key_lookup_hash IS NOT NULL
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_teacher_access_keys_lookup_active
        ON teacher_access_keys(key_lookup_hash)
        WHERE is_active=1 AND key_lookup_hash IS NOT NULL
    """)


# ============================================================
# Batch 6 migration: V2 intervention pipeline
# ============================================================

def _migration_v004_batch6_intervention_v2(conn):
    """Batch 6: add V2 intervention pipeline columns to intervention_runs and groups."""
    _ensure_column(conn, "intervention_runs", "state_assessment_id", "INTEGER")
    _ensure_column(conn, "intervention_runs", "session_id", "INTEGER")
    _ensure_column(conn, "intervention_runs", "task_id", "INTEGER")
    _ensure_column(conn, "intervention_runs", "agent_type", "TEXT DEFAULT 'strategy'")
    _ensure_column(conn, "intervention_runs", "trigger_type", "TEXT")
    _ensure_column(conn, "intervention_runs", "decision", "TEXT")
    _ensure_column(conn, "intervention_runs", "teacher_reason", "TEXT")
    _ensure_column(conn, "intervention_runs", "message_id", "INTEGER")
    _ensure_column(conn, "intervention_runs", "help_request_id", "INTEGER")
    _ensure_column(conn, "intervention_runs", "lock_acquired", "INTEGER DEFAULT 0")
    _ensure_column(conn, "intervention_runs", "cooldown_result", "TEXT")
    _ensure_column(conn, "intervention_runs", "dry_run", "INTEGER DEFAULT 0")
    _ensure_column(conn, "intervention_runs", "metadata_json", "TEXT")
    _ensure_column(conn, "intervention_runs", "validation_json", "TEXT")
    _ensure_column(conn, "intervention_runs", "revalidation_json", "TEXT")
    _ensure_column(conn, "intervention_runs", "lock_expires_at", "TEXT")
    _ensure_column(conn, "intervention_runs", "revalidated_at", "TEXT")
    _ensure_column(conn, "intervention_runs", "generated_at", "TEXT")
    _ensure_column(conn, "intervention_runs", "validated_at", "TEXT")
    _ensure_column(conn, "intervention_runs", "published_at", "TEXT")
    _ensure_column(conn, "intervention_runs", "fallback_template", "TEXT")
    _ensure_column(conn, "intervention_runs", "validation_result", "TEXT")
    _ensure_column(conn, "intervention_runs", "generator_params_json", "TEXT")
    conn.execute("DROP INDEX IF EXISTS idx_intervention_runs_group_cutoff")
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_intervention_runs_group_cutoff
        ON intervention_runs(group_id, COALESCE(cutoff_sequence, 0),
                             COALESCE(agent_type, 'strategy'),
                             COALESCE(trigger_type, 'auto_state'))
        WHERE status NOT IN ('CANCELLED', 'FAILED', 'EXPIRED', 'STALE')
          AND NOT (
              COALESCE(agent_type, '')='emotion'
              AND COALESCE(trigger_type, '')='emotion_time_slot'
          )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_intervention_runs_help_request_id
        ON intervention_runs(help_request_id)
        WHERE help_request_id IS NOT NULL
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_intervention_runs_assessment_scope
        ON intervention_runs(group_id, COALESCE(session_id, 0), state_assessment_id,
                             COALESCE(trigger_type, 'auto_state'), COALESCE(agent_type, 'strategy'))
        WHERE state_assessment_id IS NOT NULL
    """)
    _ensure_column(conn, "groups", "last_v2_intervention_run_id", "INTEGER")
    _ensure_column(conn, "messages", "intervention_run_id", "INTEGER")
    _ensure_column(conn, "messages", "trigger_source", "TEXT")


# ============================================================
# Batch 1: Collaborative document tables
# ============================================================

def _migration_batch1_collab_tables(conn):
    """Batch 1: create collaborative document tables and indexes."""
    conn.execute("""CREATE TABLE IF NOT EXISTS collaborative_documents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER NOT NULL REFERENCES groups(id),
        task_id INTEGER NOT NULL REFERENCES learning_tasks(id),
        session_no INTEGER NOT NULL DEFAULT 0,
        title TEXT,
        y_state BLOB,
        content_json TEXT,
        content_html TEXT,
        content_text TEXT,
        status TEXT NOT NULL DEFAULT 'editing' CHECK(status IN ('editing','returned','submitted','locked')),
        state_revision INTEGER NOT NULL DEFAULT 0,
        state_size_bytes INTEGER NOT NULL DEFAULT 0,
        created_by INTEGER NOT NULL REFERENCES users(id),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        submitted_at TEXT,
        UNIQUE(group_id, task_id, session_no)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS collaborative_document_checkpoints (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id INTEGER NOT NULL REFERENCES collaborative_documents(id) ON DELETE CASCADE,
        state_revision INTEGER NOT NULL DEFAULT 0,
        reason TEXT NOT NULL DEFAULT 'manual' CHECK(reason IN ('submitted','returned','manual')),
        y_state BLOB,
        content_json TEXT,
        content_html TEXT,
        content_text TEXT,
        created_by INTEGER NOT NULL REFERENCES users(id),
        created_at TEXT NOT NULL
    )""")
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_collab_docs_group_task_session
        ON collaborative_documents(group_id, task_id, session_no)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_collab_docs_status
        ON collaborative_documents(status)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_collab_checkpoints_doc_revision
        ON collaborative_document_checkpoints(document_id, state_revision)
    """)

register_migration(1, "batch1_collab_tables", _migration_batch1_collab_tables)

# ============================================================
# Teacher Console v1: Experiment session management tables
# ============================================================

def _migration_teacher_console_v1(conn):
    """Batch 1: create teacher experiment session management tables
    and add non-destructive columns to existing tables.
    All operations are idempotent.
    """
    # ============================================================
    # A. experiment_sessions
    # ============================================================
    conn.execute("""CREATE TABLE IF NOT EXISTS experiment_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_no INTEGER NOT NULL,
        session_role TEXT NOT NULL,
        task_id INTEGER,
        status TEXT NOT NULL DEFAULT 'draft',
        start_time TEXT,
        deadline TEXT,
        end_time TEXT,
        time_limit_minutes INTEGER DEFAULT 30,
        agent_detection_enabled INTEGER DEFAULT 1,
        agent_intervention_enabled INTEGER DEFAULT 1,
        created_by INTEGER,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_experiment_sessions_status
        ON experiment_sessions(status)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_experiment_sessions_role
        ON experiment_sessions(session_role)
    """)
    _ensure_unique_session_no_index(conn)

    # ============================================================
    # C. baseline_assignments
    # ============================================================
    conn.execute("""CREATE TABLE IF NOT EXISTS baseline_assignments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER NOT NULL,
        session_id INTEGER,
        baseline_metrics_json TEXT,
        matching_variables_json TEXT,
        matching_algorithm TEXT,
        random_seed INTEGER,
        pairing_id INTEGER,
        condition TEXT,
        balance_check_json TEXT,
        assigned_at TEXT,
        assigned_by INTEGER,
        frozen_at TEXT,
        allocation_locked INTEGER DEFAULT 0
    )""")
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_baseline_assignments_group
        ON baseline_assignments(group_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_baseline_assignments_session
        ON baseline_assignments(session_id)
    """)

    # ============================================================
    # D. audit_logs
    # ============================================================
    conn.execute("""CREATE TABLE IF NOT EXISTS audit_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        operator_id INTEGER,
        action_type TEXT NOT NULL,
        target_type TEXT,
        target_id INTEGER,
        before_value TEXT,
        after_value TEXT,
        reason TEXT,
        created_at TEXT NOT NULL
    )""")
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_audit_logs_operator
        ON audit_logs(operator_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_audit_logs_target
        ON audit_logs(target_type, target_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_audit_logs_action
        ON audit_logs(action_type)
    """)

    # ============================================================
    # E. safety_signals
    # ============================================================
    conn.execute("""CREATE TABLE IF NOT EXISTS safety_signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER NOT NULL,
        member_id INTEGER,
        session_id INTEGER,
        signal_type TEXT NOT NULL,
        trigger_content TEXT,
        severity TEXT NOT NULL DEFAULT 'info',
        handled_by INTEGER,
        resolution TEXT,
        created_at TEXT NOT NULL
    )""")
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_safety_signals_group
        ON safety_signals(group_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_safety_signals_severity
        ON safety_signals(severity)
    """)

    # ============================================================
    # F. group_session_controls
    # ============================================================
    conn.execute("""CREATE TABLE IF NOT EXISTS group_session_controls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER NOT NULL,
        session_id INTEGER NOT NULL,
        agent_paused INTEGER DEFAULT 0,
        session_paused INTEGER DEFAULT 0,
        pause_reason TEXT,
        paused_by INTEGER,
        paused_at TEXT,
        resumed_at TEXT
    )""")
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_group_session_controls_unique
        ON group_session_controls(group_id, session_id)
    """)

    # ============================================================
    # G. intervention_uptake
    # ============================================================
    conn.execute("""CREATE TABLE IF NOT EXISTS intervention_uptake (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        intervention_id INTEGER,
        group_id INTEGER NOT NULL,
        session_id INTEGER,
        auto_uptake_type TEXT,
        manual_uptake_type TEXT,
        first_reply_at TEXT,
        response_latency_seconds REAL,
        response_count_2min INTEGER,
        participant_count_2min INTEGER,
        target_ssrl_behavior TEXT,
        target_behavior_occurred INTEGER,
        target_behavior_at TEXT,
        state_before TEXT,
        state_after_3min TEXT,
        state_after_5min TEXT,
        corrected_by INTEGER,
        corrected_at TEXT
    )""")
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_intervention_uptake_group
        ON intervention_uptake(group_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_intervention_uptake_intervention
        ON intervention_uptake(intervention_id)
    """)

    # ============================================================
    # H. deliverable_scores
    # ============================================================
    conn.execute("""CREATE TABLE IF NOT EXISTS deliverable_scores (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        deliverable_id INTEGER,
        collaborative_document_id INTEGER,
        group_id INTEGER NOT NULL,
        session_id INTEGER,
        task_id INTEGER,
        rater_id INTEGER,
        rubric_dimension TEXT,
        score REAL,
        score_note TEXT,
        blinded INTEGER DEFAULT 1,
        scored_at TEXT
    )""")
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_deliverable_scores_group
        ON deliverable_scores(group_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_deliverable_scores_doc
        ON deliverable_scores(collaborative_document_id)
    """)

    # ============================================================
    # I. autonomous_regulation_events
    # ============================================================
    conn.execute("""CREATE TABLE IF NOT EXISTS autonomous_regulation_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER NOT NULL,
        session_id INTEGER,
        task_id INTEGER,
        event_type TEXT NOT NULL,
        evidence_message_ids_json TEXT,
        confidence REAL,
        detected_by TEXT,
        note TEXT,
        created_at TEXT NOT NULL
    )""")
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_autonomous_reg_events_group
        ON autonomous_regulation_events(group_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_autonomous_reg_events_type
        ON autonomous_regulation_events(event_type)
    """)


    # ============================================================
    # Non-destructive ALTER TABLE: autonomous_regulation_events
    # ============================================================
    _ensure_column(conn, "autonomous_regulation_events", "source_monitor_run_id", "INTEGER")
    _ensure_column(conn, "autonomous_regulation_events", "metadata_json", "TEXT")

    # ============================================================
    # Non-destructive ALTER TABLE: messages
    # ============================================================
    _ensure_column(conn, "messages", "session_id", "INTEGER")
    _ensure_column(conn, "messages", "char_len", "INTEGER")

    # ---- T3 participation stats: composite index on messages ----
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_group_session_created ON messages(group_id, session_id, created_at)")

    # ============================================================
    # Non-destructive ALTER TABLE: emotion_checkins
    # ============================================================
    _ensure_column(conn, "emotion_checkins", "session_id", "INTEGER")
    _ensure_column(conn, "emotion_checkins", "task_id", "INTEGER")

    # ============================================================
    # Non-destructive ALTER TABLE: state_assessments
    # ============================================================
    _ensure_column(conn, "state_assessments", "session_id", "INTEGER")
    _ensure_column(conn, "state_assessments", "window_start", "TEXT")
    _ensure_column(conn, "state_assessments", "window_end", "TEXT")
    _ensure_column(conn, "state_assessments", "valence_score", "REAL")
    _ensure_column(conn, "state_assessments", "interaction_activation_score", "REAL")

    # ============================================================
    # Non-destructive ALTER TABLE: intervention_logs
    # ============================================================
    _ensure_column(conn, "intervention_logs", "session_id", "INTEGER")
    _ensure_column(conn, "intervention_logs", "task_id", "INTEGER")
    _ensure_column(conn, "intervention_logs", "intervention_index", "INTEGER")
    _ensure_column(conn, "intervention_logs", "target_ssrl_behavior", "TEXT")
    _ensure_column(conn, "intervention_logs", "prompt_version", "TEXT")

    # ============================================================
    # Non-destructive ALTER TABLE: collaborative_documents
    # ============================================================
    _ensure_column(conn, "collaborative_documents", "session_id", "INTEGER")
    # Legacy document creation only stored task_id/session_no.  Backfill only
    # when that pair identifies exactly one experiment session; ambiguous rows
    # remain NULL and are handled conservatively by runtime/export fallbacks.
    conn.execute("""
        UPDATE collaborative_documents
           SET session_id = (
               SELECT MIN(es.id)
                 FROM experiment_sessions AS es
                WHERE es.task_id = collaborative_documents.task_id
                  AND es.session_no = collaborative_documents.session_no
           )
         WHERE session_id IS NULL
           AND 1 = (
               SELECT COUNT(*)
                 FROM experiment_sessions AS es
                WHERE es.task_id = collaborative_documents.task_id
                  AND es.session_no = collaborative_documents.session_no
           )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_collab_docs_session_group
        ON collaborative_documents(session_id, group_id)
    """)

    # ============================================================
    # Non-destructive ALTER TABLE: questionnaire_responses
    # ============================================================
    _ensure_column(conn, "questionnaire_responses", "session_id", "INTEGER")

    # ============================================================
    # Non-destructive ALTER TABLE: process_events
    # ============================================================
    _ensure_column(conn, "process_events", "session_id", "INTEGER")


    # ============================================================
    # Non-destructive ALTER TABLE: experiment_sessions (Batch 2)
    # ============================================================
    _ensure_column(conn, "experiment_sessions", "condition_frozen", "INTEGER DEFAULT 0")
    _ensure_column(conn, "experiment_sessions", "archived_at", "TEXT")
    _ensure_column(conn, "experiment_sessions", "title", "TEXT DEFAULT ''")
    _ensure_column(conn, "experiment_sessions", "description", "TEXT DEFAULT ''")
    _ensure_column(conn, "experiment_sessions", "deadline", "TEXT")
    _ensure_column(conn, "experiment_sessions", "time_limit_minutes", "INTEGER DEFAULT 30")
    _ensure_column(conn, "experiment_sessions", "questionnaire_set_id", "INTEGER")
    # ============================================================
    # Batch 3: Baseline assignments needs_review column
    # ============================================================
    _ensure_column(conn, "baseline_assignments", "needs_review", "INTEGER DEFAULT 0")
    _ensure_column(conn, "baseline_assignments", "review_notes", "TEXT")
    _ensure_column(conn, "baseline_assignments", "matching_variables_requested", "TEXT")
    # ---- batch 7: add session_id to intervention_decisions ----
    _ensure_column(conn, "intervention_decisions", "session_id", "INTEGER")

    # ============================================================
    # Batch 8: data quality check tables
    # ============================================================
    conn.execute("""CREATE TABLE IF NOT EXISTS data_quality_checks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL,
        group_id INTEGER NOT NULL,
        overall_status TEXT NOT NULL DEFAULT 'ok' CHECK(overall_status IN ('ok','review','critical')),
        checked_by INTEGER,
        checked_at TEXT NOT NULL,
        notes TEXT
    )""")
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_data_quality_checks_session
        ON data_quality_checks(session_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_data_quality_checks_group
        ON data_quality_checks(group_id)
    """)
    conn.execute("""CREATE TABLE IF NOT EXISTS data_quality_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        check_id INTEGER NOT NULL REFERENCES data_quality_checks(id) ON DELETE CASCADE,
        check_code TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'unknown' CHECK(status IN ('ok','review','critical','unknown')),
        issue_code TEXT,
        details_json TEXT,
        created_at TEXT NOT NULL
    )""")
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_data_quality_items_check
        ON data_quality_items(check_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_data_quality_items_code
        ON data_quality_items(check_code)
    """)
    # ============================================================
    # experiment_phases table
    # ============================================================
    conn.execute('''CREATE TABLE IF NOT EXISTS experiment_phases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        description TEXT DEFAULT '',
        default_agent_intervention_enabled INTEGER DEFAULT 1,
        is_active INTEGER DEFAULT 1,
        sort_order INTEGER DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )''')

    # ============================================================
    # learning_tasks new columns
    # ============================================================
    _ensure_column(conn, "learning_tasks", "task_type", "TEXT DEFAULT 'generic'")
    _ensure_column(conn, "learning_tasks", "experiment_phase_id", "INTEGER")
    _ensure_column(conn, "learning_tasks", "experiment_phase_name", "TEXT DEFAULT ''")
    _ensure_column(conn, "learning_tasks", "agent_intervention_enabled", "INTEGER DEFAULT 1")
    _ensure_column(conn, "learning_tasks", "task_schema_version", "INTEGER DEFAULT 2")
    _ensure_column(conn, "learning_tasks", "task_payload_json", "TEXT DEFAULT '{}'")


def _migration_agent_dual_switch(conn):
    """Batch: Add strategy/emotion agent dual switch and research log infrastructure.

    All operations are idempotent - safe for fresh databases and upgrades.
    """
    # 1. experiment_sessions: two new independent switches
    _ensure_column(conn, "experiment_sessions", "strategy_agent_enabled", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "experiment_sessions", "emotion_agent_enabled", "INTEGER NOT NULL DEFAULT 0")

    # 2. messages: agent_type and agent_event_id for traceability
    _ensure_column(conn, "messages", "agent_type", "TEXT")
    _ensure_column(conn, "messages", "agent_event_id", "INTEGER")

    # 3. intervention_runs: unified research tracking columns
    _ensure_column(conn, "intervention_runs", "agent_type", "TEXT DEFAULT 'strategy'")
    _ensure_column(conn, "intervention_runs", "trigger_type", "TEXT")
    _ensure_column(conn, "intervention_runs", "state_assessment_id", "INTEGER")
    _ensure_column(conn, "intervention_runs", "session_id", "INTEGER")
    _ensure_column(conn, "intervention_runs", "task_id", "INTEGER")
    _ensure_column(conn, "intervention_runs", "decision", "TEXT")
    _ensure_column(conn, "intervention_runs", "teacher_reason", "TEXT")
    _ensure_column(conn, "intervention_runs", "message_id", "INTEGER")
    _ensure_column(conn, "intervention_runs", "lock_acquired", "INTEGER DEFAULT 0")
    _ensure_column(conn, "intervention_runs", "cooldown_result", "TEXT")
    _ensure_column(conn, "intervention_runs", "trigger_reason_json", "TEXT")
    _ensure_column(conn, "intervention_runs", "llm_context_json", "TEXT")
    _ensure_column(conn, "intervention_runs", "llm_prompt_json", "TEXT")
    _ensure_column(conn, "intervention_runs", "llm_response_json", "TEXT")
    _ensure_column(conn, "intervention_runs", "teacher_config_snapshot_json", "TEXT")
    _ensure_column(conn, "intervention_runs", "window_start", "TEXT")
    _ensure_column(conn, "intervention_runs", "window_end", "TEXT")
    _ensure_column(conn, "intervention_runs", "scheduled_at", "TEXT")
    _ensure_column(conn, "intervention_runs", "actual_started_at", "TEXT")
    _ensure_column(conn, "intervention_runs", "actual_published_at", "TEXT")
    _ensure_column(conn, "intervention_runs", "skip_reason", "TEXT")
    conn.execute("UPDATE intervention_runs SET agent_type='strategy' WHERE agent_type IS NULL OR TRIM(agent_type)=''")
    conn.execute("DROP INDEX IF EXISTS idx_intervention_runs_group_cutoff")
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_intervention_runs_group_cutoff
        ON intervention_runs(group_id, COALESCE(cutoff_sequence, 0),
                             COALESCE(agent_type, 'strategy'),
                             COALESCE(trigger_type, 'auto_state'))
        WHERE status NOT IN ('CANCELLED', 'FAILED', 'EXPIRED', 'STALE')
          AND NOT (
              COALESCE(agent_type, '')='emotion'
              AND COALESCE(trigger_type, '')='emotion_time_slot'
          )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_intervention_runs_help_request_id
        ON intervention_runs(help_request_id)
        WHERE help_request_id IS NOT NULL
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_intervention_runs_assessment_scope
        ON intervention_runs(group_id, COALESCE(session_id, 0), state_assessment_id,
                             COALESCE(trigger_type, 'auto_state'), COALESCE(agent_type, 'strategy'))
        WHERE state_assessment_id IS NOT NULL
    """)

    # 4. agent_research_events table
    conn.execute("""CREATE TABLE IF NOT EXISTS agent_research_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER,
        task_id INTEGER,
        session_no INTEGER,
        discussion_id INTEGER,
        group_id INTEGER NOT NULL,
        agent_type TEXT NOT NULL,
        event_type TEXT NOT NULL,
        enabled_by_config INTEGER NOT NULL DEFAULT 1,
        trigger_type TEXT,
        trigger_reason_json TEXT,
        monitor_run_id INTEGER,
        intervention_run_id INTEGER,
        message_id INTEGER,
        state_before_json TEXT,
        context_snapshot_json TEXT,
        llm_prompt_json TEXT,
        llm_response_json TEXT,
        validation_json TEXT,
        scheduled_at TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        published_at TEXT,
        skipped_at TEXT,
        skip_reason TEXT,
        metadata_json TEXT
    )""")

    # 5. Indexes for agent_research_events
    conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_research_events_group_created ON agent_research_events(group_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_research_events_session ON agent_research_events(session_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_research_events_agent_type ON agent_research_events(agent_type)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_research_events_intervention_run ON agent_research_events(intervention_run_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_research_events_message ON agent_research_events(message_id)")


def _migration_unified_agent_mode(conn):
    """Add the canonical mutually-exclusive Agent mode.

    Clean historical rows are backfilled deterministically. Historical rows
    with both legacy switches enabled deliberately keep ``agent_mode`` NULL so
    startup/runtime validation blocks them instead of choosing a priority.
    Compatibility triggers keep direct legacy maintenance writes in sync;
    runtime code reads only ``agent_mode``.
    """
    _ensure_column(conn, "experiment_sessions", "agent_mode", "TEXT")
    _ensure_column(
        conn,
        "experiment_sessions",
        "research_state_monitoring_enabled",
        "INTEGER NOT NULL DEFAULT 0 CHECK(research_state_monitoring_enabled IN (0,1))",
    )
    conn.execute(
        """
        UPDATE experiment_sessions
        SET agent_mode=CASE
            WHEN COALESCE(strategy_agent_enabled, 0)=1
             AND COALESCE(emotion_agent_enabled, 0)=0 THEN 'strategy'
            WHEN COALESCE(strategy_agent_enabled, 0)=0
             AND COALESCE(emotion_agent_enabled, 0)=1 THEN 'emotion'
            WHEN COALESCE(strategy_agent_enabled, 0)=0
             AND COALESCE(emotion_agent_enabled, 0)=0 THEN 'none'
            ELSE NULL
        END
        WHERE agent_mode IS NULL OR TRIM(agent_mode)=''
        """
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_experiment_sessions_agent_mode
           ON experiment_sessions(agent_mode)"""
    )
    conn.execute("DROP TRIGGER IF EXISTS trg_experiment_sessions_agent_mode_validate_insert")
    conn.execute("DROP TRIGGER IF EXISTS trg_experiment_sessions_agent_mode_validate_update")
    conn.execute("DROP TRIGGER IF EXISTS trg_experiment_sessions_legacy_mode_insert")
    conn.execute("DROP TRIGGER IF EXISTS trg_experiment_sessions_legacy_mode_update")
    conn.execute(
        """
        CREATE TRIGGER trg_experiment_sessions_agent_mode_validate_insert
        BEFORE INSERT ON experiment_sessions
        WHEN NEW.agent_mode IS NOT NULL
         AND NEW.agent_mode NOT IN ('none', 'strategy', 'emotion')
        BEGIN
            SELECT RAISE(ABORT, 'invalid agent_mode');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER trg_experiment_sessions_agent_mode_validate_update
        BEFORE UPDATE OF agent_mode ON experiment_sessions
        WHEN NEW.agent_mode IS NOT NULL
         AND NEW.agent_mode NOT IN ('none', 'strategy', 'emotion')
        BEGIN
            SELECT RAISE(ABORT, 'invalid agent_mode');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER trg_experiment_sessions_legacy_mode_insert
        AFTER INSERT ON experiment_sessions
        WHEN NEW.agent_mode IS NULL
        BEGIN
            UPDATE experiment_sessions
            SET agent_mode=CASE
                WHEN COALESCE(NEW.strategy_agent_enabled, 0)=1
                 AND COALESCE(NEW.emotion_agent_enabled, 0)=0 THEN 'strategy'
                WHEN COALESCE(NEW.strategy_agent_enabled, 0)=0
                 AND COALESCE(NEW.emotion_agent_enabled, 0)=1 THEN 'emotion'
                WHEN COALESCE(NEW.strategy_agent_enabled, 0)=0
                 AND COALESCE(NEW.emotion_agent_enabled, 0)=0 THEN 'none'
                ELSE NULL
            END
            WHERE id=NEW.id;
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER trg_experiment_sessions_legacy_mode_update
        AFTER UPDATE OF strategy_agent_enabled, emotion_agent_enabled
        ON experiment_sessions
        BEGIN
            UPDATE experiment_sessions
            SET agent_mode=CASE
                WHEN COALESCE(NEW.strategy_agent_enabled, 0)=1
                 AND COALESCE(NEW.emotion_agent_enabled, 0)=0 THEN 'strategy'
                WHEN COALESCE(NEW.strategy_agent_enabled, 0)=0
                 AND COALESCE(NEW.emotion_agent_enabled, 0)=1 THEN 'emotion'
                WHEN COALESCE(NEW.strategy_agent_enabled, 0)=0
                 AND COALESCE(NEW.emotion_agent_enabled, 0)=0 THEN 'none'
                ELSE NULL
            END
            WHERE id=NEW.id;
        END
        """
    )


def _migration_emotion_reflection_run_audit(conn):
    """Add queryable audit fields for emotion validation and fallback runs."""
    columns = (
        ("emotion_slot_id", "INTEGER"),
        ("context_student_sequence_start", "INTEGER"),
        ("context_student_sequence_end", "INTEGER"),
        ("context_student_sequences_json", "TEXT"),
        ("latest_monitor_state_json", "TEXT"),
        ("latest_batch_state_json", "TEXT"),
        ("dominant_state", "TEXT"),
        ("dominant_state_source", "TEXT"),
        ("state_freshness_seconds", "REAL"),
        ("state_has_recovered", "INTEGER DEFAULT 0"),
        ("previous_emotion_run_id", "INTEGER"),
        ("model_raw_message", "TEXT"),
        ("validation_failure_codes_json", "TEXT"),
        ("fallback_state_code", "TEXT"),
        ("fallback_message", "TEXT"),
        ("final_visible_message", "TEXT"),
        ("final_disposition", "TEXT"),
        ("emotion_audit_json", "TEXT"),
        ("emotion_feedback_schema_version", "TEXT"),
        ("emotion_feedback_type_code", "TEXT"),
        ("emotion_feedback_type_label", "TEXT"),
        ("emotion_feedback_classification_json", "TEXT"),
        ("emotion_reference_template_ids_json", "TEXT"),
        ("emotion_feedback_output_json", "TEXT"),
    )
    for name, definition in columns:
        _ensure_column(conn, "intervention_runs", name, definition)
    # Fixed emotion slots are independent even when no new student message
    # advances cutoff_sequence. The legacy group+cutoff uniqueness otherwise
    # aliases consecutive empty windows to the first published run.
    conn.execute("DROP INDEX IF EXISTS idx_intervention_runs_group_cutoff")
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_intervention_runs_group_cutoff
        ON intervention_runs(group_id, COALESCE(cutoff_sequence, 0),
                             COALESCE(agent_type, 'strategy'),
                             COALESCE(trigger_type, 'auto_state'))
        WHERE status NOT IN ('CANCELLED', 'FAILED', 'EXPIRED', 'STALE')
          AND NOT (
              COALESCE(agent_type, '')='emotion'
              AND COALESCE(trigger_type, '')='emotion_time_slot'
          )
    """)
    conn.execute("DROP INDEX IF EXISTS idx_intervention_runs_emotion_slot")
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_intervention_runs_emotion_slot
        ON intervention_runs(emotion_slot_id)
        WHERE emotion_slot_id IS NOT NULL
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_intervention_runs_emotion_slot_active
        ON intervention_runs(emotion_slot_id)
        WHERE emotion_slot_id IS NOT NULL
          AND status NOT IN ('CANCELLED', 'FAILED', 'EXPIRED', 'STALE')
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_intervention_runs_emotion_previous
        ON intervention_runs(previous_emotion_run_id)
        WHERE previous_emotion_run_id IS NOT NULL
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_intervention_runs_emotion_feedback_type
        ON intervention_runs(emotion_feedback_type_code, created_at)
        WHERE emotion_feedback_type_code IS NOT NULL
    """)


def _migration_unified_discussion_scope(conn):
    """Add queryable, discussion-scoped columns without rewriting old rows."""
    columns = {
        "messages": (
            ("discussion_id", "INTEGER"),
            ("scope_resolved_from", "TEXT"),
            ("legacy_scope_fallback", "INTEGER NOT NULL DEFAULT 0"),
            ("scope_fallback_reason", "TEXT"),
        ),
        "state_assessments": (("discussion_id", "INTEGER"),),
        "group_states": (
            ("session_id", "INTEGER"),
            ("discussion_id", "INTEGER"),
        ),
        "collaboration_state_segments": (("discussion_id", "INTEGER"),),
        "state_assessment_batches": (
            ("session_no", "INTEGER"),
            ("task_id", "INTEGER"),
        ),
        "discussion_assessment_cursors": (
            ("session_no", "INTEGER"),
            ("task_id", "INTEGER"),
        ),
        "monitor_runs": (
            ("session_no", "INTEGER"),
            ("discussion_id", "INTEGER"),
            ("scope_resolved_from", "TEXT"),
            ("legacy_scope_fallback", "INTEGER NOT NULL DEFAULT 0"),
            ("scope_fallback_reason", "TEXT"),
        ),
        "intervention_runs": (("session_no", "INTEGER"),),
        "intervention_logs": (
            ("session_no", "INTEGER"),
            ("discussion_id", "INTEGER"),
        ),
        "help_requests": (("discussion_id", "INTEGER"),),
        "agent_research_events": (("discussion_id", "INTEGER"),),
        "collaboration_state_finalizations": (("discussion_id", "INTEGER"),),
    }
    for table, definitions in columns.items():
        for name, definition in definitions:
            _ensure_column(conn, table, name, definition)

    index_sql = (
        "CREATE INDEX IF NOT EXISTS idx_messages_discussion_scope "
        "ON messages(group_id, session_id, discussion_id, sequence)",
        "CREATE INDEX IF NOT EXISTS idx_state_assessments_discussion_scope "
        "ON state_assessments(group_id, session_id, discussion_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_group_states_discussion_scope "
        "ON group_states(group_id, session_id, discussion_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_collab_segments_discussion_scope "
        "ON collaboration_state_segments(group_id, session_id, discussion_id, start_sequence)",
        "CREATE INDEX IF NOT EXISTS idx_batches_discussion_status "
        "ON state_assessment_batches(session_id, discussion_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_monitor_runs_discussion_scope "
        "ON monitor_runs(group_id, session_id, discussion_id, cutoff_sequence)",
        "CREATE INDEX IF NOT EXISTS idx_intervention_runs_discussion_scope "
        "ON intervention_runs(group_id, session_id, discussion_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_intervention_logs_discussion_scope "
        "ON intervention_logs(group_id, session_id, discussion_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_help_requests_discussion_scope "
        "ON help_requests(group_id, session_id, discussion_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_agent_events_discussion_scope "
        "ON agent_research_events(group_id, session_id, discussion_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_finalizations_discussion_scope "
        "ON collaboration_state_finalizations(group_id, session_id, discussion_id, status)",
    )
    for statement in index_sql:
        conn.execute(statement)
