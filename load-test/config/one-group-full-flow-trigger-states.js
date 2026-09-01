const base = require('./trigger-states-4p');
const { routeForScenario, unique: uniqueRouteValues } = require('../src/strategyRouteManifest');

const SCRIPT_SEED = 20260728;
const FULL_DIALOGUE_DURATION_SECONDS = 60 * 60;
const FULL_DIALOGUE_SEND_WINDOW_SECONDS = 5 * 60;

function dialogue(memberNo, text, options = {}) {
  return { memberNo, text, ...options };
}

function interventionTrigger(memberNo, text, options = {}) {
  return dialogue(memberNo, text, {
    waitForExpectedInterventionAfter: true,
    ...options
  });
}

function continuation(memberNo, text, options = {}) {
  return dialogue(memberNo, text, { phase: 'continuation', ...options });
}

function restraintCheck(memberNo, text, options = {}) {
  return dialogue(memberNo, text, {
    waitForNoInterventionAfter: true,
    noInterventionObservationSeconds: 30,
    ...options
  });
}

const stateScenarios = [
  {
    id: 'S01',
    title: '证据不足',
    groupCode: 'G01',
    canonicalSubState: 'unknown_sub_state',
    expectedProcessState: 'unknown_sub_state',
    shouldIntervene: false,
    skipStrategyCoverage: true,
    allowedStrategyIds: [],
    windowSeconds: [0, 60],
    messages: [
      dialogue(1, '大家都能看到任务页面吗？'),
      dialogue(2, '能。'),
      dialogue(3, '我也进来了。'),
      restraintCheck(4, '收到。')
    ]
  },
  {
    id: 'S02',
    title: '标准型协作',
    groupCode: 'G01',
    canonicalSubState: 'standard',
    shouldIntervene: false,
    allowedStrategyIds: ['SS-001', 'EA-006'],
    windowSeconds: [60, 240],
    messages: [
      dialogue(1, '我先看需求数据，44%的学生希望增加安静学习座位，个人学习应该优先考虑。'),
      dialogue(2, '但35%希望有小组讨论空间，所以不能只做安静学习区。'),
      dialogue(3, '预约和噪声管理系统虽然不增加座位，但能缓解空间冲突。'),
      dialogue(4, '数字设备也有27%的需求，后面可以比较一下值不值得花2.5万元。'),
      dialogue(1, '先每个人提一套组合，再按照预算和四个硬性条件筛选吧。'),
      restraintCheck(3, '可以，这样不会一开始就只盯某一个项目。')
    ]
  },
  {
    id: 'S03',
    title: '个体边缘化',
    groupCode: 'G01',
    canonicalSubState: 'individual_marginalization',
    shouldIntervene: true,
    allowedStrategyIds: ['SS-002', 'EA-003', 'EE-005'],
    windowSeconds: [240, 450],
    messages: [
      dialogue(4, '我觉得灵活学习区也值得考虑，它能同时用于自习、讨论和展示。'),
      dialogue(2, '我先算安静区4万元加讨论区3.5万元。'),
      dialogue(1, '再加预约系统1.5万元就是9万元，刚好三个项目。'),
      dialogue(3, '这个组合的噪声管理比较完整。'),
      dialogue(2, '评价指标可以看使用率和投诉数量。'),
      dialogue(1, '那先按这个方向继续。'),
      dialogue(3, '我觉得可以。'),
      interventionTrigger(2, '我也没意见。'),
      continuation(2, '刚才D提到灵活学习区，我们回头比较一下它和讨论区的区别。'),
      continuation(4, '我担心灵活区管理要求高，但它能兼顾两种用途，所以值得放在备选方案里。')
    ]
  },
  {
    id: 'S04',
    title: '深度思考',
    groupCode: 'G01',
    canonicalSubState: 'deep_thinking',
    shouldIntervene: false,
    inhibitionStrategyId: 'OI-002',
    allowedStrategyIds: ['OI-002'],
    windowSeconds: [450, 630],
    messages: [
      dialogue(1, '我把几套组合和预算都算一下，大家先等我一会儿。'),
      dialogue(3, '我也对照一下“个人学习、小组协作、噪声和维护”这四个条件。'),
      dialogue(1, '算好了。A方案是安静区、讨论区和预约系统，共9万元。', { minimumGapBeforeSeconds: 105 }),
      dialogue(1, 'B方案是安静区、灵活区和数字区，共9.5万元。'),
      dialogue(1, 'C方案是讨论区、灵活区和数字区，共9万元。'),
      restraintCheck(3, 'A方案的噪声控制最好，B方案功能比较多，C方案缺少明确的安静区。')
    ]
  },
  {
    id: 'S05',
    title: '困惑型',
    groupCode: 'G01',
    canonicalSubState: 'confusion',
    shouldIntervene: true,
    allowedStrategyIds: ['ER-005', 'EA-001', 'EE-006'],
    windowSeconds: [630, 900],
    messages: [
      dialogue(2, '三套方案都说得通，我现在反而不知道应该先定核心目标，还是先定项目。'),
      dialogue(4, '我也有点乱，灵活区算不算同时兼顾个人和小组，还是必须另外选择安静区或讨论区？'),
      dialogue(1, '预算、需求比例、噪声、维护全挤在一起，不知道从哪一步拆。'),
      interventionTrigger(2, '感觉每个条件都重要，但没有一个明确的开始位置。', {
        // Keep the recovery batch active across the first real five-minute
        // emotion slot so batch 6 exercises defer/suppress coordination.
        minimumPauseSeconds: 165
      }),
      continuation(3, '那我们先只回答一个问题：最优先解决的是“缺少安静座位”，还是“空间功能不够综合”？'),
      continuation(1, '我选缺少安静座位，44%是最高需求，而且没有明确安静区很难保障个人学习。'),
      continuation(4, '我同意先定核心目标，再看哪些项目支撑这个目标。')
    ]
  },
  {
    id: 'S06',
    title: '建设性冲突',
    groupCode: 'G01',
    canonicalSubState: 'constructive_conflict',
    shouldIntervene: false,
    inhibitionStrategyId: 'OI-001',
    detectedSelfRegulation: true,
    allowedStrategyIds: ['OI-001'],
    windowSeconds: [900, 1140],
    messages: [
      dialogue(2, '但如果只看44%，会忽略35%的讨论需求，我更倾向灵活区、讨论区和数字区。', {
        minimumGapBeforeSeconds: 30
      }),
      dialogue(1, '我还是倾向安静区、讨论区和预约系统，至少个人和小组都有明确空间。'),
      dialogue(2, '我的担心是预约系统花了1.5万元，却不能直接增加学习空间。'),
      dialogue(3, '我们先别争具体组合，比较三个标准：需求覆盖、噪声风险和维护成本。'),
      dialogue(4, '可以把两套方案分别按这三个标准打分，再看哪一种短板更能接受。'),
      dialogue(1, '那我给安静、讨论、预约这个组合：覆盖4分、噪声5分、维护4分。'),
      dialogue(2, '我给灵活、讨论、数字这个组合：覆盖5分、噪声2分、维护2分。'),
      dialogue(3, '这样看，我们的分歧其实是“功能丰富”和“风险可控”的取舍。'),
      restraintCheck(4, '那就继续比较，不需要马上判断谁对谁错。')
    ]
  },
  {
    id: 'S07',
    title: '人际性冲突',
    groupCode: 'G01',
    canonicalSubState: 'interpersonal_conflict',
    shouldIntervene: true,
    allowedStrategyIds: ['ER-001', 'EE-001', 'SS-004', 'ER-007'],
    windowSeconds: [1140, 1380],
    messages: [
      dialogue(2, '可你们一直拿噪声当理由，感觉就是不愿意认真考虑新方案。'),
      dialogue(1, '不是不考虑，是你的方案根本没有真正的安静区。'),
      dialogue(2, '“根本没有”太绝对了，灵活区当然可以安静学习。'),
      dialogue(1, '但你每次都只说可以，具体怎么管理又说不清。'),
      dialogue(2, '你也别总把自己的标准当唯一标准。'),
      dialogue(1, '至少我的方案满足条件，不像你的方案全靠假设。'),
      interventionTrigger(4, '我感觉现在开始是在否定人，而不是比较方案了。'),
      continuation(3, '我们把人和观点分开。A担心噪声，B担心功能单一，这两个顾虑都合理。'),
      continuation(1, '好，我收回“根本没有”。我具体担心的是灵活区分时管理会不会失效。'),
      continuation(2, '我也具体回答，可以设置安静时段和讨论时段，但确实会增加管理成本。')
    ]
  },
  {
    id: 'S08',
    title: '心理安全风险',
    groupCode: 'G01',
    canonicalSubState: 'interpersonal_conflict',
    expectedOverlayTags: ['psychological_safety_risk'],
    shouldIntervene: true,
    selectedStrategyId: 'SS-004',
    allowedStrategyIds: ['SS-004'],
    windowSeconds: [1380, 1620],
    messages: [
      dialogue(4, '那能不能保留灵活区，再用预约规则划分安静和讨论时段？'),
      dialogue(2, '这个想法太理想化了，学生不会按规则来。'),
      dialogue(4, '好吧，当我没说。'),
      dialogue(1, '我们还是回到安静区加讨论区吧。'),
      dialogue(3, '预约系统可以和两个固定区域一起使用。'),
      dialogue(1, '预算是9万元，还能剩1万元。'),
      interventionTrigger(2, '我还是觉得不选数字区有点可惜。'),
      continuation(3, 'D刚才说的分时规则其实可以放进实施措施，不一定非要和灵活区绑定。'),
      continuation(2, '对，我刚才说得太满了。分时管理可以作为备选措施。'),
      continuation(4, '那我补充一下，预约系统可以设置讨论区时段、人数上限和超时释放。')
    ]
  },
  {
    id: 'S09',
    title: '高强度过载',
    groupCode: 'G01',
    canonicalSubState: 'standard',
    expectedOverlayTags: ['high_intensity_overload'],
    shouldIntervene: true,
    allowedStrategyIds: ['EA-005', 'ER-004'],
    windowSeconds: [1620, 1800],
    messages: [
      dialogue(1, '那核心目标先写“兼顾安静学习和小组协作”！'),
      dialogue(2, '还要写为什么没有选择数字区！'),
      dialogue(3, '噪声风险、设备维护、安全出口都要考虑！'),
      dialogue(4, '评价指标先定使用率、投诉数量和满意度！'),
      dialogue(1, '实施步骤是不是先划分区域再试运行？'),
      dialogue(2, '还需要问卷和预约数据！'),
      dialogue(3, '但投诉数量不一定等于实际噪声水平！'),
      dialogue(4, '那增加峰值分贝？'),
      dialogue(1, '预算表也还没有最终确认！'),
      dialogue(2, '风险应对还要写谁负责！'),
      dialogue(3, '现在同时开了太多问题，谁都没有说完！'),
      interventionTrigger(4, '我已经不知道先接哪一条了！'),
      continuation(3, '先只完成项目组合和预算，其他内容等组合确定以后再写。'),
      continuation(1, '同意，先收束到一个决定。')
    ]
  },
  {
    id: 'S10',
    title: '挫败型',
    groupCode: 'G01',
    canonicalSubState: 'frustration',
    shouldIntervene: true,
    allowedStrategyIds: ['ER-002', 'EE-003', 'SS-006', 'EA-007'],
    windowSeconds: [1800, 2100],
    messages: [
      dialogue(1, '我想把安静区、讨论区、数字区和预约系统都保留，4加3.5加2.5加1.5等于11.5万元，超了。'),
      dialogue(2, '那安静区、讨论区和灵活区也要10.5万元，还是超。'),
      dialogue(4, '换成安静区、灵活区和数字区是9.5万元，但没有明确的讨论区。'),
      dialogue(3, '讨论区、灵活区和数字区是9万元，但没有明确的安静区。'),
      dialogue(1, '怎么换都会漏一个重要条件。'),
      dialogue(2, '我们已经算了好几遍了，还是卡着。'),
      dialogue(4, '感觉怎么选都不完整。'),
      interventionTrigger(1, '预算限制让每个方案都有明显缺点。'),
      continuation(3, '既然预算迫使我们取舍，就把“为什么不选数字区”写成明确的放弃理由。'),
      continuation(1, '那回到安静区、讨论区和预约系统，共9万元，至少四个硬条件都能直接说明。'),
      continuation(2, '可以。数字需求可以通过基础充电插座补充，但不建设完整数字学习区。'),
      continuation(4, '这个取舍本身也能写进选择依据。')
    ]
  },
  {
    id: 'S11',
    title: '倦怠型',
    groupCode: 'G01',
    canonicalSubState: 'burnout',
    shouldIntervene: true,
    selectedStrategyId: 'ER-008',
    allowedStrategyIds: ['ER-008'],
    windowSeconds: [2100, 2340],
    messages: [
      dialogue(2, '说实话，学校最后也不一定真的采用我们的方案，选哪个好像都差不多。'),
      dialogue(4, '我也有点没劲，反正只是一个模拟任务。'),
      dialogue(1, '要不随便凑三个项目交了算了。'),
      dialogue(3, '讨论这么久，继续改也不知道有什么意义。'),
      interventionTrigger(2, '差不多能交就行了，不想再比较了。'),
      continuation(4, '如果只能保留一个，我最想保留安静学习区，平时确实最缺。'),
      continuation(2, '我最想保留讨论区，因为现在很难找到能正常讨论的地方。'),
      continuation(3, '那这两个就是我们真正想解决的问题，再用预约系统控制它们之间的冲突。'),
      continuation(1, '这样核心目标就不是凑三个项目，而是让安静学习和小组讨论能够共存。')
    ]
  },
  {
    id: 'S12',
    title: '跑题脱离（无自调节）',
    groupCode: 'G01',
    canonicalSubState: 'off_topic_unregulated',
    shouldIntervene: true,
    selectedStrategyId: 'ER-003',
    allowedStrategyIds: ['ER-003'],
    windowSeconds: [2340, 2520],
    messages: [
      dialogue(2, '说到找地方，上次图书馆一楼空调也太冷了。'),
      dialogue(1, '对，而且插座一半都是坏的。'),
      dialogue(4, '我们宿舍楼下那个自习室更离谱，晚上一直有人吃外卖。'),
      dialogue(3, '食堂二楼其实挺适合讨论，就是太吵。'),
      dialogue(2, '说起来新食堂那家面还不错。'),
      dialogue(1, '我还没有去，下次可以一起。'),
      interventionTrigger(4, '听说他们晚上还有套餐。', {
        minimumPauseSeconds: 65
      }),
      continuation(3, '回到任务，刚才已经定的是安静区、讨论区和预约系统。'),
      continuation(1, '我把项目组合和预算写进文档。')
    ]
  }
];

