const base = require('./trigger-states-4p');

module.exports = {
  ...base,
  name: 'trigger-states-all-groups',
  totalStudents: 60,
  groupCount: 15,
  membersPerGroup: 4,
  minReadyStudents: 60,
  rampUp: {
    batchSize: 5,
    intervalMs: 10 * 1000
  },
  scriptedDiscussion: {
    ...base.scriptedDiscussion,
    description: `${base.scriptedDiscussion.description} The same four-member script is repeated for every load-test group.`,
    repeatForEachGroup: true
  }
};
