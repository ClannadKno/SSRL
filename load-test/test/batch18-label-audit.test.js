const test = require('node:test');
const assert = require('node:assert/strict');
const {
  buildBatch18LabelAudit,
  summarizeStage2Health,
  summarizeStrategyCompatibility
} = require('../src/batch18LabelAudit');

const manifest = require('../../services/strategy_route_manifest.json');

test('batch18 audit accepts three visible unclassified paths with consistent UI/API/export data', () => {
  const groups = fixtureGroups();
  const report = buildBatch18LabelAudit({
    sessionId: 2,
    sourceSummary: sourceSummary(),
    session: sessionRow(),
    groups,
    ui: {
      groupCode: 'G06',
      messages: groups[5].review.messages.map((message) => ({
        sequence: message.sequence,
        display_state_label: message.display_state_label
      }))
    },
    manifest
  });

  assert.equal(report.passed, true);
  assert.equal(report.counts.student_message_count, 8);
  assert.equal(report.counts.precise_state_student_message_count, 5);
  assert.equal(report.counts.success_uncovered_student_message_count, 1);
  assert.equal(report.counts.unknown_sub_state_student_message_count, 1);
  assert.equal(report.counts.failed_unclassified_student_message_count, 1);
  assert.equal(report.display_label_coverage_rate, 1);
  assert.equal(report.strategy_compatibility.compatibility_rate, 1);
  assert.equal(report.strict_scenario_trigger_coverage.affects_batch18_pass, false);
});

test('batch18 audit rejects unclassified backfill and cross-surface label drift', () => {
  const groups = fixtureGroups();
  groups[5].review.messages[0].final_sub_state_code = 'standard';
  groups[5].messagesExport[0].display_state_label = '错误标签';
  const report = buildBatch18LabelAudit({
    sessionId: 2,
    sourceSummary: sourceSummary(),
    session: sessionRow(),
    groups,
    ui: {
      groupCode: 'G06',
      messages: groups[5].review.messages.map((message) => ({
        sequence: message.sequence,
        display_state_label: message.display_state_label
      }))
    },
    manifest
  });

  assert.equal(report.passed, false);
  assert.equal(report.checks.no_unclassified_state_backfill, false);
  assert.equal(report.checks.teacher_api_export_consistent, false);
});

test('stage2 and strategy helpers reject truncation and route-invalid selections', () => {
  const health = summarizeStage2Health([
    latency(1, { finish_reason: 'length', json_extractable: false, incomplete_response: true })
  ]);
  assert.equal(health.passed, false);
  assert.deepEqual(health.truncated_attempt_ids, [1]);

  const compatibility = summarizeStrategyCompatibility([{
    id: 9,
    canonical_sub_state_code: 'execution_progress',
    state_overlays: [],
    selected_strategy_id: 'ER-002'
  }], manifest);
  assert.equal(compatibility.passed, false);
  assert.equal(compatibility.invalid_selections[0].route_valid, false);
});

function fixtureGroups() {
  const groups = [];
  for (let index = 1; index <= 6; index += 1) {
    const groupCode = `G${String(index).padStart(2, '0')}`;
    const messages = index === 6
      ? [
          message(601, 1, 'assessment_complete_unconfirmed', '已完成评估但无确认片段'),
          message(602, 2, 'unknown_sub_state', '子状态不确定'),
          message(603, 3, 'assessment_failed_unclassified', '评估失败/未分类')
        ]
      : [message(index * 100 + 1, 1, 'standard', '常规协作', 'standard')];
    const summary = summaryFor(messages);
    groups.push({
      groupCode,
      groupId: index,
      review: { messages, message_assignment_summary: summary },
      detail: { message_assignment_summary: { ...summary } },
      messagesExport: messages.map((item) => ({ ...item, message_id: item.id })),
      unifiedExport: messages.map((item) => ({
        ...item,
        actor_role: 'student',
        event_type: 'state_assignment',
        related_id: item.id
      })),
      dbAudit: {
        tables: {
          state_assessment_batches: [{ id: index, status: index === 6 ? 'failed' : 'succeeded' }],
          strategy_pipeline_runs: [{
            id: index,
            final_status: index === 6 ? 'FAILED' : 'PUBLISHED',
            stage3_status: index === 6 ? '' : 'SUCCEEDED',
            canonical_sub_state_code: index === 6 ? null : 'standard',
            state_overlays: [],
            selected_strategy_id: index === 6 ? null : 'SS-001'
          }],
          strategy_pipeline_latency_events: [latency(index, {
            finish_reason: 'stop',
            json_extractable: true,
            incomplete_response: false
          })]
        }
      }
    });
  }
  return groups;
}

function message(id, sequence, code, label, finalCode = '') {
  return {
    id,
    sequence,
    role: 'student',
    display_state_code: code,
    display_state_label: label,
    final_sub_state_code: finalCode
  };
}

function summaryFor(messages) {
  const count = (code) => messages.filter((item) => item.display_state_code === code).length;
  const precise = messages.filter((item) => item.final_sub_state_code).length;
  return {
    policy: 'message_state_assignment_v2',
    student_message_count: messages.length,
    display_assigned_student_message_count: messages.length,
    precise_sub_state_message_count: precise,
    unknown_sub_state_student_message_count: count('unknown_sub_state'),
    success_uncovered_student_message_count: count('assessment_complete_unconfirmed'),
    failed_unclassified_student_message_count: count('assessment_failed_unclassified')
  };
}

function latency(id, details) {
  return {
    id,
    event: 'stage2_llm_attempt_1_finished',
    details_json: JSON.stringify(details)
  };
}

function sourceSummary() {
  return {
    runId: 'real-six-group-run',
    scenario: 'six-group-strategy-coverage',
    validationMode: 'real_coverage',
    auditAvailable: true,
    input_integrity: {
      status: 'COMPLETE',
      expected_script_messages: 8,
      successful_script_messages: 8
    },
    actualStrategyCoverage: { sessionId: 2 },
    coverage_statistics: {
      scenario_coverage_passed_count: 2,
      scenario_coverage_expected_count: 3
    }
  };
}

function sessionRow() {
  return {
    id: 2,
    session_no: 2,
    status: 'ended',
    agent_detection_enabled: 1,
    strategy_agent_enabled: 1,
    emotion_agent_enabled: 0
  };
}