stateScenarios.push(
  {
    id: 'S13',
    title: '跑题脱离（有自调节）',
    groupCode: 'G01',
    canonicalSubState: 'off_topic_self_regulated',
    shouldIntervene: false,
    inhibitionStrategyId: 'OI-003',
    detectedSelfRegulation: true,
    allowedStrategyIds: ['OI-003'],
    windowSeconds: [2520, 2640],
    messages: [
      dialogue(4, '预约系统要不要顺便加入空调报修功能？'),
      dialogue(2, '这个以后好像也可以扩展。'),
      dialogue(3, '我们又开始扩题了，当前任务只需要控制预约和噪声，先把第一周方案写完。'),
      dialogue(1, '对，先回到风险和评价指标。'),
      dialogue(4, '好，空调报修不写了。'),
      restraintCheck(2, '那接着写预约冲突怎么处理。')
    ]
  },
  {
    id: 'S14',
    title: '敷衍脱离',
    groupCode: 'G01',
    canonicalSubState: 'perfunctory_detachment',
    shouldIntervene: true,
    allowedStrategyIds: ['ER-006', 'SS-005'],
    windowSeconds: [2640, 2880],
    messages: [
      dialogue(2, '剩下你们写吧，我都行。'),
      dialogue(1, '风险和指标还没有分工，不能只剩两个人完成。'),
      dialogue(2, '反正前面都说过了，复制进去差不多。'),
      dialogue(4, '我也不知道写什么，你们决定就行。'),
      dialogue(3, '现在方案主线已经有了，但任务都压在A和我这边。'),
      dialogue(2, '那你们看着办吧。'),
      interventionTrigger(4, '最后发我看一下就行。'),
      continuation(1, '重新分一下。我写核心目标和预算，B写选择依据，C写风险，D写评价指标。'),
      continuation(2, '行，我负责解释为什么放弃灵活区和数字区。'),
      continuation(4, '那我负责写三个可以测量的指标。'),
      continuation(3, '我写噪声、安全和长期维护风险。')
    ]
  },
  {
    id: 'S15',
    title: '执行推进',
    groupCode: 'G01',
    canonicalSubState: 'execution_progress',
    shouldIntervene: false,
    inhibitionStrategyId: 'OI-004',
    allowedStrategyIds: ['OI-004'],
    windowSeconds: [2880, 3240],
    messages: [
      dialogue(3, '我写噪声外溢、预约冲突和长期维护三个风险。'),
      dialogue(1, '核心目标已经写好。'),
      dialogue(2, '选择依据我来。'),
      dialogue(4, '指标写座位使用率、噪声投诉率、预约履约率，可以吗？'),
      dialogue(3, '可以，再把满意度放到后续改进里。'),
      dialogue(1, '预算表是4加3.5加1.5，共9万元。'),
      dialogue(2, '收到，我把放弃理由补上。'),
      dialogue(4, '三个指标已经填好。'),
      dialogue(3, '风险部分完成。'),
      dialogue(1, '我开始做最后一致性检查。'),
      restraintCheck(2, '我的部分也完成了。')
    ]
  },
  {
    id: 'S16',
    title: '阶段性成果',
    groupCode: 'G01',
    canonicalSubState: 'execution_progress',
    expectedOverlayTags: ['stage_achievement'],
    shouldIntervene: false,
    inhibitionStrategyId: 'OI-004',
    allowedStrategyIds: ['OI-004'],
    windowSeconds: [3240, 3480],
    messages: [
      dialogue(1, '我检查了一遍，三个项目共9万元，同时覆盖个人学习和小组协作。'),
      dialogue(2, '选择依据用了44%的安静需求和35%的讨论需求，也解释了为什么不选数字区和灵活区。'),
      dialogue(3, '实施步骤以及噪声、安全和维护风险都写好了。'),
      dialogue(4, '三个指标也齐了：座位使用率、预约履约率和噪声投诉率。'),
      dialogue(2, '刚才最卡的是项目取舍，现在终于完整了。'),
      dialogue(3, '而且我们把前面的争论变成了明确的选择理由。'),
      dialogue(1, '大家都确认提交吗？'),
      dialogue(2, '确认。'),
      dialogue(3, '确认。'),
      restraintCheck(4, '确认，可以提交。')
    ]
  }
);

