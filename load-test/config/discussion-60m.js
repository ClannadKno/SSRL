const common = require('./common');

module.exports = {
  ...common,
  name: 'discussion-60m',
  discussionDurationMs: 60 * 60 * 1000,
  profiles: {
    active: {
      ratio: 0.1,
      messageIntervalMs: [15 * 1000, 35 * 1000],
      sendChance: 1,
      replyChance: 0.4,
      scrollChance: 0.35
    },
    normal: {
      ratio: 0.6,
      messageIntervalMs: [45 * 1000, 120 * 1000],
      sendChance: 0.7,
      replyChance: 0.22,
      scrollChance: 0.5
    },
    quiet: {
      ratio: 0.3,
      messageIntervalMs: [120 * 1000, 240 * 1000],
      sendChance: 0.4,
      replyChance: 0.06,
      scrollChance: 0.7
    }
  }
};
