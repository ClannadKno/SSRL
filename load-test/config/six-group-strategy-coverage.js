const base = require('./trigger-states-4p');
const {
  SCENARIO_TYPES,
  scenarioTypeFor
} = require('../src/scenarioClassification');
const { allManifestStrategyIds } = require('../src/strategyRouteManifest');

const strategyRouteManifest = require('../../services/strategy_route_manifest.json');

const GROUP_CODES = ['G01', 'G02', 'G03', 'G04', 'G05', 'G06'];
const INTERVENTION_SPACING_SECONDS = 130;
const DISCUSSION_DURATION_SECONDS = 40 * 60;
const SCRIPT_SETTLE_HEADROOM_SECONDS = 4 * 60;
const GROUP_START_SECONDS = 5;
const GROUP_START_STAGGER_SECONDS = 20;
const GROUP_END_STAGGER_SECONDS = 6;
const DEFAULT_MESSAGE_GAP_SECONDS = 25;
const STAGE_BOUNDARY_GAP_SECONDS = 75;
const STAGE_BOUNDARY_SLACK_WEIGHT = 3;

const DEFAULT_EVIDENCE_DESCRIPTIONS = Object.freeze({
  confusion: 'Persistent uncertainty about the next step or how to interpret the task.',
  frustration: 'Repeated failed attempts or explicit discouragement while trying to continue.',
  interpersonal_conflict: 'Personalized rejection, blame, or relational tension between members.',
  burnout: 'Low energy, loss of meaning, and explicit reluctance to continue the task.',
  off_topic_unregulated: 'Off-topic drift without a clear self-recovery back to the task.',
  perfunctory_detachment: 'Low-effort, careless, or mechanical task participation.',
  individual_marginalization: 'A member repeatedly contributes but is ignored or excluded from decisions.',
  standard: 'Routine, safe collaboration without a distinct risk signal.',
  deep_thinking: 'Sustained individual reasoning or research with an explicit need for uninterrupted time.',
  execution_progress: 'Concrete task execution is progressing with roles, calculations, or deliverables.',
  constructive_conflict: 'Disagreement remains task-focused and is repaired through evidence or negotiation.',
  off_topic_self_regulated: 'Temporary off-topic movement followed by an explicit return to the task.',
  unknown_sub_state: 'Insufficient evidence for a canonical state.'
});

function dialogue(memberNo, text, options = {}) {
  return { memberNo, text, ...options };
}

function trigger(memberNo, text, options = {}) {
  return dialogue(memberNo, text, {
    waitForExpectedInterventionAfter: true,
    ...options
  });
}

function continuation(memberNo, text, options = {}) {
  return dialogue(memberNo, text, { phase: 'continuation', ...options });
}

function restraint(memberNo, text, observationSeconds, options = {}) {
  return dialogue(memberNo, text, {
    waitForNoInterventionAfter: true,
    noInterventionObservationSeconds: observationSeconds,
    ...options
  });
}

function buildCandidateWindow(id, messages) {
  const triggerIndex = messages.findIndex((message) => message.waitForExpectedInterventionAfter);
  const hasTrigger = triggerIndex >= 0;
  const candidateStartIndex = hasTrigger ? Math.max(0, triggerIndex - 2) : 0;
  const candidateEndIndex = hasTrigger ? triggerIndex : Math.max(0, messages.length - 1);
  const evidenceIndices = hasTrigger ? [triggerIndex] : [];
  const messageKey = (index) => `script:${id}:m${index + 1}`;
  return {
    trigger_message_id: hasTrigger ? messageKey(triggerIndex) : null,
    candidate_start_sequence: candidateStartIndex + 1,
    candidate_end_sequence: candidateEndIndex + 1,
    expected_evidence_message_ids: evidenceIndices.map(messageKey),
    trigger_message_index: hasTrigger ? triggerIndex + 1 : null,
    expected_evidence_message_indices: evidenceIndices.map((index) => index + 1)
  };
}

function stage({
  id,
  title,
  groupCode,
  processState,
  canonicalSubState,
  preferredStrategyId = null,
  allowedStrategyIds = [],
  inhibitionStrategyId = null,
  shouldIntervene = false,
  detectedSelfRegulation = false,
  expectedOverlayTags = [],
  expectedPrimaryStrategyId = null,
  expectedEvidenceDescription = null,
  skipStrategyCoverage = false,
  messages
}) {
  const scenarioType = scenarioTypeFor({
    canonicalSubState,
    expectedOverlayTags,
    inhibitionStrategyId,
    shouldIntervene
  });
  const annotated = messages.map((message, index) => ({
    ...message,
    ...(scenarioType === SCENARIO_TYPES.OPTIONAL_SUPPORT
      ? { waitForExpectedInterventionAfter: false }
      : {}),
    ...(index === 0 && shouldIntervene
      ? { minimumPreviousInterventionGapSeconds: INTERVENTION_SPACING_SECONDS }
      : {})
  }));
  const candidateWindow = buildCandidateWindow(id, annotated);
  const expectedEvidence = expectedEvidenceDescription ||
    DEFAULT_EVIDENCE_DESCRIPTIONS[canonicalSubState] ||
    'Evidence is limited to the scripted candidate window.';
  return {
    id,
    title,
    groupCode,
    processState,
    canonicalSubState,
    preferredStrategyId,
    allowedStrategyIds,
    inhibitionStrategyId,
    shouldIntervene,
    scenarioType,
    scenario_type: scenarioType,
    detectedSelfRegulation,
    expectedOverlayTags,
    expected_primary_strategy_id: expectedPrimaryStrategyId || (
      strategyRouteManifest.routes[canonicalSubState] &&
      strategyRouteManifest.routes[canonicalSubState].primary_strategy_ids &&
      strategyRouteManifest.routes[canonicalSubState].primary_strategy_ids[0]
    ) || null,
    expectedPrimaryStrategyId: expectedPrimaryStrategyId || (
      strategyRouteManifest.routes[canonicalSubState] &&
      strategyRouteManifest.routes[canonicalSubState].primary_strategy_ids &&
      strategyRouteManifest.routes[canonicalSubState].primary_strategy_ids[0]
    ) || null,
    expectedPrimaryState: canonicalSubState,
    expectedOverlayStates: [...expectedOverlayTags],
    expected_primary_state: canonicalSubState,
    expected_overlay_states: [...expectedOverlayTags],
    runtime_route_version: strategyRouteManifest.version,
    runtimeRouteVersion: strategyRouteManifest.version,
    expected_evidence_description: expectedEvidence,
    expectedEvidenceDescription: expectedEvidence,
    ...candidateWindow,
    candidate_window: candidateWindow,
    skipStrategyCoverage,
    messages: annotated
  };
}