for (const stateScenario of stateScenarios) {
  const route = routeForScenario(stateScenario);
  const primaryStrategyIds = route.primaryRoute && route.primaryRoute.primary_strategy_ids;
  const expectedPrimaryStrategyId = Array.isArray(primaryStrategyIds) && primaryStrategyIds.length
    ? primaryStrategyIds[0]
    : null;
  stateScenario.expected_primary_strategy_id = expectedPrimaryStrategyId;
  stateScenario.expectedPrimaryStrategyId = expectedPrimaryStrategyId;
}

const overlayScenarios = stateScenarios.filter(
  (item) => Array.isArray(item.expectedOverlayTags) && item.expectedOverlayTags.length
);

const modelFailureScenarios = [
  {
    id: 'F01',
    title: '状态模型失败降级',
    groupCode: 'G01',
    canonicalSubState: 'confusion',
    expectedAssessmentStatus: 'unclassified',
    expectedFailureFallback: true,
    skipStrategyCoverage: true,
    shouldIntervene: false,
    allowedStrategyIds: [],
    messages: [
      dialogue(1, '我们面对预算、需求和维护条件，还没有共同的起点。'),
      dialogue(2, '先讨论哪一个条件都可能影响后面的选择。'),
      dialogue(3, '我不确定应该先筛项目还是先定评价标准。'),
      dialogue(4, '大家暂时都没有形成可执行的共同方向。')
    ]
  }
];

