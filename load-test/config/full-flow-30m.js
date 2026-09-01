const base = require('./discussion-30m');

module.exports = {
  ...base,
  name: 'full-flow-30m',
  timeouts: {
    ...base.timeouts,
    loginMs: 120 * 1000,
    questionnaireMs: 120 * 1000,
    discussionEnterMs: 120 * 1000
  },
  stopConditions: {
    ...base.stopConditions,
    maxLoginFailureRate: 0.35
  },
  flow: {
    ...base.flow,
    fullFlow: true,
    submitPreQuestionnaires: true,
    submitPostQuestionnaires: true,
    submitPostCheckin: true,
    submitDeliverable: true,
    groupCheckins: [2, 3],
    groupHelps: [1, 2],
    actionTickMs: 5000,
    helpMinGapMs: 75 * 1000
  }
};