const groupFlows = {
  G01: [
    stage({
      id: 'G1-A', title: '不确定性正常化', groupCode: 'G01',
      processState: 'blocked_frustration', canonicalSubState: 'confusion',
      preferredStrategyId: 'EE-006', allowedStrategyIds: ['EE-006', 'ER-005', 'EA-001'],
      shouldIntervene: true,
      messages: [
        dialogue(1, '我不太确定是不是应该先选三个项目再算预算，我这个理解可能不对。'),
        dialogue(2, '我也有点不确定，万一顺序错了，后面算的东西是不是都要重来？'),
        dialogue(3, '我有个想法，但可能挺幼稚的，要不先把六个项目按需求分类？'),
        trigger(1, '算了，我刚才可能说错了，你们先说吧。'),
        continuation(4, '我也没完全看懂“覆盖两类需求”具体要怎么证明。'),
        continuation(3, '我重新看了要求，两类需求可以按个人学习和小组协作来解释。'),
        continuation(2, '那我们先把每个项目放进这两类，至少第一步明确了。'),
        continuation(4, '好，我来列小组协作类的项目。'),
        continuation(1, '我列个人学习类的。')
      ]
    }),
    stage({
      id: 'G1-B', title: '问题解构', groupCode: 'G01',
      processState: 'blocked_frustration', canonicalSubState: 'confusion',
      preferredStrategyId: 'ER-005', allowedStrategyIds: ['ER-005', 'EE-006', 'EA-001'],
      shouldIntervene: true,
      messages: [
        dialogue(2, '现在六个项目都各有道理，我完全不知道应该从哪开始。'),
        dialogue(3, '预算、至少三项、覆盖两类需求、放弃理由、评价指标，全挤在一起了。'),
        dialogue(4, '我翻来翻去还是找不到第一步应该做什么。'),
        trigger(1, '是不是要先定评价标准？但评价标准本身也不知道怎么定。'),
        continuation(2, '我们先停一下，把第一步说清楚；不是做不下去，是还没理解顺序。'),
        continuation(1, '先只解决一个问题吧，哪些项目最直接满足学习需求。'),
        continuation(3, '我觉得安静学习区和讨论区最直接。'),
        continuation(4, '我先做项目优先级，不同时处理预算。'),
        continuation(2, '这样清楚多了。')
      ]
    }),
    stage({
      id: 'G1-C', title: '同伴求助激活', groupCode: 'G01',
      processState: 'blocked_frustration', canonicalSubState: 'confusion',
      preferredStrategyId: 'SS-003', allowedStrategyIds: ['SS-003', 'EE-003', 'ER-005'],
      shouldIntervene: true,
      expectedEvidenceDescription: 'A member explicitly cannot write the evaluation criteria and asks peers for help before the group resolves it.',
      messages: [
        dialogue(4, '评价指标这一块我不理解，不知道指标究竟要证明什么。'),
        dialogue(1, '我也分不清“可观察”和“可测量”，下一步应该先写哪一种？', { minimumGapBeforeSeconds: 35 }),
        trigger(4, '我还是不会写评价指标，能不能先帮我把要求和第一步讲清楚？', { minimumGapBeforeSeconds: 40 }),
        continuation(3, '噪声系统可以看投诉数量有没有下降。', { minimumGapBeforeSeconds: 45 }),
        continuation(1, '这些指标应该已经够两个了吧。', { minimumGapBeforeSeconds: 35 }),
        continuation(2, '我们继续算预算。', { minimumGapBeforeSeconds: 30 }),
        continuation(3, 'S4 刚才说不会写指标，我们把刚才三个例子整理给他看看。'),
        continuation(4, '明白了，就是要能实际统计的东西。'),
        continuation(1, '对，你可以帮我们检查哪些指标最容易收集。'),
        continuation(4, '那我来整理最后的评价指标。')
      ]
    }),
    stage({
      id: 'G1-D', title: '集体沉默觉察', groupCode: 'G01',
      processState: 'negative_silence', canonicalSubState: 'confusion',
      preferredStrategyId: 'EA-001', allowedStrategyIds: ['EA-001'], shouldIntervene: true,
      messages: [
        dialogue(1, '放弃哪个项目还是很难定。'),
        dialogue(2, '我也没想好，感觉每个都有用。'),
        dialogue(3, '大家先各自想一下吧。'),
        trigger(4, '放弃哪一项我还是完全没想清楚，大家先停两三分钟各自想，暂时不要继续发消息。', {
          minimumSilenceBeforeInterventionSeconds: 190,
          minimumPauseSeconds: 190
        }),
        continuation(2, '我卡在数字学习区和休息交流区之间。'),
        continuation(1, '我也是，那就比较它们和核心学习需求的关系。'),
        continuation(4, '休息区比较间接，我倾向放弃它。'),
        continuation(3, '可以，把“预算有限且与核心学习需求关系较弱”写成放弃理由。')
      ]
    }),
    stage({
      id: 'G1-E', title: '深度思考保护', groupCode: 'G01',
      processState: 'positive_collaboration', canonicalSubState: 'deep_thinking',
      preferredStrategyId: 'OI-002', allowedStrategyIds: ['OI-002'],
      inhibitionStrategyId: 'OI-002', shouldIntervene: false,
      messages: [
        dialogue(1, '预算组合我重新算一下，给我两分钟。'),
        dialogue(2, '我查一下预约系统和讨论区能不能算作两类不同需求。'),
        dialogue(3, '我也看看各项目费用有没有重复建设。'),
        restraint(4, '刚才大家安静地独立核算和查资料；结果是安静区三万、讨论区三万、预约系统两万、数字区两万，正好十万。', 140, {
          minimumGapBeforeSeconds: 120
        }),
        continuation(1, '我再核了一遍，四项合计确实是十万。'),
        continuation(2, '我查了，这个组合可以同时覆盖个人学习和小组协作。'),
        continuation(3, '数字区和安静区有部分功能重叠，可以考虑删数字区留预算余量。'),
        continuation(4, '这段时间是在算和查资料，不是卡住。')
      ]
    })
  ],

  G02: [
    stage({
      id: 'G2-A', title: '任务难度重评', groupCode: 'G02',
      processState: 'blocked_frustration', canonicalSubState: 'frustration',
      preferredStrategyId: 'ER-002', allowedStrategyIds: ['ER-002'], shouldIntervene: true,
      messages: [
        dialogue(1, '又超预算了，这个组合已经是我们第三次算错。'),
        dialogue(2, '安静区、讨论区、数字区和系统加起来十二万，根本放不进去。'),
        dialogue(3, '每次删一个又会不满足需求覆盖，这方案是不是根本做不出来？'),
        trigger(4, '我们刚刚那套思路又失败了，越算越乱。'),
        continuation(1, '感觉预算限制把所有方案都堵死了。'),
        continuation(2, '超预算至少说明我们必须明确优先级。'),
        continuation(3, '可以把“预算不足”直接转成放弃数字区的理由。'),
        continuation(4, '那先保留安静区、讨论区和预约系统。'),
        continuation(1, '这样反而比刚才清楚。')
      ]
    }),
    stage({
      id: 'G2-B', title: '困难外化请求', groupCode: 'G02',
      processState: 'blocked_frustration', canonicalSubState: 'frustration',
      preferredStrategyId: 'EE-003', allowedStrategyIds: ['EE-003', 'ER-002'], shouldIntervene: true,
      messages: [
        dialogue(2, '评价指标我已经改了三遍，还是不合要求，真的很挫败。'),
        dialogue(3, '我也反复改“可观察”和“可测量”，每次都觉得又写错了。'),
        trigger(4, '连使用人数都被我们推翻两次了，我现在不知道还能怎么改。'),
        continuation(1, '这部分确实难，我们先把各自卡住的地方分开。'),
        continuation(2, '我们几个好像卡的地方还不太一样。'),
        continuation(4, '我最难的是不知道什么数据能拿到。'),
        continuation(3, '我是不知道指标要评价项目还是评价整个方案。'),
        continuation(1, '那我们先分别说最难的点。'),
        continuation(2, '这样至少可以逐个解决。')
      ]
    }),
    stage({
      id: 'G2-C', title: '共情反馈', groupCode: 'G02',
      processState: 'blocked_frustration', canonicalSubState: 'frustration',
      preferredStrategyId: 'SS-006', allowedStrategyIds: ['SS-006'], shouldIntervene: true,
      messages: [
        dialogue(2, '一直改还是不对，我现在真的有点泄气。'),
        dialogue(1, '预约系统费用已经反复改了两次，预算表还是对不上。'),
        trigger(3, '讨论区也推翻了好几版，我越改越觉得这次又会失败。'),
        continuation(4, '预算表还差一列。'),
        continuation(1, '先一起核对出错的位置，不让一个人继续扛。'),
        continuation(3, '放弃理由也需要跟着预算一起检查。'),
        continuation(4, '其实我刚才算预算时也挺挫败的。'),
        continuation(1, '是，我们已经推翻好几版了。'),
        continuation(3, '先别让一个人扛着，我们一起核对。'),
        continuation(2, '好，那我把出错的位置发出来。')
      ]
    }),
    stage({
      id: 'G2-D', title: '情绪起点回溯', groupCode: 'G02',
      processState: 'blocked_frustration', canonicalSubState: 'frustration',
      preferredStrategyId: 'EA-007', allowedStrategyIds: ['EA-007'], shouldIntervene: true,
      messages: [
        dialogue(1, '不知道为什么，越讨论越觉得不顺。'),
        dialogue(2, '我也有这种感觉，但好像又说不出具体是哪一步错了。', { minimumGapBeforeSeconds: 65 }),
        dialogue(3, '前面还挺顺的，现在大家说话都有点没劲。', { minimumGapBeforeSeconds: 65 }),
        dialogue(4, '不是哪个项目特别难，就是整个讨论突然有点沉。', { minimumGapBeforeSeconds: 65 }),
        trigger(1, '我们一直往后推，但好像漏了什么。', { minimumGapBeforeSeconds: 30 }),
        continuation(2, '可能是从我们把需求分类推翻以后开始乱的。'),
        continuation(3, '对，那之后每个人理解的标准就不一样了。'),
        continuation(4, '回到那一步重新统一标准吧。'),
        continuation(1, '找到起点之后好处理多了。')
      ]
    }),
    stage({
      id: 'G2-E', title: '群体成就标记', groupCode: 'G02',
      processState: 'positive_collaboration', canonicalSubState: 'execution_progress',
      expectedOverlayTags: ['stage_achievement'],
      preferredStrategyId: 'SS-007', allowedStrategyIds: ['SS-007'], shouldIntervene: true,
      messages: [
        dialogue(1, '预算确认了，四个项目一共九万八。'),
        dialogue(2, '个人学习和小组协作两类需求也都覆盖了。'),
        dialogue(3, '放弃数字学习区的理由已经写清楚，是成本高且和现有设备重复。'),
        dialogue(4, '评价指标也定了：使用人数、预约率和噪声投诉变化。'),
        dialogue(2, '我们刚才卡了那么久，现在终于把最难的几块一起解决了。'),
        trigger(1, '而且这版是全组都认可的。')
      ]
    })
  ],

  G03: [
    stage({
      id: 'G3-A', title: '建设性冲突保护', groupCode: 'G03',
      processState: 'conflict_tension', canonicalSubState: 'constructive_conflict',
      preferredStrategyId: 'OI-001', allowedStrategyIds: ['OI-001'], inhibitionStrategyId: 'OI-001',
      detectedSelfRegulation: true, shouldIntervene: false,
      messages: [
        dialogue(1, '我不同意把预约系统放在最高优先级，它解决不了座位不足。'),
        dialogue(2, '你的担心是空间本身不够，我担心的是空间有了却管理不好。'),
        dialogue(3, '我们是不是把问题想成二选一了？空间建设和管理其实可以组合。'),
        dialogue(4, '可以结合一下，先保证基本空间，再用低成本系统提升使用效率。'),
        dialogue(1, '这样我能接受，关键是把两部分预算比例写清楚。'),
        restraint(2, '那我算一个组合方案。', 90)
      ]
    }),
    stage({
      id: 'G3-B', title: '幽默降压', groupCode: 'G03',
      processState: 'conflict_tension', canonicalSubState: 'interpersonal_conflict',
      preferredStrategyId: 'ER-007', allowedStrategyIds: ['ER-007', 'EE-001'], shouldIntervene: true,
      messages: [
        dialogue(1, '你又把预算加错了😂，十万都快被你算成十五万了。'),
        dialogue(2, '那你来算，我已经改三遍了，再改我都要怀疑计算器了。'),
        dialogue(3, '你们两个今天是和预算表杠上了。'),
        dialogue(4, '说真的，规则确实绕，但现在有点开始互相较劲了。'),
        dialogue(1, '我没有生气，就是着急。'),
        trigger(2, '我也没真生气，但被连续指出来确实有点烦。')
      ]
    }),
    stage({
      id: 'G3-C', title: '分歧外化', groupCode: 'G03',
      processState: 'conflict_tension', canonicalSubState: 'interpersonal_conflict',
      preferredStrategyId: 'EE-001', allowedStrategyIds: ['EE-001'], shouldIntervene: true,
      messages: [
        dialogue(1, '你们一直否定我的讨论区方案，根本没有认真听我的理由。'),
        trigger(2, '你每次都说不是针对我，却又直接绕过我的顾虑，我觉得你根本不愿意听。'),
        continuation(3, '不过现在每个人说的都是项目，真正担心的东西好像不一样。'),
        continuation(4, '我不反对讨论区，我担心的是最后没有预算做管理系统。'),
        continuation(1, '我希望先把各自真正担心的事情说清楚。'),
        continuation(2, '那先分别写下空间不足和管理效率两个顾虑。')
      ]
    }),
    stage({
      id: 'G3-D', title: '节奏调控', groupCode: 'G03',
      processState: 'conflict_tension', canonicalSubState: 'interpersonal_conflict',
      expectedOverlayTags: ['high_intensity_overload'],
      preferredStrategyId: 'ER-004', allowedStrategyIds: ['EA-005', 'ER-004'], shouldIntervene: true,
      messages: [
        dialogue(1, '先删数字区！'),
        dialogue(2, '不行，数字区很重要！', { minimumGapBeforeSeconds: 2 }),
        dialogue(3, '那休息区呢？', { minimumGapBeforeSeconds: 2 }),
        dialogue(4, '先看预算！', { minimumGapBeforeSeconds: 2 }),
        dialogue(1, '预算就是超了！', { minimumGapBeforeSeconds: 2 }),
        dialogue(2, '需求、预算和三套方案同时在变，我已经来不及整理了！', { minimumGapBeforeSeconds: 2 }),
        trigger(3, '你别又打断我；需求、预算、指标的消息一起涌过来，我根本跟不上，你每次都只顾自己的方案！', { minimumGapBeforeSeconds: 2 }),
        continuation(4, '先暂停互相指责，把三套方案按同一顺序整理。', { minimumGapBeforeSeconds: 2 })
      ]
    }),
    stage({
      id: 'G3-E', title: '冲突认知重评', groupCode: 'G03',
      processState: 'conflict_tension', canonicalSubState: 'interpersonal_conflict',
      preferredStrategyId: 'ER-001', allowedStrategyIds: ['ER-001'], shouldIntervene: true,
      messages: [
        dialogue(1, '你这个方案根本不合理，只考虑设备，完全没考虑真实使用。'),
        dialogue(2, '你每次都直接否定别人，好像只有你的方案是对的。'),
        dialogue(3, '现在明显变成两边了，一边坚持空间建设，一边坚持数字项目。'),
        dialogue(4, '谁都不愿意改，继续这样根本谈不拢。'),
        dialogue(1, '因为对方的前提就是错的，我为什么要让？'),
        trigger(2, '那也没必要继续讨论了，反正你不会听。'),
        continuation(3, '先假设对方是对的，各自说说对方方案成立需要什么条件。'),
        continuation(4, '空间方案成立的前提是使用需求确实很高。'),
        continuation(2, '数字方案成立的前提是学校已有空间够用。'),
        continuation(1, '这样看，其实我们缺的是需求依据，不是单纯谁说服谁。')
      ]
    }),
    stage({
      id: 'G3-F', title: '心理安全重建', groupCode: 'G03',
      processState: 'conflict_tension', canonicalSubState: 'interpersonal_conflict',
      preferredStrategyId: 'SS-004', allowedStrategyIds: ['SS-004'], shouldIntervene: true,
      expectedEvidenceDescription: 'A member proposes an idea, is directly dismissed, withdraws, and reports relational safety damage before repair begins.',
      messages: [
        dialogue(3, '要不把短暂休息交流区作为第三项？'),
        dialogue(1, '这个想法太离谱了，任务明明是学习空间，不是休闲装修。'),
        trigger(3, '好吧，那我不说了，后面你们定。'),
        continuation(2, '我们先继续算安静区和讨论区。'),
        continuation(4, '预约系统费用是两万。'),
        continuation(2, '评价指标先写使用人数。', { minimumGapBeforeSeconds: 45 }),
        continuation(1, '好。', { minimumGapBeforeSeconds: 45 }),
        continuation(4, '另一个写预约成功率。', { minimumGapBeforeSeconds: 45 }),
        continuation(2, '刚才休息区的想法也不是完全没价值，可以讨论它满足什么需求。'),
        continuation(1, '我刚才否定得太直接了。'),
        continuation(3, '我主要是想解决长时间学习后的恢复问题。'),
        continuation(4, '那可以把这个需求写进放弃项目的权衡说明。')
      ]
    })
  ],

  G04: [
    stage({
      id: 'G4-A', title: '积极氛围强化', groupCode: 'G04',
      processState: 'positive_collaboration', canonicalSubState: 'standard',
      preferredStrategyId: 'SS-001', allowedStrategyIds: ['SS-001'], shouldIntervene: true,
      expectedEvidenceDescription: 'The group shows ordinary positive collaboration and mutual listening without a distinct risk signal or required intervention.',
      messages: [
        dialogue(1, '大家刚才都把自己的想法说出来了，配合得挺顺。'),
        dialogue(2, '我喜欢我们先听完再回应，没有人急着否定别人。'),
        dialogue(3, '气氛比刚开始轻松多了。'),
        dialogue(4, '如果继续保持这种讨论方式，后面应该也会顺利。'),
        dialogue(1, '我觉得每个人的声音都被听见了。'),
        dialogue(2, '那我们先保持这个节奏。')
      ]
    }),
    stage({
      id: 'G4-B', title: '贡献可见化', groupCode: 'G04',
      processState: 'positive_collaboration', canonicalSubState: 'individual_marginalization',
      preferredStrategyId: 'SS-002', allowedStrategyIds: ['SS-002'], shouldIntervene: true,
      expectedEvidenceDescription: 'The same member offers measurable value twice, but the group repeatedly changes topic and proceeds without acknowledging the contribution.',
      messages: [
        dialogue(3, '其实预约系统可以减少空座却约不到位置的问题，还能提供可测量的预约率数据。'),
        dialogue(1, '安静区预算是三万，我们先写这个，预约系统先不展开。', { minimumGapBeforeSeconds: 20 }),
        dialogue(3, '我还可以把预约率直接列成评价指标，这样效果比较容易核对。'),
        trigger(2, '还是先不回应 S3 的预约系统建议，讨论区可能要四万。'),
        continuation(4, '那只剩三万给第三个项目。'),
        continuation(1, '第三项要不要直接选数字学习区？'),
        continuation(2, '刚才 S3 说预约系统还能直接提供评价数据，这一点挺重要。'),
        continuation(1, '那回头比较预约系统和数字区。'),
        continuation(3, '我把预约系统的优势列出来。')
      ]
    }),
    stage({
      id: 'G4-C', title: '个体情绪聚焦', groupCode: 'G04',
      processState: 'blocked_frustration', canonicalSubState: 'individual_marginalization',
      preferredStrategyId: 'EA-003', allowedStrategyIds: ['EA-003', 'EE-005'], shouldIntervene: true,
      expectedEvidenceDescription: 'One member first says their ideas are no longer useful and later repeats an unanswered concern after the group has moved on.',
      messages: [
        dialogue(4, '你们已经定得很快了，我有点跟不上，可能我的想法也没什么用了。'),
        dialogue(1, '安静区确定。'),
        dialogue(2, '讨论区也确定。'),
        dialogue(3, '第三项先看系统。'),
        dialogue(1, '我来写放弃理由。'),
        trigger(4, '我刚才说的预算顾虑还没人回应，如果我的意见一直不被听见，可能我的想法真的没什么用了。'),
        continuation(3, 'S4 可能对现在的组合有不同看法。'),
        continuation(4, '我担心两个实体空间占掉太多预算。'),
        continuation(1, '这个顾虑很关键，我们听一下替代组合。'),
        continuation(4, '可以用安静区加系统，再配一个低成本灵活区。')
      ]
    }),
    stage({
      id: 'G4-D', title: '沉默者邀请', groupCode: 'G04',
      processState: 'positive_collaboration', canonicalSubState: 'individual_marginalization',
      preferredStrategyId: 'EE-005', allowedStrategyIds: ['EE-005'], shouldIntervene: true,
      expectedEvidenceDescription: 'A member raises the same concrete budget concern twice while others continue the assigned work without responding.',
      messages: [
        dialogue(1, '我们开始整理最终方案。'),
        dialogue(2, '我刚核了一下，讨论区预算好像少算一万，先提醒一下。', { minimumGapBeforeSeconds: 70 }),
        dialogue(4, '我写需求覆盖。', { minimumGapBeforeSeconds: 70 }),
        dialogue(1, '放弃理由写数字学习区与现有资源重复。', { minimumGapBeforeSeconds: 70 }),
        dialogue(3, '指标写使用人数和预约率。', { minimumGapBeforeSeconds: 70 }),
        dialogue(2, '我再看了一遍，预算数字可能还是不对。', { minimumGapBeforeSeconds: 70 }),
        trigger(2, '这个问题我已经提醒两次了，但还没人回应。', { minimumGapBeforeSeconds: 30 }),
        continuation(2, '我看了一下，预算数字里讨论区好像少算了一万。'),
        continuation(3, '确实，幸好你检查了。'),
        continuation(1, '那组合需要调整。'),
        continuation(2, '可以把数字区删掉，其他要求仍然满足。')
      ]
    }),
    stage({
      id: 'G4-E', title: '执行推进保护', groupCode: 'G04',
      processState: 'positive_collaboration', canonicalSubState: 'execution_progress',
      preferredStrategyId: 'OI-004', allowedStrategyIds: ['OI-004'], inhibitionStrategyId: 'OI-004',
      detectedSelfRegulation: true, shouldIntervene: false,
      messages: [
        dialogue(1, '那需求分析我来。'),
        dialogue(2, '预算表我来。'),
        dialogue(3, '放弃理由我写。'),
        dialogue(4, '评价指标我整理。'),
        dialogue(1, '好。'),
        dialogue(2, '行，十分钟后合并。'),
        dialogue(3, '收到。'),
        restraint(4, '我已经开始写了。', 90)
      ]
    })
  ],

  G05: [
    stage({
      id: 'G5-A', title: '注意力重定向', groupCode: 'G05',
      processState: 'task_detached', canonicalSubState: 'off_topic_unregulated',
      preferredStrategyId: 'ER-003', allowedStrategyIds: ['ER-003'], shouldIntervene: true,
      messages: [
        dialogue(1, '你们中午吃了什么？食堂新开的窗口排队好长。'),
        dialogue(2, '我吃了面，味道一般。'),
        dialogue(3, '我刚才还在看周末去哪玩。'),
        dialogue(4, '最近那个游戏更新了吗？'),
        dialogue(1, '更新了，新地图还挺好玩。'),
        trigger(2, '等会一起开一局？'),
        continuation(3, '先回任务，我们刚才做到项目优先级。'),
        continuation(4, '我建议先定安静区。'),
        continuation(1, '讨论区也保留。'),
        continuation(2, '再比较系统和数字区。')
      ]
    }),
    stage({
      id: 'G5-B', title: '群体自修复保护', groupCode: 'G05',
      processState: 'task_detached', canonicalSubState: 'off_topic_self_regulated',
      preferredStrategyId: 'OI-003', allowedStrategyIds: ['OI-003'], inhibitionStrategyId: 'OI-003',
      detectedSelfRegulation: true, shouldIntervene: false,
      messages: [
        dialogue(1, '刚才说到食堂，我觉得新窗口其实还不错。'),
        dialogue(2, '就是排队太慢。'),
        dialogue(3, '咱们又跑题了，先回到共享学习空间。'),
        dialogue(4, '对，刚才讨论到预约系统要不要保留。'),
        dialogue(1, '那继续比较预约系统和数字学习区。'),
        dialogue(2, '预约系统更容易形成评价指标。'),
        dialogue(3, '我把两者优缺点列出来。'),
        restraint(4, '一分钟后我们投票。', 90)
      ]
    }),
    stage({
      id: 'G5-C', title: '目标聚焦重锚', groupCode: 'G05',
      processState: 'task_detached', canonicalSubState: 'perfunctory_detachment',
      preferredStrategyId: 'ER-006', allowedStrategyIds: ['ER-006'], shouldIntervene: true,
      expectedEvidenceDescription: 'Members repeatedly propose careless, low-effort completion shortcuts instead of doing the required task work.',
      messages: [
        dialogue(1, '预算大概填一下就行，反正谁也不会细看。'),
        dialogue(2, '项目随便选三个，差不多能交就好。'),
        dialogue(3, '理由复制几句模板，应该没人核对。'),
        dialogue(4, '指标先写“有效提升”，具体数字以后再说。'),
        dialogue(1, '反正不超预算就算完成，别再改了。'),
        trigger(2, '我现在完全不想继续挑项目了，随便定一个吧。'),
        continuation(3, '这次真正要交的是十万元以内的项目组合和理由。'),
        continuation(4, '只写“有效提升”不能说明评价指标，还是要补具体依据。'),
        continuation(1, '先把项目组合定下来，再逐项补放弃理由。'),
        continuation(2, '好，那我重新看需求覆盖和评价指标。')
      ]
    }),
    stage({
      id: 'G5-D', title: '分工支持', groupCode: 'G05',
      processState: 'blocked_frustration', canonicalSubState: 'individual_marginalization',
      preferredStrategyId: 'EE-005', allowedStrategyIds: ['EA-003', 'EE-005', 'SS-002'], shouldIntervene: true,
      expectedEvidenceDescription: 'One member carries the work, asks for shared ownership, and is dismissed before the others finally take assigned parts.',
      messages: [
        dialogue(1, '预算表是我算的，需求分类也是我写的。'),
        dialogue(2, '你先做吧，我等最后看结果。'),
        dialogue(3, '我都可以，定了告诉我。'),
        dialogue(4, '我暂时没什么想法。'),
        dialogue(1, '怎么又都是我在整理？其他人能不能也分一点任务？'),
        trigger(2, '反正你已经做得差不多了。'),
        continuation(3, '我来负责放弃项目和理由。'),
        continuation(4, '我整理评价指标。'),
        continuation(2, '那我核对预算和需求覆盖。'),
        continuation(1, '可以，我只负责合并。')
      ]
    }),
    stage({
      id: 'G5-E', title: '意义重连', groupCode: 'G05',
      processState: 'task_detached', canonicalSubState: 'burnout',
      preferredStrategyId: 'ER-008', allowedStrategyIds: ['ER-008'], shouldIntervene: true,
      messages: [
        dialogue(1, '反正学校也不一定会真的采用，讨论这么细有什么用。'),
        dialogue(2, '我也觉得选哪个都差不多，随便凑三个就行。'),
        dialogue(3, '做完可能也没有下文，没必要一直改。'),
        dialogue(4, '差不多得了，能交就行。'),
        dialogue(1, '预算别超就行，理由随便写两句吧。'),
        trigger(2, '我现在完全不想继续挑项目了。'),
        continuation(3, '那先不想学校会不会采用，只说一个自己最希望校园里真的有的项目。'),
        continuation(4, '我最想要安静学习区，现在确实很难找到安静位置。'),
        continuation(1, '我最想保留预约系统，找座位太浪费时间。'),
        continuation(2, '这样看还是有真实需求的，可以从这两个开始组合。')
      ]
    })
  ],

  G06: [
    stage({
      id: 'G6-A', title: 'unknown 基线', groupCode: 'G06',
      processState: 'unknown', canonicalSubState: 'unknown_sub_state',
      allowedStrategyIds: [], shouldIntervene: false, skipStrategyCoverage: true,
      messages: [
        dialogue(1, '我进来了。'), dialogue(2, '看到了。'), dialogue(3, '我也到了。'),
        dialogue(4, '好。'), restraint(1, '我先看看题目。', 60)
      ]
    }),
    stage({
      id: 'G6-B', title: '群体能量镜像', groupCode: 'G06',
      processState: 'positive_collaboration', canonicalSubState: 'standard',
      preferredStrategyId: 'EA-006', allowedStrategyIds: ['EA-006', 'SS-001'], shouldIntervene: true,
      expectedEvidenceDescription: 'High enthusiasm and mutual encouragement remain routine collaboration; no distinct risk state requires intervention.',
      messages: [
        dialogue(1, '这个任务还挺有意思，大家都愿意把想法说出来。'),
        dialogue(2, '对，我很想听听每个人最关注的方向。', { minimumGapBeforeSeconds: 30 }),
        dialogue(3, '我也觉得先把不同看法都摆出来会更清楚。', { minimumGapBeforeSeconds: 30 }),
        dialogue(4, '大家回应得都很快，气氛特别好。', { minimumGapBeforeSeconds: 30 }),
        dialogue(1, '而且没有人打断别人，讨论起来很舒服。', { minimumGapBeforeSeconds: 30 }),
        dialogue(2, '这版比我预想得顺，感觉可以继续保持这个节奏。', { minimumGapBeforeSeconds: 30 }),
        trigger(3, '对，大家现在思路特别多，但先听完彼此的想法。', { minimumGapBeforeSeconds: 20 })
      ]
    }),
    stage({
      id: 'G6-C', title: '节奏性停顿觉察', groupCode: 'G06',
      processState: 'conflict_tension', canonicalSubState: 'standard',
      expectedOverlayTags: ['high_intensity_overload'],
      preferredStrategyId: 'EA-005', allowedStrategyIds: ['EA-005', 'ER-004'], shouldIntervene: true,
      expectedEvidenceDescription: 'The group is overloaded by simultaneous options and changing plans, but the disagreement is not personalized; overload is an overlay on a routine primary state.',
      messages: [
        dialogue(1, '安静区先保留，预算比例放到比较表里。'),
        dialogue(2, '讨论区也作为备选，用同一套需求标准比较。', { minimumGapBeforeSeconds: 35 }),
        dialogue(3, '预约系统的成本和管理收益我补到第三列。', { minimumGapBeforeSeconds: 35 }),
        dialogue(4, '数字区也先保留为备选，暂时不急着删除。', { minimumGapBeforeSeconds: 35 }),
        dialogue(1, '六个项目都放进比较表，按相同顺序查看。', { minimumGapBeforeSeconds: 35 }),
        dialogue(2, '我补需求覆盖列，信息会多一些但步骤很清楚。', { minimumGapBeforeSeconds: 35 }),
        dialogue(3, '我补预算、放弃理由和评价指标三列。', { minimumGapBeforeSeconds: 35 }),
        dialogue(4, '当前项目组合已经记好，稍后把备选一起排序。', { minimumGapBeforeSeconds: 35 }),
        dialogue(1, '表里先保留四项目方案作为一个备选。', { minimumGapBeforeSeconds: 35 }),
        dialogue(2, '三项目方案的需求表也列好了，接下来和四项目方案并排比较。', { minimumGapBeforeSeconds: 35 }),
        dialogue(3, '现在三套方案的信息都齐了，选项同时摆在眼前有点多，但大家还在正常讨论。', { minimumGapBeforeSeconds: 35 }),
        dialogue(1, '需求、预算、理由和指标同时展开，信息量明显过载，不过没有人受挫或争执。', { minimumGapBeforeSeconds: 35 }),
        trigger(4, '大家讨论很投入，没有争执或受挫；只是多套方案信息同时出现，需要先按预算和需求整理顺序再继续。', { minimumGapBeforeSeconds: 35 }),
        continuation(1, '每个人先只保留一个最清晰的判断。'),
        continuation(2, '我最清晰的是安静区不能删。'),
        continuation(4, '我先把安静区和预约系统这两个共识写下来，再处理剩余项目。')
      ]
    }),
    stage({
      id: 'G6-D', title: '最终积极协作收尾', groupCode: 'G06',
      processState: 'positive_collaboration', canonicalSubState: 'execution_progress',
      allowedStrategyIds: ['OI-004'], inhibitionStrategyId: 'OI-004',
      detectedSelfRegulation: true, shouldIntervene: false,
      messages: [
        dialogue(1, '最终组合确定为安静区、讨论区和预约系统。'),
        dialogue(2, '总预算八万，保留两万作为设备和维护余量。'),
        dialogue(3, '放弃数字区，因为和现有机房资源重复且成本较高。'),
        dialogue(4, '指标用座位使用率、讨论区预约率和噪声投诉数量。'),
        dialogue(1, '我合并正文。'), dialogue(2, '我核预算。'), dialogue(3, '我检查理由。'),
        restraint(4, '我检查指标，完成后提交。', 90)
      ]
    })
  ]
};