const agentLockScenarios = [
  {
    ...stateScenarios.find((item) => item.id === 'S05'),
    id: 'L01',
    title: '策略 Agent 介入与房间锁'
  },
  {
    id: 'L02',
    title: '双 Agent 后恢复输入',
    groupCode: 'G01',
    canonicalSubState: 'standard',
    shouldIntervene: false,
    allowedStrategyIds: ['SS-001', 'EA-006'],
    messages: [
      dialogue(3, '我们先回应刚才的提示，暂停针对人的说法，只比较方案标准。', {
        minimumGapBeforeSeconds: 45
      }),
      dialogue(1, '可以，我把关注点改成预算和覆盖人数，不再评价合作态度。'),
      dialogue(2, '我也同意，先听完整理由，再按同样标准比较。'),
      dialogue(4, '现在房间可以继续输入，我们回到安静区、讨论区和预约系统这个组合。'),
      dialogue(1, '这个组合兼顾个人与小组需求，预算也在范围内。'),
      dialogue(2, '我补充维护角度：预约系统成本低，也能减少噪声冲突。'),
      dialogue(3, '大家的观点已经能互相补充，没有必要再次介入。'),
      dialogue(4, '形成初步共识后，再一起检查安全与长期维护。')
    ]
  }
];

function seededWeights(count, salt) {
  let seed = SCRIPT_SEED;
  for (const ch of String(salt || '')) seed = (seed * 31 + ch.charCodeAt(0)) >>> 0;
  return Array.from({ length: count }, () => {
    seed = (seed + 0x6D2B79F5) >>> 0;
    let value = seed;
    value = Math.imul(value ^ (value >>> 15), value | 1);
    value ^= value + Math.imul(value ^ (value >>> 7), value | 61);
    return 0.85 + (((value ^ (value >>> 14)) >>> 0) / 4294967296) * 0.3;
  });
}

