const base = require('./full-flow-30m');

const FINAL_STATE_CODES = [
  'positive_collaboration',
  'negative_silence',
  'conflict_tension',
  'blocked_frustration',
  'task_detached',
  'unknown'
];

module.exports = {
  ...base,
  name: 'trigger-states-4p',
  totalStudents: 4,
  groupCount: 1,
  membersPerGroup: 4,
  minReadyStudents: 4,
  discussionDurationMs: 30 * 60 * 1000,
  rampUp: {
    batchSize: 4,
    intervalMs: 1000
  },
  flow: {
    ...base.flow,
    groupCheckins: [0, 0],
    groupHelps: [0, 0],
    actionTickMs: 1000
  },
  profiles: {
    scripted: {
      ratio: 1,
      messageIntervalMs: [30 * 1000, 60 * 1000],
      sendChance: 1,
      replyChance: 0,
      scrollChance: 0.15
    }
  },
  scriptedDiscussion: {
    description: 'Four-person G01 first-week campus shared learning-space script. It separates task detachment and real silence from later blocked/frustration and conflict windows.',
    expectedFinalStates: FINAL_STATE_CODES,
    mustCoverAllFinalStates: true,
    messages: [
      // Positive collaboration: first-week task framing, evidence roles, and comparison criteria.
      {
        afterSeconds: 15,
        studentId: 'S1-G01-M1',
        state: 'positive_collaboration',
        text: '大家先把第一周任务拆一下：我们要给校园共享学习空间做优化方案，我先记录问题和证据。'
      },
      {
        afterSeconds: 15,
        studentId: 'S1-G01-M2',
        state: 'positive_collaboration',
        text: '我负责整理图书馆自习区的拥挤、插座和预约数据，先看证据再提方案。'
      },
      {
        afterSeconds: 15,
        studentId: 'S1-G01-M3',
        state: 'positive_collaboration',
        text: '我补充学生访谈角度，比如小组讨论区不够和安静区被打扰的问题。'
      },
      {
        afterSeconds: 15,
        studentId: 'S1-G01-M4',
        state: 'positive_collaboration',
        text: '我来做方案比较表：空间布局、预约规则、成本和风险，最后一起选优先方案。'
      },

      // Observation / unknown: one student dominates, but not yet a negative final state.
      {
        afterSeconds: 45,
        studentId: 'S1-G01-M1',
        state: 'unknown',
        text: '我觉得直接做预约制就行，先把所有座位都放进系统，写起来也比较清楚。'
      },
      {
        afterSeconds: 15,
        studentId: 'S1-G01-M1',
        state: 'unknown',
        text: '我再补一句，预约制还能统计使用率，所以其他方案可以先不用展开太多。'
      },
      {
        afterSeconds: 15,
        studentId: 'S1-G01-M1',
        state: 'unknown',
        text: '如果没人补充，我就按这个方向列提纲：问题、证据、方案、预期效果。'
      },
      {
        afterSeconds: 15,
        studentId: 'S1-G01-M1',
        state: 'unknown',
        text: '我先把结论也暂定为预约优先，后面大家补几条材料就可以。'
      },

      // Task detachment: separated from conflict so the off-task/passive signal can be detected cleanly.
      {
        afterSeconds: 75,
        studentId: 'S1-G01-M4',
        state: 'task_detached',
        text: '先停一下，我有点无聊了，感觉这个共享空间方案怎么写都差不多。'
      },
      {
        afterSeconds: 20,
        studentId: 'S1-G01-M3',
        state: 'task_detached',
        text: '我也不太想继续讨论，刚才看到食堂新窗口了，等会儿吃什么？'
      },
      {
        afterSeconds: 20,
        studentId: 'S1-G01-M4',
        state: 'task_detached',
        text: '要不先聊会儿游戏放松一下，反正最后能交上去就行。'
      },

      // Real negative silence: no student chat for more than the silence threshold before these low-volume restarts.
      {
        afterSeconds: 250,
        studentId: 'S1-G01-M2',
        state: 'negative_silence',
        text: '刚才沉默太久了，我现在只确定一个问题：预约制会不会让临时自习的人更不方便？'
      },
      {
        afterSeconds: 60,
        studentId: 'S1-G01-M3',
        state: 'negative_silence',
        text: '我还没想好访谈材料怎么用，只能先说小组讨论区确实经常被占满。'
      },
      {
        afterSeconds: 60,
        studentId: 'S1-G01-M1',
        state: 'negative_silence',
        text: '我也有点不确定，我们是不是需要先把比较标准列出来？'
      },

      // Blocked frustration: unclear roles and evidence-to-conclusion blockage.
      {
        afterSeconds: 80,
        studentId: 'S1-G01-M2',
        state: 'blocked_frustration',
        text: '我还是卡住，不知道怎么把观察到的问题和最终优化方案连起来。'
      },
      {
        afterSeconds: 20,
        studentId: 'S1-G01-M3',
        state: 'blocked_frustration',
        text: '谁负责证据，谁负责成本？现在没有下一步，我有点不知道该写哪部分。'
      },
      {
        afterSeconds: 20,
        studentId: 'S1-G01-M4',
        state: 'blocked_frustration',
        text: '我们好像越整理越乱，空间布局、预约规则和费用都混在一起，结论写不出来。'
      },

      // Conflict tension: destructive disagreement appears after the blocked window, not before task detachment.
      {
        afterSeconds: 100,
        studentId: 'S1-G01-M2',
        state: 'conflict_tension',
        text: '不对，你这个全部改成预约制根本没考虑临时自习的人，方案不合理。'
      },
      {
        afterSeconds: 15,
        studentId: 'S1-G01-M3',
        state: 'conflict_tension',
        text: '你也别一直否定，我觉得开放讨论区才是重点，你的方案太片面。'
      },
      {
        afterSeconds: 15,
        studentId: 'S1-G01-M2',
        state: 'conflict_tension',
        text: '别乱说，你没有看清任务要求，成本也完全没算，强行推讨论区肯定不行。'
      },

      // Recovery and sustained collaboration: repair the conflict and finish with evidence-based roles.
      {
        afterSeconds: 60,
        studentId: 'S1-G01-M1',
        state: 'positive_collaboration',
        text: '先别争，我们把预约制和开放讨论区两个方案按证据、成本、可执行性分别打分。'
      },
      {
        afterSeconds: 25,
        studentId: 'S1-G01-M4',
        state: 'positive_collaboration',
        text: '可以，我来记录评分，M2补预约数据，M3补讨论区访谈，我整理风险。'
      },
      {
        afterSeconds: 25,
        studentId: 'S1-G01-M2',
        state: 'positive_collaboration',
        text: '我负责预约方案的证据：高峰时段座位紧张、插座不足，以及预约能缓解排队。'
      },
      {
        afterSeconds: 25,
        studentId: 'S1-G01-M3',
        state: 'positive_collaboration',
        text: '我负责讨论区方案的证据：访谈里多人提到小组讨论没地方，而且容易影响安静区。'
      },
      {
        afterSeconds: 45,
        studentId: 'S1-G01-M4',
        state: 'positive_collaboration',
        text: '阶段总结：我们保留两个方案，主方案是分区优化，预约制作为高峰时段辅助规则。'
      },
      {
        afterSeconds: 120,
        studentId: 'S1-G01-M2',
        state: 'positive_collaboration',
        text: '我补一条成本风险：预约系统需要维护，但可以先用现有平台做试点，降低投入。'
      },
      {
        afterSeconds: 120,
        studentId: 'S1-G01-M3',
        state: 'positive_collaboration',
        text: '访谈证据可以写成两类需求：安静自习和小组讨论，所以分区优化更能解释材料。'
      },
      {
        afterSeconds: 120,
        studentId: 'S1-G01-M4',
        state: 'positive_collaboration',
        text: '我把比较表整理成三列：问题证据、优化动作、可能风险，最后给出优先级。'
      },
      {
        afterSeconds: 120,
        studentId: 'S1-G01-M1',
        state: 'positive_collaboration',
        text: '最终结论可以写：先调整分区和预约规则，再根据试点数据决定是否扩大实施。'
      }
    ]
  }
};