function scheduleGroupFlow(groupCode, stages) {
  const groupIndex = GROUP_CODES.indexOf(groupCode);
  const normalizedGroupIndex = Math.max(0, groupIndex);
  const startAtSeconds = GROUP_START_SECONDS + normalizedGroupIndex * GROUP_START_STAGGER_SECONDS;
  const targetEndAtSeconds = (
    DISCUSSION_DURATION_SECONDS -
    SCRIPT_SETTLE_HEADROOM_SECONDS -
    (GROUP_CODES.length - 1 - normalizedGroupIndex) * GROUP_END_STAGGER_SECONDS
  );
  const entries = [];
  const gaps = [];

  for (const item of stages) {
    item.messages.forEach((message, index) => {
      if (entries.length > 0) {
        const isStageBoundary = index === 0;
        gaps.push({
          minimumSeconds: isStageBoundary
            ? STAGE_BOUNDARY_GAP_SECONDS
            : Math.max(
              DEFAULT_MESSAGE_GAP_SECONDS,
              Number(message.minimumGapBeforeSeconds || 0)
            ),
          slackWeight: isStageBoundary ? STAGE_BOUNDARY_SLACK_WEIGHT : 1
        });
      }
      entries.push({ item, message });
    });
  }

  const minimumGapSeconds = gaps.reduce((sum, gap) => sum + gap.minimumSeconds, 0);
  const availableGapSeconds = targetEndAtSeconds - startAtSeconds;
  if (minimumGapSeconds > availableGapSeconds) {
    throw new Error(
      `${groupCode} needs ${minimumGapSeconds}s for protected message gaps, ` +
      `but only ${availableGapSeconds}s is available in the 40-minute discussion.`
    );
  }
  const totalSlackWeight = gaps.reduce((sum, gap) => sum + gap.slackWeight, 0);
  const slackPerWeight = totalSlackWeight
    ? (availableGapSeconds - minimumGapSeconds) / totalSlackWeight
    : 0;
  let atSeconds = startAtSeconds;

  return entries.map(({ item, message }, index) => {
    const gap = index > 0 ? gaps[index - 1] : null;
    if (gap) {
      atSeconds += gap.minimumSeconds + slackPerWeight * gap.slackWeight;
    }
    return {
      ...message,
      atSeconds: Math.round(atSeconds),
      minimumActualGapBeforeSeconds: gap ? gap.minimumSeconds : 0,
      groupCode,
      memberNo: message.memberNo,
      studentId: `S1-${groupCode}-M${message.memberNo}`,
      scenarioId: item.id,
      state: item.canonicalSubState,
      phase: message.phase || 'scenario'
    };
  });
}