function scenarioDurationSeconds(stateScenario) {
  const window = stateScenario.windowSeconds;
  if (Array.isArray(window) && window.length === 2) {
    return Math.max(20, Number(window[1]) - Number(window[0]));
  }
  return Math.max(45, (stateScenario.messages || []).length * 15);
}

function scenarioMessageTimes(stateScenario, startAtSeconds) {
  const messages = stateScenario.messages || [];
  if (messages.length <= 1) return [startAtSeconds + 5];
  const usableSeconds = Math.max(messages.length - 1, scenarioDurationSeconds(stateScenario) - 10);
  const fixedGaps = messages.slice(1).map((message) => (
    Math.max(0, Number(message.minimumGapBeforeSeconds || 0))
  ));
  const fixedTotal = fixedGaps.reduce((sum, value) => sum + value, 0);
  const flexibleIndexes = fixedGaps
    .map((value, index) => (value > 0 ? null : index))
    .filter((value) => value !== null);
  const flexibleSeconds = Math.max(flexibleIndexes.length, usableSeconds - fixedTotal);
  const weights = seededWeights(flexibleIndexes.length, stateScenario.id);
  const weightTotal = weights.reduce((sum, value) => sum + value, 0) || 1;
  flexibleIndexes.forEach((gapIndex, weightIndex) => {
    fixedGaps[gapIndex] = Math.max(
      1,
      Math.round(flexibleSeconds * weights[weightIndex] / weightTotal)
    );
  });

  const times = [startAtSeconds + 5];
  fixedGaps.forEach((gap) => times.push(times[times.length - 1] + gap));
  return times;
}

