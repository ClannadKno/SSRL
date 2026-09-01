# SSRL-ESP browser load test

This directory contains a single-machine Playwright load test for the deployed SSRL-ESP student discussion flow.

## What it simulates

- 1 shared Chromium browser process.
- 60 isolated browser contexts.
- 60 student pages.
- Key-based login through `/login`.
- Optional full flow: pre-questionnaire, group discussion, mid/event check-ins, active help requests, group deliverable submission, post-check-in, and post-questionnaire.
- One continuous 60-minute, four-student, text-only replay with 16 scenes and 144 student messages.
- Twelve primary sub-states, three overlay cases, and one successful `unknown_sub_state` process-state case.
- Entry to `/student/collab?phase=discussion`.
- 30-minute or 60-minute discussion windows.
- Randomized student behavior: reading, scrolling, posting, and reply-like messages.
- Browser-side metrics: page errors, console errors, failed requests, websocket closes, message latency, and per-student failures.

## Install

```bash
cd load-test
npm install
npm run install:browsers
```

## Prepare students

By default the full-flow scenarios read the project-level CSV:

```text
../data/login_keys.csv
```

The CSV must contain the columns:

```text
role,participant_code,display_name,group_code,group_no,member_no,group_id,user_id,login_key
```

The runner selects `role=student`, `group_no=1..15`, and `member_no=1..4`, for a total of 60 students.

You can still create `data/students.json` from `data/students.example.json` and override the input with `--students`.

The file may be either:

```json
[
  { "id": "student_001", "loginKey": "real-key-001" },
  { "id": "student_002", "loginKey": "real-key-002" }
]
```

or:

```json
{
  "students": [
    { "id": "student_001", "loginKey": "real-key-001" },
    { "id": "student_002", "loginKey": "real-key-002" }
  ]
}
```

Provide at least 60 real student login keys if using JSON.

## Dry run

```bash
npm run dry-run:30m -- --base-url https://your-deployed-server.com
npm run dry-run:60m -- --base-url https://your-deployed-server.com
npm run dry-run:full-flow -- --base-url https://your-deployed-server.com
```

## Run

30-minute discussion:

```bash
npm run test:discussion:30m -- --base-url https://your-deployed-server.com
```

60-minute discussion:

```bash
npm run test:discussion:60m -- --base-url https://your-deployed-server.com
```

Full 15 groups x 4 students flow, 30-minute discussion:

```bash
npm run test:full-flow:30m -- --base-url https://your-deployed-server.com
```

Full 15 groups x 4 students flow, 60-minute discussion:

```bash
npm run test:full-flow:60m -- --base-url https://your-deployed-server.com
```

Four-person canonical sub-state tests:

```bash
npm run dry-run:one-group:states -- --base-url https://your-deployed-server.com
npm run test:one-group:states -- --base-url https://your-deployed-server.com
npm run test:one-group:states -- --base-url https://your-deployed-server.com --state-case S01
npm run test:one-group:states -- --base-url https://your-deployed-server.com --state-case standard
npm run test:one-group:states -- --base-url https://your-deployed-server.com --state-case psychological_safety_risk
npm run test:one-group:states -- --base-url https://your-deployed-server.com --state-case unknown_sub_state
npm run test:one-group:states -- --base-url https://your-deployed-server.com --suite primary-substates
npm run test:one-group:states -- --base-url https://your-deployed-server.com --suite overlays
npm run test:one-group:states -- --base-url https://your-deployed-server.com --suite model-failure
npm run test:one-group:states -- --base-url https://your-deployed-server.com --suite agent-lock
npm run test:p0-batch6 -- --base-url https://your-deployed-server.com --expected-session-id <id>
npm run test:p0-batch6-direct -- --base-url https://your-deployed-server.com
npm run dry-run:six-group:strategy-coverage -- --base-url https://your-deployed-server.com
npm run test:six-group:strategy-coverage -- --base-url https://your-deployed-server.com --expected-session-id <id>
```

