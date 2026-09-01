#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Safely prepare failed Stage 2 assessment windows for ordered replay.

The command is a dry run unless ``--apply`` is supplied. ``--enqueue`` also
requests the first window; normal continuation then schedules the rest.
Replayed windows are state-only and cannot publish historical interventions.
"""

from __future__ import annotations

import argparse
import json
import os
import sys


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare terminal read-timeout/schema failures for replay."
    )
    parser.add_argument("--group-id", type=int, required=True)
    parser.add_argument("--session-id", type=int, required=True)
    parser.add_argument("--discussion-id", type=int, required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Commit the cursor rewind and batch reset. Default is dry-run.",
    )
    parser.add_argument(
        "--enqueue",
        action="store_true",
        help="After --apply, enqueue the first replay window.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Include the complete matched batch rows in JSON output.",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.enqueue and not args.apply:
        raise SystemExit("--enqueue requires --apply")

    from services.state_assessment_batch_service import StateAssessmentBatchService

    result = StateAssessmentBatchService.prepare_scope_reprocessing(
        group_id=args.group_id,
        session_id=args.session_id,
        discussion_id=args.discussion_id,
        apply=args.apply,
    )
    if args.enqueue and result.get("prepared"):
        from services.state_assessment_scheduler import request_state_assessment

        result["enqueue_result"] = request_state_assessment(
            group_id=args.group_id,
            session_id=args.session_id,
            discussion_id=args.discussion_id,
            trigger_type="message_count_periodic",
            continuation=True,
        )
    output = dict(result)
    if not args.verbose:
        output.pop("batches", None)
    print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("batches") else 1


if __name__ == "__main__":
    raise SystemExit(main())