function messagesForScenario(stateScenario, startAtSeconds = 0) {
  const times = scenarioMessageTimes(stateScenario, startAtSeconds);
  return stateScenario.messages.map((message, index) => {
    return {
      ...message,
      atSeconds: times[index],
      studentId: `S1-${stateScenario.groupCode}-M${message.memberNo}`,
      groupCode: stateScenario.groupCode,
      scenarioId: stateScenario.id,
      state: stateScenario.canonicalSubState,
      phase: message.phase || 'scenario'
    };
  });
}

function recoveryBridgeMessages(stateScenario, startAtSeconds) {
  const bridge = [
    dialogue(3, '我们先回应刚才的 Agent 提示，暂停风险表达并回到任务标准。', {
      minimumGapBeforeSeconds: 45
    }),
    dialogue(1, '收到。我会只说明方案理由，不再评价成员本人。'),
    dialogue(2, '我也同意，先听完彼此依据，再比较预算和需求。'),
    dialogue(4, '现在已恢复中性协作，确认可以继续输入后再进入下一场景。')
  ];
  const bridgeScenario = {
    id: `${stateScenario.id}-RECOVERY`,
    groupCode: stateScenario.groupCode,
    canonicalSubState: 'recovery_bridge',
    windowSeconds: [0, 90],
    messages: bridge
  };
  return messagesForScenario(bridgeScenario, startAtSeconds).map((message, index) => {
    return {
      ...message,
      phase: 'recovery',
      bridgeIndex: index + 1
    };
  });
}

function fitMessagesIntoSendWindow(messages, sendWindowSeconds) {
  if (!sendWindowSeconds || messages.length < 2) return messages;

  const firstAtSeconds = Number(messages[0].atSeconds || 0);
  const lastAtSeconds = Number(messages[messages.length - 1].atSeconds || 0);
  if (lastAtSeconds <= sendWindowSeconds) return messages;

  const gapPlans = messages.slice(1).map((message, index) => {
    const originalGapSeconds = Math.max(
      1,
      Number(message.atSeconds) - Number(messages[index].atSeconds)
    );
    const minimumGapSeconds = Math.max(1, Number(message.minimumGapBeforeSeconds || 0));
    return {
      minimumGapSeconds,
      flexibleGapSeconds: Math.max(0, originalGapSeconds - minimumGapSeconds)
    };
  });
  const minimumGapTotal = gapPlans.reduce((sum, gap) => sum + gap.minimumGapSeconds, 0);
  const flexibleGapTotal = gapPlans.reduce((sum, gap) => sum + gap.flexibleGapSeconds, 0);
  const availableFlexibleSeconds = sendWindowSeconds - firstAtSeconds - minimumGapTotal;
  if (availableFlexibleSeconds < 0) {
    throw new Error(
      `Scripted send window ${sendWindowSeconds}s cannot preserve required message gaps ` +
      `(${firstAtSeconds + minimumGapTotal}s minimum).`
    );
  }

  const flexibleScale = flexibleGapTotal > 0
    ? availableFlexibleSeconds / flexibleGapTotal
    : 0;
  let scheduledAtSeconds = firstAtSeconds;
  return messages.map((message, index) => {
    if (index > 0) {
      const gap = gapPlans[index - 1];
      scheduledAtSeconds += gap.minimumGapSeconds + gap.flexibleGapSeconds * flexibleScale;
    }
    return {
      ...message,
      atSeconds: Math.round(scheduledAtSeconds * 1000) / 1000
    };
  });
}

