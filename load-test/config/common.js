const path = require('path');

const rootDir = path.resolve(__dirname, '..');

module.exports = {
  baseUrl: process.env.LOAD_TEST_BASE_URL || 'https://your-server.example.com',
  loginPath: '/login',
  discussionPath: '/student/collab?phase=discussion',
  studentsFile: process.env.LOAD_TEST_STUDENTS_FILE || path.resolve(rootDir, '..', 'data', 'login_keys.csv'),
  reportDir: path.join(rootDir, 'reports'),

  totalStudents: 60,
  groupCount: 15,
  membersPerGroup: 4,
  minReadyStudents: 60,
  maxWarmupWaitMs: 10 * 60 * 1000,

  rampUp: {
    batchSize: 5,
    intervalMs: 10 * 1000
  },

  browser: {
    headless: true,
    slowMo: 0,
    launchArgs: [
      '--disable-dev-shm-usage',
      '--disable-gpu',
      '--no-sandbox',
      // Load-test targets must not inherit a desktop proxy. A shared browser
      // with many polling contexts can exhaust a local proxy's socket pool and
      // surface net::ERR_PROXY_CONNECTION_FAILED even while the target is
      // directly reachable.
      '--no-proxy-server',
      '--disable-background-timer-throttling',
      '--disable-renderer-backgrounding',
      '--disable-backgrounding-occluded-windows'
    ]
  },

  context: {
    viewport: { width: 1280, height: 720 },
    locale: 'zh-CN',
    timezoneId: 'Asia/Shanghai',
    ignoreHTTPSErrors: true
  },

  resourceMode: 'light',
  resourceBlocking: {
    light: {
      blockResourceTypes: ['image', 'media', 'font'],
      blockUrlPatterns: [
        'google-analytics.com',
        'googletagmanager.com',
        'hm.baidu.com',
        'cnzz.com'
      ]
    },
    full: {
      blockResourceTypes: [],
      blockUrlPatterns: []
    }
  },

  selectors: {
    loginKeyInput: 'input[name="login_key"]',
    loginSubmit: 'button[type="submit"], button.btn, form button',
    discussionInput: '#messageInput',
    sendButton: '.send-btn',
    helpButton: '.help-btn',
    aiLockHint: '#aiLockHint',
    chatBox: '#chatBox',
    messageText: '#chatBox .msg-text',
    collaborativeEditorArea: '#collaborativeEditorArea'
  },

  timeouts: {
    navigationMs: 45 * 1000,
    loginMs: 45 * 1000,
    questionnaireMs: 60 * 1000,
    discussionEnterMs: 60 * 1000,
    discussionReadyMs: 10 * 60 * 1000,
    apiResponseMs: 20 * 1000,
    messageVisibleMs: 20 * 1000,
    aiInputLockUiMs: 5 * 1000,
    aiInputLockMaxWaitMs: 90 * 1000,
    aiInputLockObserverGraceMs: 5 * 1000,
    expectedPipelineDiscoveryMaxWaitMs: 10 * 1000,
    messageInputMaxWaitMs: 90 * 1000,
    groupSubmitMs: 60 * 1000,
    groupSubmitWaitMs: 4 * 60 * 1000
  },

  flow: {
    fullFlow: false,
    submitPreQuestionnaires: false,
    submitPostQuestionnaires: false,
    submitPostCheckin: false,
    submitDeliverable: false,
    groupCheckins: [0, 0],
    groupHelps: [0, 0],
    actionTickMs: 5000,
    helpMinGapMs: 60 * 1000,
    verifyAiInputLock: false,
    verifyAiInputLockApiReject: true,
    aiInputLockSendRetries: 2,
    aiInputLockPollMs: 1000,
    expectedPipelinePollMs: 1000,
    messageInputPollMs: 250,
    messageInputMaxPollMs: 3000,
    messageRetryBaseMs: 250,
    messageRetryMaxMs: 5000
  },

  profiles: {
    active: {
      ratio: 0.1,
      messageIntervalMs: [10 * 1000, 25 * 1000],
      sendChance: 1,
      replyChance: 0.45,
      scrollChance: 0.35
    },
    normal: {
      ratio: 0.6,
      messageIntervalMs: [30 * 1000, 90 * 1000],
      sendChance: 0.75,
      replyChance: 0.25,
      scrollChance: 0.5
    },
    quiet: {
      ratio: 0.3,
      messageIntervalMs: [90 * 1000, 180 * 1000],
      sendChance: 0.45,
      replyChance: 0.08,
      scrollChance: 0.7
    }
  },

  stopConditions: {
    maxLoginFailureRate: 0.1,
    maxMessageFailureRate: 0.08,
    maxFatalStudentErrors: 8
  },

  messageTexts: [
    'I think we should compare the evidence before deciding.',
    'My group can start from the first constraint and list options.',
    'This point may need a clearer example from the task material.',
    'I agree with the previous idea, but the risk should be checked.',
    'Can we split the work and then merge the conclusions?',
    'The current plan looks workable if the assumptions are correct.',
    'I want to add one possible counterexample for discussion.',
    'Maybe we should summarize what has been confirmed so far.'
  ]
};