const scenarios = GROUP_CODES.flatMap((groupCode) => groupFlows[groupCode]);
const messages = GROUP_CODES.flatMap((groupCode) => scheduleGroupFlow(groupCode, groupFlows[groupCode]));
const lastMessageAtMs = Math.max(...messages.map((message) => message.atSeconds * 1000));
const expectedStrategyIds = allManifestStrategyIds(strategyRouteManifest);

module.exports = {
  ...base,
  name: 'six-group-strategy-coverage',
  totalStudents: 24,
  groupCount: 6,
  membersPerGroup: 4,
  minReadyStudents: 24,
  maxWarmupWaitMs: 12 * 60 * 1000,
  timeouts: {
    ...base.timeouts,
    discussionEnterMs: 3 * 60 * 1000,
    expectedPipelineDiscoveryMaxWaitMs: 210 * 1000
  },
  discussionDurationMs: DISCUSSION_DURATION_SECONDS * 1000,
  rampUp: { batchSize: 4, intervalMs: 20 * 1000, byGroup: true },
  flow: {
    ...base.flow,
    fullFlow: true,
    submitPreQuestionnaires: true,
    submitPostQuestionnaires: false,
    submitPostCheckin: false,
    submitDeliverable: false,
    groupCheckins: [0, 0],
    groupHelps: [0, 0],
    captureConversation: true,
    actionTickMs: 2000,
    discussionEnterAttempts: 6,
    discussionEnterRetryDelayMs: 1500,
    discussionReadyRefreshAttempts: 2,
    discussionReadyRefreshAfterMs: 15 * 1000,
    stopAfterScriptedMessagesComplete: false,
    verifyAiInputLock: true,
    verifyAiInputLockApiReject: true,
    aiInputLockSendRetries: 3,
    aiInputLockPollMs: 1500,
    enforceScriptedInterventionExpectations: true,
    continueAfterScriptBoundaryFailure: true,
    expectedInterventionMaxWaitMs: 4 * 60 * 1000,
    expectedNoInterventionPollMs: 2000,
    scriptedGroupActions: []
  },
  strategyAudit: {
    enabled: false,
    requireCleanContext: false,
    expectedEmotionAgentEnabled: false,
    settleMs: 30 * 1000,
    maxWaitMs: 8 * 60 * 1000,
    pollIntervalMs: 5 * 1000,
    requireActualCoverage: false,
    interventionAcceptance: {
      baselinePublishedCases: 2,
      minPublishedInterventionCases: 18,
      expectedPublishedCaseCount: 21,
      expectedInhibitionCaseCount: 5
    }
  },
  stateSuite: {
    enabled: false,
    mode: 'six-group-strategy-coverage',
    groupCodes: GROUP_CODES,
    isolation: 'fresh_shared_session_six_group_discussions',
    postDialogueWaitMs: 30 * 1000,
    requireAgentLockRecovery: true,
    scenarios
  },
  scriptedDiscussion: {
    description: 'Six isolated four-student groups execute distinct strategy-coverage flows in one shared lesson/task.',
    timeline: {
      discussionDurationSeconds: DISCUSSION_DURATION_SECONDS,
      settleHeadroomSeconds: SCRIPT_SETTLE_HEADROOM_SECONDS,
      defaultMessageGapSeconds: DEFAULT_MESSAGE_GAP_SECONDS,
      stageBoundaryGapSeconds: STAGE_BOUNDARY_GAP_SECONDS,
      lastMessageAtSeconds: lastMessageAtMs / 1000
    },
    scenarios,
    expectedRuleStates: scenarios.map((item) => ({
      state: item.canonicalSubState,
      groupCode: item.groupCode,
      scenarioId: item.id
    })),
    expectedStrategyIds,
    mustCoverAllExpectedStrategies: false,
    allowRepeatedCanonicalSubStates: true,
    requireIsolatedDiscussions: false,
    verificationMode: 'strict-coverage-with-nonfatal-stage-boundaries',
    repeatForEachGroup: false,
    messages
  }
};

module.exports.GROUP_CODES = GROUP_CODES;
module.exports.groupFlows = groupFlows;
module.exports.scenarios = scenarios;