function messagesForActiveSession(scenarios, {
  includeRecoveryBridges = false,
  sendWindowSeconds = null
} = {}) {
  const messages = [];
  let cursorSeconds = 0;
  scenarios.forEach((stateScenario, index) => {
    const scenarioMessages = messagesForScenario(stateScenario, cursorSeconds);
    messages.push(...scenarioMessages);
    cursorSeconds += scenarioDurationSeconds(stateScenario);
    if (includeRecoveryBridges && stateScenario.shouldIntervene && index < scenarios.length - 1) {
      const bridgeMessages = recoveryBridgeMessages(stateScenario, cursorSeconds);
      messages.push(...bridgeMessages);
      cursorSeconds += 90;
    }
  });
  return fitMessagesIntoSendWindow(messages, sendWindowSeconds);
}

function buildScenario(scenarios, {
  suiteName,
  description,
  includeRecoveryBridges = false,
  expectedEmotionAgentEnabled = false,
  requireAgentLockRecovery = false,
  requireModelFailureFallback = false,
  strictCoverage = true
}) {
  const sendWindowSeconds = scenarios === stateScenarios
    ? FULL_DIALOGUE_SEND_WINDOW_SECONDS
    : null;
  const messages = messagesForActiveSession(scenarios, {
    includeRecoveryBridges,
    sendWindowSeconds
  });
  const lastMessageAtMs = Math.max(...messages.map((message) => Number(message.atSeconds) * 1000));
  const expectedStrategyIds = uniqueRouteValues(
    scenarios.flatMap((stateScenario) => routeForScenario(stateScenario).runtimeAllowedStrategyIds)
  );
  return {
    ...base,
    name: `one-group-full-flow-trigger-states-${suiteName}`,
    totalStudents: 4,
    groupCount: 1,
    membersPerGroup: 4,
    minReadyStudents: 4,
    discussionDurationMs: Math.max(
      lastMessageAtMs + 30 * 1000,
      scenarios === stateScenarios ? FULL_DIALOGUE_DURATION_SECONDS * 1000 : 0
    ),
    maxWarmupWaitMs: 5 * 60 * 1000,
    rampUp: {
      batchSize: 4,
      intervalMs: 500
    },
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
      actionTickMs: 750,
      verifyAiInputLock: true,
      verifyAiInputLockApiReject: strictCoverage,
      aiInputLockSendRetries: 3,
      aiInputLockPollMs: 750,
      enforceScriptedInterventionExpectations: strictCoverage,
      expectedInterventionMaxWaitMs: 5 * 60 * 1000,
      expectedNoInterventionPollMs: 1000,
      scriptedGroupActions: []
    },
    strategyAudit: {
      enabled: strictCoverage,
      requireCleanContext: strictCoverage,
      expectedEmotionAgentEnabled,
      settleMs: 15 * 1000,
      maxWaitMs: 5 * 60 * 1000,
      pollIntervalMs: 5 * 1000,
      requireActualCoverage: strictCoverage
    },
    stateSuite: {
      enabled: strictCoverage,
      mode: strictCoverage ? suiteName : 'discussion-only',
      groupCode: 'G01',
      isolation: strictCoverage
        ? 'fresh_test_session_or_discussion'
        : 'teacher-controlled-current-session',
      postDialogueWaitMs: 30 * 1000,
      includeRecoveryBridges,
      requireAgentLockRecovery,
      requireModelFailureFallback,
      scenarios
    },
    scriptedDiscussion: {
      description,
      scenarios,
      expectedRuleStates: scenarios.map((stateScenario) => ({
        state: stateScenario.canonicalSubState,
        groupCode: stateScenario.groupCode,
        scenarioId: stateScenario.id
      })),
      expectedStrategyIds,
      mustCoverAllExpectedStrategies: false,
      requireIsolatedDiscussions: false,
      verificationMode: strictCoverage ? 'strict-coverage' : 'discussion-only',
      sendWindowSeconds: sendWindowSeconds || Math.ceil(lastMessageAtMs / 1000),
      repeatForEachGroup: true,
      messages
    }
  };
}

const suiteScenario = buildScenario(stateScenarios, {
  suiteName: 'discussion-only',
  description: 'Four G01 students replay the 16-scene, 144-message campus learning-space dialogue in a 60-minute room, with the base send schedule front-loaded to leave time for real Agent input locks.',
  strictCoverage: false
});

const primaryCoverageScenario = buildScenario(stateScenarios, {
  suiteName: 'primary-substates',
  description: 'Strict server-side coverage audit for the complete 16-scene campus learning-space dialogue.'
});

