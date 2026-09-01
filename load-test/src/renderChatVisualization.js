const fs = require("fs");
const path = require("path");

function readJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, "utf8"));
}

function escapeScriptJson(value) {
  return JSON.stringify(value).replace(/</g, "\\u003c");
}

function resolveSummaryPath(transcriptPath) {
  const candidate = transcriptPath.replace(/-transcript\.json$/i, "-summary.json");
  return fs.existsSync(candidate) ? candidate : null;
}

function resolveOutputPath(transcriptPath, explicitOutputPath) {
  if (explicitOutputPath) {
    return path.resolve(explicitOutputPath);
  }

  return path.resolve(transcriptPath.replace(/-transcript\.json$/i, "-chat-visualization.html"));
}

function htmlDocument(data) {
  const embeddedData = escapeScriptJson(data);

  return `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>${data.runId} 聊天过程可视化</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f5f7fa;
      --surface: #ffffff;
      --surface-2: #f0f4f8;
      --text: #17202a;
      --muted: #5d6b7a;
      --line: #d9e1ea;
      --line-strong: #b8c5d4;
      --agent: #244c7a;
      --student: #215c52;
      --warn: #a25319;
      --danger: #a33a3a;
      --shadow: 0 8px 22px rgba(24, 39, 58, 0.08);
      font-family: Inter, "Segoe UI", "Microsoft YaHei", Arial, sans-serif;
    }

    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      min-width: 320px;
      background: var(--bg);
      color: var(--text);
      letter-spacing: 0;
    }

    .page {
      max-width: 1440px;
      margin: 0 auto;
      padding: 22px;
    }

    header {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 18px;
      align-items: end;
      margin-bottom: 18px;
    }

    h1 {
      margin: 0 0 8px;
      font-size: 25px;
      line-height: 1.2;
      font-weight: 760;
    }

    .meta {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      color: var(--muted);
      font-size: 13px;
    }

    .pill {
      display: inline-flex;
      align-items: center;
      max-width: 100%;
      min-height: 26px;
      padding: 4px 9px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: var(--surface);
      overflow-wrap: anywhere;
    }

    .toolbar {
      display: flex;
      flex-wrap: wrap;
      justify-content: flex-end;
      gap: 8px;
      min-width: 0;
    }

    .toolbar > * {
      max-width: 100%;
    }

    button,
    select,
    input {
      min-height: 34px;
      border: 1px solid var(--line-strong);
      border-radius: 6px;
      background: var(--surface);
      color: var(--text);
      font: inherit;
      font-size: 13px;
    }

    button {
      cursor: pointer;
      padding: 0 10px;
    }

    button.active {
      border-color: #244c7a;
      background: #e5eef8;
      color: #16385e;
      font-weight: 650;
    }

    select,
    input {
      padding: 0 10px;
    }

    input {
      width: min(260px, 100%);
    }

    .grid {
      display: grid;
      grid-template-columns: minmax(0, 1.35fr) minmax(320px, 0.65fr);
      gap: 14px;
      align-items: start;
    }

    .panel {
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
    }

    .panel-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 12px;
      padding: 14px 16px;
      border-bottom: 1px solid var(--line);
    }

    .panel h2 {
      margin: 0;
      font-size: 15px;
      line-height: 1.25;
    }

    .panel-body {
      padding: 16px;
    }

    .metric-grid {
      display: grid;
      grid-template-columns: repeat(5, minmax(118px, 1fr));
      gap: 10px;
      margin-bottom: 14px;
    }

    .metric {
      min-height: 80px;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
    }

    .metric .value {
      font-size: 24px;
      line-height: 1;
      font-weight: 780;
    }

    .metric .label {
      margin-top: 8px;
      color: var(--muted);
      font-size: 12px;
    }

    .timeline {
      position: relative;
      min-height: 214px;
      padding: 8px 0 2px;
    }

    .axis {
      position: relative;
      height: 44px;
      margin: 8px 0 18px;
      border-top: 1px solid var(--line-strong);
    }

    .tick {
      position: absolute;
      top: -5px;
      width: 1px;
      height: 12px;
      background: var(--line-strong);
    }

    .tick span {
      position: absolute;
      top: 14px;
      left: 50%;
      transform: translateX(-50%);
      color: var(--muted);
      font-size: 11px;
      white-space: nowrap;
    }

    .state-runs {
      position: relative;
      height: 74px;
      margin-bottom: 8px;
      border-radius: 7px;
      background: var(--surface-2);
      border: 1px solid var(--line);
      overflow: hidden;
    }

    .run {
      position: absolute;
      top: 0;
      height: 100%;
      border-right: 1px solid rgba(255, 255, 255, 0.75);
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 6px;
      color: #fff;
      font-size: 11px;
      font-weight: 700;
      text-align: center;
      overflow: hidden;
    }

    .run span {
      max-width: 100%;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .agent-lane {
      position: relative;
      height: 50px;
      border-radius: 7px;
      border: 1px dashed var(--line-strong);
      background: #fbfcfe;
    }

    .agent-marker {
      position: absolute;
      top: 8px;
      width: 12px;
      height: 34px;
      transform: translateX(-50%);
      border: 2px solid #244c7a;
      border-radius: 8px;
      background: #e5eef8;
      cursor: pointer;
    }

    .agent-marker.warning {
      border-color: #a25319;
      background: #fff2e3;
    }

    .lane-label {
      position: absolute;
      left: 10px;
      top: 50%;
      transform: translateY(-50%);
      color: var(--muted);
      font-size: 12px;
      pointer-events: none;
    }

    .legend {
      display: flex;
      flex-wrap: wrap;
      gap: 7px;
      margin-top: 13px;
    }

    .legend-item {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      color: var(--muted);
      font-size: 12px;
    }

    .swatch {
      width: 12px;
      height: 12px;
      border-radius: 3px;
    }

    .bars {
      display: grid;
      gap: 9px;
    }

    .bar-row {
      display: grid;
      grid-template-columns: 64px minmax(0, 1fr) 32px;
      gap: 10px;
      align-items: center;
      font-size: 13px;
    }

    .bar-track {
      height: 12px;
      border-radius: 999px;
      background: var(--surface-2);
      overflow: hidden;
      border: 1px solid var(--line);
    }

    .bar-fill {
      height: 100%;
      border-radius: 999px;
    }

    .notes {
      display: grid;
      gap: 8px;
    }

    .note {
      padding: 10px 11px;
      border-left: 4px solid var(--warn);
      background: #fff8ef;
      border-radius: 6px;
      color: #513214;
      font-size: 13px;
      line-height: 1.45;
    }

    .note strong {
      color: #281605;
    }

    .messages {
      display: grid;
      gap: 8px;
    }

    .message {
      display: grid;
      grid-template-columns: 58px 86px minmax(86px, 118px) minmax(116px, 150px) minmax(0, 1fr);
      gap: 10px;
      align-items: start;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
    }

    .message.agent {
      border-color: #b7cbe1;
      background: #f5f9fd;
    }

    .message.warning {
      border-color: #edc89c;
      background: #fff8ef;
    }

    .seq {
      color: var(--muted);
      font-variant-numeric: tabular-nums;
      font-size: 12px;
    }

    .speaker {
      font-weight: 700;
      color: var(--text);
    }

    .time {
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
    }

    .badge {
      display: inline-flex;
      max-width: 100%;
      align-items: center;
      padding: 4px 7px;
      border-radius: 999px;
      color: #fff;
      font-size: 11px;
      line-height: 1.15;
      font-weight: 700;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .content {
      line-height: 1.52;
      word-break: break-word;
    }

    .empty {
      padding: 18px;
      border: 1px dashed var(--line-strong);
      border-radius: 8px;
      color: var(--muted);
      text-align: center;
    }

    @media (max-width: 1060px) {
      .grid {
        grid-template-columns: 1fr;
      }

      .metric-grid {
        grid-template-columns: repeat(2, minmax(120px, 1fr));
      }
    }

    @media (max-width: 720px) {
      .page {
        padding: 14px;
      }

      header {
        grid-template-columns: 1fr;
      }

      .toolbar {
        justify-content: flex-start;
      }

      .message {
        grid-template-columns: 46px minmax(0, 1fr);
      }

      .message .time,
      .message .speaker,
      .message .state-cell,
      .message .content {
        grid-column: 2;
      }

      .metric-grid {
        grid-template-columns: 1fr;
      }
    }
  </style>
</head>
<body>
  <div class="page">
    <header>
      <div>
        <h1>聊天过程可视化</h1>
        <div class="meta" id="runMeta"></div>
      </div>
      <div class="toolbar" aria-label="消息筛选">
        <button type="button" class="role-filter active" data-role="all" title="显示全部消息">全部</button>
        <button type="button" class="role-filter" data-role="student" title="只显示学生消息">学生</button>
        <button type="button" class="role-filter" data-role="agent" title="只显示智能体消息">Agent</button>
        <select id="stateFilter" title="按脚本状态筛选"></select>
        <input id="searchBox" type="search" placeholder="搜索消息内容" />
      </div>
    </header>

    <section class="panel" aria-labelledby="metricsTitle">
      <div class="panel-head">
        <h2 id="metricsTitle">运行概览</h2>
        <span class="pill" id="runWindow"></span>
      </div>
      <div class="panel-body">
        <div class="metric-grid" id="metricGrid"></div>
        <div class="timeline">
          <div class="axis" id="axis"></div>
          <div class="state-runs" id="stateRuns" aria-label="脚本状态时间线"></div>
          <div class="agent-lane" id="agentLane" aria-label="Agent 介入时间线">
            <span class="lane-label">Agent 介入</span>
          </div>
          <div class="legend" id="legend"></div>
        </div>
      </div>
    </section>

    <main class="grid" style="margin-top:14px;">
      <section class="panel" aria-labelledby="messagesTitle">
        <div class="panel-head">
          <h2 id="messagesTitle">消息流</h2>
          <span class="pill" id="visibleCount"></span>
        </div>
        <div class="panel-body">
          <div class="messages" id="messages"></div>
        </div>
      </section>

      <aside style="display:grid;gap:14px;">
        <section class="panel" aria-labelledby="participantsTitle">
          <div class="panel-head">
            <h2 id="participantsTitle">参与度</h2>
            <span class="pill">学生消息</span>
          </div>
          <div class="panel-body">
            <div class="bars" id="speakerBars"></div>
          </div>
        </section>

        <section class="panel" aria-labelledby="statesTitle">
          <div class="panel-head">
            <h2 id="statesTitle">状态分布</h2>
            <span class="pill">脚本消息</span>
          </div>
          <div class="panel-body">
            <div class="bars" id="stateBars"></div>
          </div>
        </section>

        <section class="panel" aria-labelledby="notesTitle">
          <div class="panel-head">
            <h2 id="notesTitle">观察点</h2>
            <span class="pill" id="noteCount"></span>
          </div>
          <div class="panel-body">
            <div class="notes" id="notes"></div>
          </div>
        </section>
      </aside>
    </main>
  </div>

  <script id="reportData" type="application/json">${embeddedData}</script>
  <script>
    const data = JSON.parse(document.getElementById("reportData").textContent);
    const transcript = data.transcript.transcripts[0];
    const messages = transcript.messages;
    const summary = data.summary || {};
    const counters = summary.counters || {};
    const latencies = summary.latencies || {};
    const totalSeq = Math.max(...messages.map((message) => message.sequence));

    const stateColors = {
      unknown: "#6c7684",
      positive_collaboration: "#20745f",
      participation_imbalance: "#b36a12",
      coordination_disorder: "#366ca4",
      conflict_tension: "#b83f3f",
      conflict_repair: "#5f7f2d",
      task_detached: "#7f5d3b",
      blocked_frustration: "#9a4c7b",
      negative_silence: "#4d6172",
      positive_recovery: "#16835d"
    };

    const stateLabels = {
      unknown: "准备进入",
      positive_collaboration: "积极协作",
      participation_imbalance: "参与失衡",
      coordination_disorder: "协调混乱",
      conflict_tension: "冲突升温",
      conflict_repair: "冲突修复",
      task_detached: "任务脱离",
      blocked_frustration: "卡住挫败",
      negative_silence: "负向沉默",
      positive_recovery: "积极恢复"
    };

    const speakerColors = {
      "G01-M1": "#244c7a",
      "G01-M2": "#20745f",
      "G01-M3": "#b36a12",
      "G01-M4": "#8b4f9f",
      "Agent": "#4d6172"
    };

    let activeRole = "all";

    function el(tagName, attrs = {}, children = []) {
      const node = document.createElement(tagName);
      Object.entries(attrs).forEach(([key, value]) => {
        if (key === "class") node.className = value;
        else if (key === "text") node.textContent = value;
        else if (key === "style") Object.assign(node.style, value);
        else node.setAttribute(key, value);
      });
      children.forEach((child) => node.append(child));
      return node;
    }

    function pct(sequence) {
      if (totalSeq <= 1) return 0;
      return ((sequence - 1) / (totalSeq - 1)) * 100;
    }

    function formatTime(value) {
      return value ? value.slice(11, 16) : "";
    }

    function stateText(state) {
      return state ? (stateLabels[state] || state) : "非脚本";
    }

    function isWarningAgent(message, previousStudentState) {
      if (message.role !== "agent") return false;
      const text = message.content.trim();
      if (message.sequence === 36) return true;
      if (previousStudentState === "positive_recovery" && /停一停|深吸|放轻松|重新看看/.test(text)) return true;
      return false;
    }

    function previousStudentStateFor(sequence) {
      for (let index = messages.findIndex((message) => message.sequence === sequence) - 1; index >= 0; index -= 1) {
        if (messages[index].role === "student" && messages[index].scriptedState) {
          return messages[index].scriptedState;
        }
      }
      return null;
    }

    function countBy(items, keyFn) {
      return items.reduce((acc, item) => {
        const key = keyFn(item);
        acc[key] = (acc[key] || 0) + 1;
        return acc;
      }, {});
    }

    function renderMeta() {
      const meta = document.getElementById("runMeta");
      [
        data.runId,
        summary.scenario || data.transcript.scenario,
        summary.baseUrl || data.transcript.baseUrl,
        \`组别 \${transcript.groupCode}\`,
        \`捕获人 \${transcript.capturedBy}\`
      ].filter(Boolean).forEach((item) => meta.append(el("span", { class: "pill", text: item })));

      document.getElementById("runWindow").textContent = \`\${summary.startedAt || data.transcript.generatedAt} 到 \${summary.endedAt || data.transcript.generatedAt}\`;
    }

    function renderMetrics() {
      const metricGrid = document.getElementById("metricGrid");
      const metrics = [
        { value: \`\${counters.loginSuccess || 0}/\${counters.loginAttempted || 0}\`, label: "登录成功" },
        { value: \`\${counters.messageSuccess || 0}/\${counters.messageAttempted || 0}\`, label: "脚本消息成功" },
        { value: transcript.messageCount, label: "捕获消息总数" },
        { value: transcript.agentMessageCount, label: "Agent 消息" },
        { value: \`\${latencies.messageMs?.p95 || "-"}ms\`, label: "消息 p95 延迟" },
        { value: counters.pageErrors || 0, label: "页面错误" },
        { value: counters.consoleErrors || 0, label: "控制台错误" },
        { value: counters.requestFailures || 0, label: "请求失败" },
        { value: \`\${counters.helpAccepted || 0}/\${counters.helpAttempted || 0}\`, label: "求助接受" },
        { value: \`\${counters.deliverableSubmitted || 0}/\${counters.deliverableAttempted || 0}\`, label: "产物提交" }
      ];

      metrics.forEach((metric) => {
        metricGrid.append(el("div", { class: "metric" }, [
          el("div", { class: "value", text: metric.value }),
          el("div", { class: "label", text: metric.label })
        ]));
      });
    }

    function stateRuns() {
      const scripted = messages.filter((message) => message.role === "student" && message.scriptedState);
      const runs = [];
      scripted.forEach((message) => {
        const last = runs[runs.length - 1];
        if (last && last.state === message.scriptedState) {
          last.end = message.sequence;
          last.count += 1;
        } else {
          runs.push({ state: message.scriptedState, start: message.sequence, end: message.sequence, count: 1 });
        }
      });
      return runs;
    }

    function renderTimeline() {
      const axis = document.getElementById("axis");
      [1, 10, 20, 30, 40, 52].forEach((sequence) => {
        axis.append(el("div", { class: "tick", style: { left: \`\${pct(sequence)}%\` } }, [
          el("span", { text: \`#\${sequence}\` })
        ]));
      });

      const stateRunBox = document.getElementById("stateRuns");
      stateRuns().forEach((run) => {
        const left = pct(run.start);
        const right = pct(run.end + 1);
        const width = Math.max(2.2, right - left);
        stateRunBox.append(el("div", {
          class: "run",
          title: \`\${stateText(run.state)}：#\${run.start}-#\${run.end}，\${run.count} 条\`,
          style: {
            left: \`\${left}%\`,
            width: \`\${width}%\`,
            backgroundColor: stateColors[run.state] || "#6c7684"
          }
        }, [el("span", { text: stateText(run.state) })]));
      });

      const agentLane = document.getElementById("agentLane");
      messages.filter((message) => message.role === "agent").forEach((message) => {
        const previousState = previousStudentStateFor(message.sequence);
        const marker = el("button", {
          type: "button",
          class: \`agent-marker\${isWarningAgent(message, previousState) ? " warning" : ""}\`,
          title: \`#\${message.sequence} \${message.content}\`,
          style: { left: \`\${pct(message.sequence)}%\` }
        });
        marker.addEventListener("click", () => {
          const row = document.querySelector(\`[data-sequence="\${message.sequence}"]\`);
          row?.scrollIntoView({ behavior: "smooth", block: "center" });
          row?.animate([{ outlineColor: "#244c7a" }, { outlineColor: "transparent" }], { duration: 1200 });
        });
        agentLane.append(marker);
      });

      const legend = document.getElementById("legend");
      Object.keys(stateLabels).forEach((state) => {
        legend.append(el("span", { class: "legend-item" }, [
          el("span", { class: "swatch", style: { backgroundColor: stateColors[state] } }),
          document.createTextNode(stateLabels[state])
        ]));
      });
    }

    function renderBars(targetId, counts, colorMap) {
      const box = document.getElementById(targetId);
      const max = Math.max(...Object.values(counts), 1);
      Object.entries(counts).forEach(([name, count]) => {
        box.append(el("div", { class: "bar-row" }, [
          el("div", { text: stateLabels[name] || name }),
          el("div", { class: "bar-track" }, [
            el("div", {
              class: "bar-fill",
              style: {
                width: \`\${(count / max) * 100}%\`,
                backgroundColor: colorMap[name] || "#6c7684"
              }
            })
          ]),
          el("div", { class: "seq", text: String(count) })
        ]));
      });
    }

    function renderSidePanels() {
      const studentMessages = messages.filter((message) => message.role === "student");
      renderBars("speakerBars", countBy(studentMessages, (message) => message.displayName), speakerColors);

      const scriptedMessages = messages.filter((message) => message.scriptedState);
      const stateCounts = countBy(scriptedMessages, (message) => message.scriptedState);
      renderBars("stateBars", stateCounts, stateColors);

      const notes = [
        { seq: 36, text: "Agent 第 36 条疑似截断，回复停在“方案一和方案”，需要排查生成、流式保存或渲染链路。" },
        { seq: 12, text: "参与失衡之后的 Agent 回复偏安抚，没有直接邀请低发言成员进入讨论，策略贴合度偏弱。" },
        { seq: 47, text: "积极恢复阶段已经重新分工后，Agent 又触发“停一停、深吸一口气”，时机略反向。" }
      ];

      document.getElementById("noteCount").textContent = \`\${notes.length} 个观察点\`;
      const notesBox = document.getElementById("notes");
      notes.forEach((note) => {
        const node = el("button", { type: "button", class: "note", title: \`跳到 #\${note.seq}\` }, [
          el("strong", { text: "#" + note.seq + " " }),
          document.createTextNode(note.text)
        ]);
        node.addEventListener("click", () => {
          const row = document.querySelector(\`[data-sequence="\${note.seq}"]\`);
          row?.scrollIntoView({ behavior: "smooth", block: "center" });
        });
        notesBox.append(node);
      });
    }

    function renderStateFilter() {
      const select = document.getElementById("stateFilter");
      select.append(el("option", { value: "all", text: "全部状态" }));
      transcript.scriptedStates.forEach((state) => {
        select.append(el("option", { value: state, text: stateText(state) }));
      });
      select.append(el("option", { value: "agent", text: "Agent 消息" }));
    }

    function messageMatchesFilters(message) {
      const stateFilter = document.getElementById("stateFilter").value;
      const search = document.getElementById("searchBox").value.trim().toLowerCase();

      if (activeRole !== "all" && message.role !== activeRole) return false;
      if (stateFilter === "agent" && message.role !== "agent") return false;
      if (stateFilter !== "all" && stateFilter !== "agent" && message.scriptedState !== stateFilter) return false;
      if (search && !message.content.toLowerCase().includes(search)) return false;
      return true;
    }

    function renderMessages() {
      const box = document.getElementById("messages");
      box.innerHTML = "";

      const visible = messages.filter(messageMatchesFilters);
      document.getElementById("visibleCount").textContent = \`\${visible.length}/\${messages.length} 条\`;
      if (!visible.length) {
        box.append(el("div", { class: "empty", text: "没有匹配的消息。" }));
        return;
      }

      visible.forEach((message) => {
        const previousState = previousStudentStateFor(message.sequence);
        const warning = isWarningAgent(message, previousState);
        const speaker = message.role === "agent" ? "Agent" : message.displayName;
        const state = message.role === "agent" ? "Agent 介入" : stateText(message.scriptedState);
        const color = message.role === "agent" ? "#244c7a" : (stateColors[message.scriptedState] || "#6c7684");
        const row = el("article", {
          class: \`message \${message.role}\${warning ? " warning" : ""}\`,
          "data-sequence": String(message.sequence)
        }, [
          el("div", { class: "seq", text: \`#\${message.sequence}\` }),
          el("div", { class: "time", text: formatTime(message.createdAt) }),
          el("div", { class: "speaker", text: speaker }),
          el("div", { class: "state-cell" }, [
            el("span", { class: "badge", text: state, style: { backgroundColor: color } })
          ]),
          el("div", { class: "content", text: message.content })
        ]);
        box.append(row);
      });
    }

    function bindControls() {
      document.querySelectorAll(".role-filter").forEach((button) => {
        button.addEventListener("click", () => {
          activeRole = button.dataset.role;
          document.querySelectorAll(".role-filter").forEach((item) => item.classList.toggle("active", item === button));
          renderMessages();
        });
      });

      document.getElementById("stateFilter").addEventListener("change", renderMessages);
      document.getElementById("searchBox").addEventListener("input", renderMessages);
    }

    renderMeta();
    renderMetrics();
    renderTimeline();
    renderSidePanels();
    renderStateFilter();
    bindControls();
    renderMessages();
  </script>
</body>
</html>`;
}

function main() {
  const [, , transcriptArg, outputArg] = process.argv;
  if (!transcriptArg) {
    console.error("Usage: node src/renderChatVisualization.js <transcript.json> [output.html]");
    process.exit(1);
  }

  const transcriptPath = path.resolve(transcriptArg);
  const summaryPath = resolveSummaryPath(transcriptPath);
  const outputPath = resolveOutputPath(transcriptPath, outputArg);
  const transcript = readJson(transcriptPath);
  const summary = summaryPath ? readJson(summaryPath) : null;
  const data = {
    runId: transcript.runId,
    transcript,
    summary
  };

  fs.writeFileSync(outputPath, htmlDocument(data), "utf8");
  console.log(outputPath);
}

main();
