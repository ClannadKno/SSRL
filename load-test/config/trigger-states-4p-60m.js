const base = require('./trigger-states-4p');

function scaleMessages(messages, factor) {
  return messages.map((message) => ({
    ...message,
    afterSeconds: message.afterSeconds === undefined ? undefined : message.afterSeconds * factor,
    atSeconds: message.atSeconds === undefined ? undefined : message.atSeconds * factor
  }));
}

module.exports = {
  ...base,
  name: 'trigger-states-4p-60m',
  discussionDurationMs: 60 * 60 * 1000,
  scriptedDiscussion: {
    ...base.scriptedDiscussion,
    description: `${base.scriptedDiscussion.description} Intervals are doubled for a 60-minute discussion window.`,
    messages: scaleMessages(base.scriptedDiscussion.messages, 2)
  }
};