function scenarioIdentity(stateScenario) {
  return (stateScenario.expectedOverlayTags || [])[0] ||
    stateScenario.expectedProcessState ||
    stateScenario.canonicalSubState;
}

function forStateCase(caseName) {
  const normalized = String(caseName || '').trim();
  const normalizedId = normalized.toUpperCase();
  const normalizedState = normalized.toLowerCase().replace(/-/g, '_');
  const stateScenario = stateScenarios.find(
    (item) => item.id === normalizedId || scenarioIdentity(item) === normalizedState
  );
  if (!stateScenario) {
    throw new Error(
      `Unknown --state-case ${caseName}. Expected S01..S16 or one of: ` +
      `${stateScenarios.map(scenarioIdentity).join(', ')}.`
    );
  }

  const result = buildScenario([stateScenario], {
    suiteName: `case-${stateScenario.id.toLowerCase()}`,
    description: `${stateScenario.id} ${stateScenario.title}; one isolated G01 report scene.`
  });
  result.stateSuite.activeScenarioId = stateScenario.id;
  return result;
}

function forStateRange(caseName) {
  const normalizedId = String(caseName || '').trim().toUpperCase();
  const startIndex = stateScenarios.findIndex((item) => item.id === normalizedId);
  if (startIndex < 0) {
    throw new Error(`Unknown --from-state-case '${caseName}'. Expected S01..S16.`);
  }
  const selected = stateScenarios.slice(startIndex);
  const result = buildScenario(selected, {
    suiteName: `primary-substates-from-${normalizedId.toLowerCase()}`,
    description: `Resume the canonical primary-state suite at ${normalizedId} without replaying earlier successful cases.`,
    includeRecoveryBridges: false
  });
  result.stateSuite.resumeFromScenarioId = normalizedId;
  return result;
}

function forSuite(suiteName) {
  const normalized = String(suiteName || 'primary-substates').trim().toLowerCase();
  if (['primary-substates', 'primary', 'all'].includes(normalized)) return primaryCoverageScenario;
  if (normalized === 'overlays') {
    return buildScenario(overlayScenarios, {
      suiteName: 'overlays',
      description: 'Three overlay cases verify tags separately from their compatible canonical primary sub-state.'
    });
  }
  if (normalized === 'model-failure') {
    return buildScenario(modelFailureScenarios, {
      suiteName: 'model-failure',
      description: 'A test-server fault-injection case verifies explicit unclassified fallback and no intervention.',
      requireModelFailureFallback: true
    });
  }
  if (normalized === 'agent-lock') {
    return buildScenario(agentLockScenarios, {
      suiteName: 'agent-lock',
      description: 'A strategy intervention followed by neutral recovery verifies lock release with the emotion Agent enabled.',
      expectedEmotionAgentEnabled: true,
      requireAgentLockRecovery: true
    });
  }
  if (normalized === 'p0-batch6') {
    const result = buildScenario([stateScenarios[4], stateScenarios[5]], {
      suiteName: 'p0-batch6',
      description: 'P0 batch 6 runs a normal confusion intervention followed by fresh constructive self-regulation/OI restraint across a real five-minute emotion slot.',
      expectedEmotionAgentEnabled: true,
      requireAgentLockRecovery: true
    });
    result.p0Batch6Acceptance = true;
    result.stateSuite.requireEmotionStrategyCollision = true;
    return result;
  }
  if (normalized === 'p0-batch6-direct') {
    const result = buildScenario([stateScenarios[4], stateScenarios[5]], {
      suiteName: 'p0-batch6-direct',
      description: 'Direct P0 batch 6 observation in the current teacher-controlled discussion without the isolated DB audit preflight.',
      expectedEmotionAgentEnabled: true,
      strictCoverage: false
    });
    result.p0Batch6Acceptance = true;
    result.p0Batch6Direct = true;
    result.stateSuite.mode = 'p0-batch6-direct';
    result.stateSuite.isolation = 'current_teacher_controlled_discussion';
    result.stateSuite.requireEmotionStrategyCollision = true;
    result.scriptedDiscussion.verificationMode = 'direct-observation-only';
    return result;
  }
  throw new Error('Unknown --suite. Expected primary-substates, overlays, model-failure, agent-lock, p0-batch6, or p0-batch6-direct.');
}

suiteScenario.forStateCase = forStateCase;
suiteScenario.forStateRange = forStateRange;
suiteScenario.forSuite = forSuite;
suiteScenario.availableSuites = ['primary-substates', 'overlays', 'model-failure', 'agent-lock', 'p0-batch6', 'p0-batch6-direct'];

module.exports = suiteScenario;