`six-group-strategy-coverage` uses G01-G06 concurrently with four students per
group. Each group receives its own scripted state family; messages are never
repeated across groups. Each group's conversation now spans roughly 34-35.5
minutes, with the final scripted messages landing between 35:30 and 36:00 of
the fixed 40-minute window. Normal planned message gaps are at least 35
seconds, state-family transitions receive at least 75 seconds, and the final
four minutes are reserved for pipeline settlement. If an Agent gate makes
messages overdue, the runner still enforces a 25-second same-group runtime
gap instead of sending a catch-up burst. The scenario preserves the real 190-second EA-001
silence, the 130-150-second OI-002 thinking window, the five-minute
participation/overload windows, and at least 130 seconds between observed
visible interventions in the same group. Stage boundary failures are recorded
without aborting the remaining groups. This scenario currently runs in
`student_discussion_only` mode: it verifies the scripted student flow and
visible intervention expectations, but does not run or claim DB/API/export
audit coverage and does not require `SSRL_ENABLE_STATE_SUITE_AUDIT`.

The `p0-batch6` suite keeps four students in one real discussion for about
8.5 minutes. Both Agents must be enabled. It crosses a real five-minute
emotion slot, requests one normal strategy intervention, then supplies fresh
self-regulation/OI evidence that must suppress a second intervention. A run
also fails unless the emotion slot is eventually sent or intentionally
suppressed after strategy coordination. Its report subdirectory contains the
nine exact batch-6 artifacts listed in `docs/plan.md`.

`p0-batch6-direct` is the explicitly non-isolated fallback. It writes the same
16 student messages into the current teacher-controlled discussion and keeps
the real 8.5-minute clock, but it does not require or claim DB-equivalent
server audit coverage. The nine-file bundle marks `auditAvailable=false` and
`p0Batch6Acceptance.passed=false`; use its browser events, transcript, Agent
messages, and input-lock observations only as supplemental evidence.

The command without `--state-case` or `--suite` runs the complete `S01`-`S16` serial dialogue in student-discussion-only mode. It logs in the four students to the current teacher-controlled session, submits the normal student prerequisites, and sends the scripted discussion through the student UI. It does not select a session, log in as a teacher, inspect Agent switches, or assert server-side state/strategy outcomes. It uses the supplied five project costs consistently: quiet area 40,000, discussion area 35,000, flexible area 30,000, digital area 25,000, and reservation/noise management 15,000 yuan. The unlocked base schedule sends the last message by 05:00 and preserves 55 minutes of the 60-minute room for real Agent input-lock delays; lock waits stretch the actual wall-clock replay while preserving message order and required pauses.

For isolated diagnosis, each `--state-case` run produces its own report and requires a fresh test session/discussion with no earlier student messages or active pipeline. Select a case by `S01`–`S16`, a primary state, an overlay name, or `unknown_sub_state`. The runner never creates, ends, resets, or clears a production session.

The three overlay scenes follow the current ontology: `psychological_safety_risk` is asserted on `interpersonal_conflict`, `high_intensity_overload` can be asserted on `standard`, `interpersonal_conflict`, or the legacy-compatible `frustration`, and `stage_achievement` on `execution_progress`. `unknown_sub_state` means the model completed successfully but had insufficient evidence; it is not the same as the fault-injection suite's `unclassified` fallback.

Explicit strict suites selected with `--suite` or `--state-case` enable teacher-side audit. Deterministic primary, overlay, and model-failure tests require the target test session to have state detection and the strategy Agent enabled and the emotion Agent disabled. `agent-lock` and `p0-batch6` instead require the emotion Agent enabled. `model-failure` must target a test server configured to inject a real state-model failure; it passes only when the server records an explicit failed/quarantined batch, materializes an `unclassified` fallback segment, and publishes no strategy intervention.

The isolated test server must start with `SSRL_ENABLE_STATE_SUITE_AUDIT=1`. This enables an authenticated, content-free audit endpoint for the seven required DB tables plus a token-free room-lock snapshot; it does not return message text, participant identity, model payloads, generated Agent text, or complete lock tokens. The flag defaults to off. A live strict state-suite run fails immediately when this audit, any required teacher API, or the structured export contract is unavailable. `--dry-run` skips those server checks and prints `validation_mode=plan_only`; dry-run output is never counted as real coverage.

For a clean alternative group in the same running session, add `--target-group-code G02`. The override selects only that group's four login rows and retargets scripted-message and server-audit metadata together; it is valid only for one-group scenarios. The strict clean-context preflight still runs before any student login or message write.

In the default discussion-only mode, Agent behavior remains under teacher control. When the room enters `AI_INTERVENING`, students wait for the input lock to clear and then continue; the runner does not require or prohibit an intervention at scripted boundaries. In strict suites, nine intervention scenes require lock, Agent reply, and unlock evidence, while seven restraint scenes fail on an unexpected room lock or Agent message. Final strict audit also requires detected states, terminal strategy runs, and the configured Agent isolation.

Completed reports keep planned and actual evidence separate:

- `scriptedStateCoverage`: script labels and message send results only; `provesServerDetection=false`.
- `scriptedStrategyCoverage`: declared route options and message send results only.
- `actualStateCoverage`: canonical states and overlay tags observed in server audit runs.
- `actualStrategyCoverage`: one valid server-side strategy/OI route per detected sub-state.
- `modelFailureCoverage`: explicit LLM failure, `unclassified` fallback, and no intervention.
- `agentLockCoverage`: dual-Agent precondition plus published/terminal strategy run recovery.
- `planned_script_coverage`: scenario plan only.
- `message_send_coverage`: successful sends with server-assigned message IDs.
- `canonical_db_coverage`: scoped messages, segments, batches, pipelines, interventions, evidence, and deduplication.
- `teacher_api_coverage`: emotion trend/review, Agent audit, group list, and group detail agreement.
- `export_coverage`: parsed `messages.csv`, `strategy_pipeline_runs.csv`, `interventions.csv`, and `unified-events.csv`.
- `intervention_coverage` / `inhibition_coverage`: published strategy and OI suppression agreement across DB and export.
- `*-strategy-audit.json`: baseline/final teacher audit snapshots used by the actual assertions.

Start and configure the intended session from the teacher side before running the default command. For strict suites, add `--expected-session-id <id>` when session identity must be pinned; strict runs exit non-zero on dirty context, wrong Agent flags, missing server detection, non-terminal lock state, or failed suite assertions.

You can also override the duration:

```bash
node src/run.js --scenario discussion-30m --base-url https://your-deployed-server.com --duration-minutes 5
```

You can slow the ramp-up when checking capacity boundaries:

```bash
npm run test:full-flow:30m -- --base-url http://your-server.example.com --ramp-batch-size 2 --ramp-interval-seconds 15
```

## Resource modes

Default mode is `light`, which keeps JS and CSS but blocks images, media, fonts, and common third-party analytics domains.

```bash
node src/run.js --scenario discussion-30m --base-url https://your-deployed-server.com --resource-mode full
```

Use `full` when you want a more realistic browser load and the machine has enough memory.

## Reports

Each run writes:

- `reports/<runId>-summary.json`
- `reports/<runId>-events.csv`
- `reports/<runId>-errors.log`
- `reports/<runId>-transcript.json`
- `reports/<runId>-transcript.md`

The most important fields are:

- `counters.loginSuccess`
- `counters.preQuestionnaireSuccess`
- `counters.discussionReady`
- `counters.messageSuccess`
- `counters.checkinSuccess`
- `counters.helpAccepted`
- `counters.deliverableSubmitted`
- `counters.postQuestionnaireSuccess`
- `latencies.messageMs.p95`
- `counters.pageErrors`
- `counters.requestFailures`
- `students[].fatalError`
- `transcripts[].agentMessageCount`
- `counters.expectedInterventionStarted` / `Completed` / `Failed`
- `counters.expectedNoInterventionStarted` / `Completed` / `Failed`
- `scriptedStateCoverage`
- `transcripts[].scriptedStates`

For the single-group state replay, use `*-transcript.md` to inspect the ordered conversation. Scripted student messages include the reproduced state label, while `role=agent` rows show actual SERA interventions that appeared in the student chat.

## Notes

- Use dedicated test participants and a dedicated test session.
- Do not run this against a shared production session without a stop plan.
- If one machine cannot hold 60 contexts, keep `resourceMode=light`, reduce viewport size in `config/common.js`, or split the same scenario across two machines.
