# -*- coding: utf-8 -*-
"""Base page shell and shared CSS for SSRL-ESP."""

BASE_CSS = """
:root {
  --primary: var(--ui-brand-700, #1d548d);
  --primary-hover: var(--ui-brand-800, #153b70);
  --primary-soft: var(--ui-brand-soft, rgba(70,118,190,.12));
  --primary-border: var(--ui-border-strong, rgba(93,126,177,.40));
  --danger-soft: var(--ui-danger-surface, #fff0ef);
  --danger-border: var(--ui-danger-border, rgba(160,72,72,.28));
  --danger-text: var(--ui-danger-text, #984848);
  --warning-soft: var(--ui-warning-surface, #fff6e8);
  --warning-border: var(--ui-warning-border, rgba(155,105,37,.28));
  --warning-text: var(--ui-warning-text, #8b5f21);
  --success-soft: var(--ui-success-surface, #edf7f0);
  --success-border: var(--ui-success-border, rgba(56,118,78,.28));
  --success-text: var(--ui-success-text, #376f4b);
  --bg: var(--ui-bg-base, #eef3ff);
  --surface: var(--ui-surface-solid, #fff);
  --surface-alpha: var(--ui-glass-workspace, rgba(255,255,255,.70));
  --surface-soft: var(--ui-glass-control, rgba(255,255,255,.64));
  --surface-card: var(--ui-glass-card, rgba(255,255,255,.82));
  --border: var(--ui-border-soft, rgba(145,169,207,.24));
  --border-light: rgba(145,169,207,.16);
  --text: var(--ui-text-primary, #314764);
  --text-secondary: var(--ui-text-secondary, #5e6f87);
  --text-muted: var(--ui-text-muted, #62738c);
  --shadow-sm: var(--ui-shadow-control, 0 7px 22px rgba(54,83,137,.06));
  --shadow: var(--ui-shadow-card, 0 16px 42px rgba(54,83,137,.08));
  --shadow-md: var(--ui-shadow-hover, 0 20px 50px rgba(54,83,137,.12));
  --shadow-lg: var(--ui-shadow-workspace, 0 30px 80px rgba(54,83,137,.12));
  --radius-sm: var(--ui-radius-small, 12px);
  --radius: var(--ui-radius-control, 18px);
  --radius-lg: var(--ui-radius-panel, 22px);
  --radius-xl: var(--ui-radius-card, 28px);
  --radius-full: var(--ui-radius-pill, 999px);
  --space-1: 4px;
  --space-2: 8px;
  --space-3: 12px;
  --space-4: 16px;
  --space-5: 20px;
  --space-6: 24px;
  --space-8: 32px;
  --space-10: 40px;
  --font: var(--ui-font-sans, "Microsoft YaHei", "PingFang SC", -apple-system, sans-serif);
  --font-mono: var(--ui-font-mono, "Cascadia Code", "Fira Code", "Consolas", monospace);
  --text-xs: 11px;
  --text-sm: 13px;
  --text-base: 14px;
  --text-lg: 16px;
  --text-xl: 18px;
  --text-2xl: 22px;
  --text-3xl: 28px;
  --leading: 1.6;
  --leading-tight: 1.3;
}

*, *::before, *::after { box-sizing: border-box; }
html, body { margin: 0; padding: 0; min-height: 100%; }
body {
  font-family: var(--font); font-size: var(--text-base); line-height: var(--leading);
  color: var(--text);
  background:
    var(--ui-page-glow-cool, radial-gradient(900px 560px at 12% 8%, rgba(115,157,242,.22), rgba(115,157,242,0) 72%)),
    var(--ui-page-glow-warm, radial-gradient(780px 500px at 88% 18%, rgba(255,221,180,.22), rgba(255,221,180,0) 74%)),
    var(--ui-page-gradient-veil, linear-gradient(to bottom, rgba(255,255,255,.02) 0%, rgba(255,255,255,.30) 220px, rgba(255,255,255,.72) 620px, rgba(255,255,255,.88) 100%)),
    var(--ui-page-gradient-base, linear-gradient(115deg, #dceaff 0%, #edf3ff 44%, #f7f9ff 68%, #fff1e4 100%));
  background-repeat: no-repeat, no-repeat, no-repeat, no-repeat;
  background-position: center top, center top, center top, center top;
  background-size: cover, cover, cover, cover;
  -webkit-font-smoothing: antialiased; -moz-osx-font-smoothing: grayscale;
}
a { color: var(--primary); transition: color 0.15s; }
a:hover { color: var(--primary-hover); }
img { max-width: 100%; }
.app-shell, .workspace { min-height: 100vh; }

.topbar, .nav, .navbar {
  position: sticky; top: 0; z-index: 100;
  background: var(--surface-alpha);
  backdrop-filter: blur(22px);
  -webkit-backdrop-filter: blur(22px);
  border-bottom: 1px solid var(--border);
}
.container, .page-wrap, .teacher-page, .student-page, .nav {
  width: min(1200px, calc(100vw - 32px));
  margin: 0 auto;
}
.container, .page-wrap, .teacher-page, .student-page { padding: var(--space-6) 0 var(--space-10); }
.nav {
  min-height: 64px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-4);
}
.nav-title {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  font-size: var(--text-xl);
  font-weight: 800;
  letter-spacing: -0.3px;
}
.nav-title:before {
  content: "";
  display: inline-grid;
  place-items: center;
  width: 34px;
  height: 34px;
  border-radius: 50%;
  color: #fff;
  background: var(--primary);
  box-shadow: 0 8px 20px rgba(111, 143, 167, 0.24);
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64' fill='none' stroke='white' stroke-width='4' stroke-linecap='round' stroke-linejoin='round'%3E%3Ccircle cx='20' cy='20' r='5' fill='white' stroke='white'/%3E%3Ccircle cx='44' cy='20' r='5' fill='white' stroke='white'/%3E%3Ccircle cx='32' cy='42' r='5' fill='white' stroke='white'/%3E%3Cpath d='M25 20h14M23 25l6 11M41 25l-6 11'/%3E%3C/svg%3E");
  background-repeat: no-repeat;
  background-position: center;
  background-size: 60%;
}
.nav-user { display: flex; align-items: center; gap: var(--space-2); color: var(--text-muted); font-size: var(--text-sm); }
.nav-user .btn.small { height: 28px; padding: 0 12px; font-size: var(--text-xs); }

h1, h2, h3, h4 { margin: 0; line-height: var(--leading-tight); font-weight: 700; }
h1 { font-size: var(--text-3xl); }
h2 { font-size: var(--text-2xl); }
h3 { font-size: var(--text-xl); }
.muted, .label, .text-muted { color: var(--text-muted); font-size: var(--text-sm); }
.text-secondary { color: var(--text-secondary); }
.text-sm { font-size: var(--text-sm); }
.text-xs { font-size: var(--text-xs); }
.text-center { text-align: center; }
.mono { font-family: var(--font-mono); }

.btn {
  display: inline-flex; align-items: center; justify-content: center;
  gap: var(--space-2); height: 40px; padding: 0 var(--space-5);
  border: none; border-radius: var(--radius-full);
  font-size: var(--text-sm); font-weight: 700; cursor: pointer;
  transition: all 0.15s ease; text-decoration: none;
  background: var(--primary); color: #fff;
  box-shadow: 0 8px 18px rgba(111, 143, 167, 0.20);
  white-space: nowrap; line-height: 1;
}
.btn:hover { background: var(--primary-hover); box-shadow: 0 10px 24px rgba(111, 143, 167, 0.24); transform: translateY(-1px); text-decoration: none; }
.btn:active { transform: translateY(0); }
.btn.secondary { background: var(--surface-soft); color: var(--text-secondary); box-shadow: inset 0 0 0 1.5px var(--border); }
.btn.secondary:hover { background: var(--surface); color: var(--text); transform: none; }
.btn.danger { background: var(--danger-text); box-shadow: 0 8px 18px rgba(184, 94, 94, 0.20); }
.btn.small { height: 32px; padding: 0 var(--space-4); font-size: var(--text-xs); }
.btn.large { height: 48px; padding: 0 var(--space-6); font-size: var(--text-base); }
.btn:disabled { opacity: 0.5; cursor: not-allowed; transform: none; }

.badge, .pill, .tag {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 3px 10px; border-radius: var(--radius-full);
  font-size: var(--text-xs); font-weight: 700; line-height: 1.5;
  background: var(--primary-soft); color: var(--primary);
  border: 1px solid var(--primary-border); white-space: nowrap;
}
.badge.high { background: var(--danger-soft); color: var(--danger-text); border-color: var(--danger-border); }
.badge.mid { background: var(--warning-soft); color: var(--warning-text); border-color: var(--warning-border); }
.badge.low { background: var(--success-soft); color: var(--success-text); border-color: var(--success-border); }

.card, .panel, .group-card, .hero-card, .export-card, .dock-card,
.task-hero, .dialogue-panel, .teacher-hero, .analysis-card {
  background: var(--surface-card); border: 1px solid var(--border);
  border-radius: var(--radius-lg); box-shadow: var(--shadow-sm);
  backdrop-filter: blur(22px);
  -webkit-backdrop-filter: blur(22px);
}
.card { overflow: hidden; }
.card-hd {
  display: flex; align-items: center; justify-content: space-between;
  gap: var(--space-3); padding: var(--space-4) var(--space-5);
  border-bottom: 1px solid var(--border-light); background: rgba(255,255,255,.28);
}
.card-hd h2 { font-size: var(--text-lg); font-weight: 700; }
.card-bd { padding: var(--space-5); }
.group-card { padding: var(--space-5); display: grid; gap: var(--space-4); }
.group-card-head {
  display: flex; align-items: flex-start; justify-content: space-between;
  gap: var(--space-4); flex-wrap: wrap;
}
.group-title { font-size: var(--text-lg); font-weight: 800; }

.kpi, .metric {
  padding: var(--space-4) var(--space-5); border-radius: var(--radius);
  background: rgba(255,255,255,.42); border: 1px solid var(--border-light);
}
.kpi span, .metric span { display: block; color: var(--text-muted); font-size: var(--text-xs); }
.kpi strong, .metric strong { display: block; margin-top: var(--space-1); font-size: 26px; font-weight: 800; letter-spacing: -0.5px; }

.kpi-grid { display: grid; gap: var(--space-4); margin: var(--space-6) 0; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); }
.group-board { display: grid; gap: var(--space-5); grid-template-columns: repeat(auto-fit, minmax(380px, 1fr)); }
.metric-row { display: grid; gap: var(--space-3); grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); }
.teacher-actions, .export-links { display: flex; flex-wrap: wrap; gap: var(--space-2); }

.evidence, .context-box {
  padding: var(--space-3) var(--space-4); border-radius: var(--radius);
  background: var(--primary-soft); border: 1px solid var(--primary-border);
  color: var(--text-secondary); line-height: var(--leading);
  font-size: var(--text-sm); white-space: pre-wrap;
}

.chat-shell, #chatBox { border-radius: var(--radius); background: var(--surface); border: 1px solid var(--border); }
#chatBox { max-height: 520px; overflow-y: auto; padding: var(--space-4); }
.msg { display: flex; margin-bottom: var(--space-3); }
.msg.me { justify-content: flex-end; }
.msg .msg-content { max-width: min(75%, 560px); }
.msg-meta { margin-bottom: 4px; color: var(--text-muted); font-size: var(--text-xs); }
.msg-bubble {
  padding: var(--space-3) var(--space-4); border-radius: var(--radius);
  background: var(--surface); border: 1px solid var(--border);
  box-shadow: var(--shadow-sm); line-height: var(--leading);
  font-size: var(--text-sm); white-space: pre-wrap;
}
.msg.me .msg-bubble { background: var(--primary-soft); border-color: var(--primary-border); }
.msg.agent .msg-bubble { background: var(--primary-soft); border-color: var(--primary-border); }

textarea, input[type="text"], input[type="password"], input[type="number"], input[type="email"], select {
  width: 100%; height: 42px; border-radius: var(--radius);
  border: 1.5px solid var(--border); background: var(--surface-soft);
  padding: 0 var(--space-4); color: var(--text);
  font-size: var(--text-sm); transition: border-color 0.15s, box-shadow 0.15s;
  outline: none;
}
textarea { height: auto; min-height: 100px; padding: var(--space-3) var(--space-4); resize: vertical; }
textarea:focus, input:focus, select:focus { border-color: var(--primary); background: rgba(255,255,255,.72); box-shadow: 0 0 0 3px rgba(111, 143, 167, 0.12); }
::placeholder { color: var(--text-muted); }

.flex { display: flex; }
.flex-col { flex-direction: column; }
.flex-wrap { flex-wrap: wrap; }
.items-center { align-items: center; }
.items-start { align-items: flex-start; }
.justify-between { justify-content: space-between; }
.justify-center { justify-content: center; }
.gap-1 { gap: var(--space-1); }
.gap-2 { gap: var(--space-2); }
.gap-3 { gap: var(--space-3); }
.gap-4 { gap: var(--space-4); }
.gap-6 { gap: var(--space-6); }
.grid { display: grid; }
.grid-2 { display: grid; gap: var(--space-4); grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); }
.grid-3 { display: grid; gap: var(--space-4); grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); }
.mt-1 { margin-top: var(--space-1); } .mt-2 { margin-top: var(--space-2); }
.mt-3 { margin-top: var(--space-3); } .mt-4 { margin-top: var(--space-4); }
.mb-2 { margin-bottom: var(--space-2); } .mb-3 { margin-bottom: var(--space-3); }
.mb-4 { margin-bottom: var(--space-4); }
.p-4 { padding: var(--space-4); } .p-5 { padding: var(--space-5); }
.w-full { width: 100%; }
.truncate { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.teacher-hero { padding: var(--space-6); display: grid; gap: var(--space-5); }
.hero-card, .export-card { padding: var(--space-5); }
.export-card h2 { font-size: var(--text-lg); margin: 0 0 var(--space-2); }
.export-card p { line-height: var(--leading); margin: 0; display: none; }
#panelTaskAdmin, #panelQuestionnaireAdmin { margin-top: var(--space-6); }

.learning-layout {
  display: grid;
  grid-template-columns: 1fr 1.4fr;
  gap: var(--space-5);
  max-width: min(1400px, calc(100vw - 32px));
  margin: 0 auto;
  padding: var(--space-6) 0 var(--space-10);
}
.material-panel {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
.panel-top {
  display: flex; align-items: center; justify-content: space-between;
  padding: var(--space-4) var(--space-5);
  border-bottom: 1px solid var(--border);
}
.panel-top .crumbs { display: flex; align-items: center; gap: var(--space-2); font-size: var(--text-xs); color: var(--text-muted); }
.panel-top .crumbs .dot { width: 3px; height: 3px; border-radius: 50%; background: var(--border); }
.material-body { padding: var(--space-5); display: grid; gap: var(--space-4); }
.slide-card { display: grid; gap: var(--space-3); }
.slide-kicker { font-size: var(--text-xs); font-weight: 700; color: var(--primary); text-transform: uppercase; letter-spacing: 1px; }
.slide-title { font-size: var(--text-2xl); font-weight: 800; line-height: var(--leading-tight); }
.slide-desc { color: var(--text-secondary); line-height: var(--leading); font-size: var(--text-sm); }
.submission-preview { padding: var(--space-3) var(--space-4); border-radius: var(--radius); background: var(--border-light); border: 1px solid var(--border); font-size: var(--text-sm); line-height: var(--leading); }
.submission-file { display: inline-flex; align-items: center; gap: var(--space-1); padding: var(--space-1) var(--space-3); border-radius: var(--radius-sm); background: var(--primary-soft); color: var(--primary); font-size: var(--text-xs); text-decoration: none; }
.submission-file:hover { background: var(--primary-border); text-decoration: none; }

.dialogue-panel {
  display: flex; flex-direction: column;
  background: var(--surface-card); border: 1px solid var(--border);
  border-radius: var(--radius-lg); overflow: hidden;
  backdrop-filter: blur(22px);
  -webkit-backdrop-filter: blur(22px);
}
.dialogue-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: var(--space-4) var(--space-5);
  border-bottom: 1px solid var(--border-light); background: rgba(255,255,255,.28);
}
.dialogue-body { flex: 1; padding: var(--space-4); overflow-y: auto; max-height: 540px; }
.dialogue-footer { padding: var(--space-4); border-top: 1px solid var(--border); display: flex; gap: var(--space-2); }
.dialogue-footer textarea { flex: 1; border-radius: var(--radius); min-height: 42px; max-height: 120px; resize: none; }

.sera-panel { margin-top: var(--space-4); padding: var(--space-4); border-radius: var(--radius); background: var(--primary-soft); border: 1px solid var(--primary-border); }
.sera-panel .sera-title { font-size: var(--text-sm); font-weight: 700; color: var(--primary); margin-bottom: var(--space-2); }
.sera-panel .sera-msg { font-size: var(--text-sm); line-height: var(--leading); color: var(--text-secondary); }

.feedback-row { display: flex; gap: var(--space-2); margin-top: var(--space-3); }
.feedback-btn { display: inline-flex; align-items: center; gap: 4px; padding: 4px 12px; border-radius: var(--radius-full); border: 1px solid var(--border); background: var(--surface); font-size: var(--text-xs); cursor: pointer; transition: all 0.12s; color: var(--text-secondary); }
.feedback-btn:hover { border-color: var(--primary-border); background: var(--primary-soft); }
.feedback-btn.active { background: var(--primary-soft); border-color: var(--primary); color: var(--primary); }

@media (max-width: 820px) {
  .learning-layout { grid-template-columns: 1fr; max-width: min(100vw - 16px, 1200px); }
}

.module-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: var(--space-4);
  margin-bottom: var(--space-4);
}
.module-card {
  background: var(--surface-card);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: var(--space-5);
  cursor: pointer;
  transition: all 0.15s ease;
  display: grid;
  gap: var(--space-2);
}
.module-card:hover {
  border-color: var(--primary-border);
  box-shadow: var(--shadow-md);
  transform: translateY(-2px);
}
.module-card-danger:hover {
  border-color: var(--danger-border);
  box-shadow: 0 16px 42px rgba(184, 94, 94, 0.12);
}
.module-icon {
  width: 40px;
  height: 40px;
  border-radius: var(--radius);
  background: var(--primary-soft);
  color: var(--primary);
  border: 1px solid var(--primary-border);
  display: grid;
  place-items: center;
  font-weight: 800;
  font-size: var(--text-sm);
}
.module-title {
  font-size: var(--text-lg);
  font-weight: 700;
  margin: 0;
}
.module-desc {
  font-size: var(--text-xs);
  color: var(--text-secondary);
  line-height: 1.5;
  margin: 0;
}
.t0-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
  gap: var(--space-3);
}
.t0-item {
  display: grid;
  gap: 4px;
}
.t0-label {
  font-size: var(--text-xs);
  color: var(--text-muted);
  font-weight: 600;
}
.balance-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--text-sm);
  margin-top: var(--space-3);
}
.balance-table th {
  background: rgba(255,255,255,.34);
  text-align: left;
  padding: 6px 10px;
  font-weight: 700;
  color: var(--text-secondary);
}
.balance-table td {
  padding: 6px 10px;
  border-bottom: 1px solid var(--border-light);
}
.balance-table tr:hover td { background: rgba(255,255,255,.26); }
@media (max-width: 768px) {
  :root { --space-6: 20px; --space-5: 16px; --space-4: 12px; }
  .container, .page-wrap, .teacher-page, .student-page, .nav { width: calc(100vw - 16px); }
  .nav { min-height: 56px; }
  .card-hd, .card-bd, .group-card { padding: var(--space-4); }
  .group-board { grid-template-columns: 1fr; }
  .group-card-head { flex-direction: column; }
  .msg .msg-content { max-width: 100%; }
  .kpi-grid { grid-template-columns: repeat(2, 1fr); }
  .teacher-hero { padding: var(--space-4); }
  h1 { font-size: var(--text-2xl); }
  h2 { font-size: var(--text-xl); }
}

"""

UI_STYLESHEET_LINKS = """
      <link rel="stylesheet" href="/static/ui/design-tokens.css">
      <link rel="stylesheet" href="/static/ui/ui-primitives.css">
      <link rel="stylesheet" href="/static/ui/ui-motion.css">
"""


def page_shell(title, body, script=""):
    return f"""
    <!doctype html>
    <html lang="zh-CN">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>{title}</title>
      <style>{BASE_CSS}</style>
      {UI_STYLESHEET_LINKS}
    </head>
    <body class="ui-page-background ui-bg-workspace">
      {body}
      {script}
    </body>
    </html>
    """

COLLAB_CSS = r"""
:root {
  --paper-bg: var(--ui-bg-base, #eef3ff);
  --paper-surface: var(--ui-glass-workspace, rgba(255,255,255,.70));
  --paper-strong: var(--ui-glass-card, rgba(255,255,255,.82));
  --paper-soft: var(--ui-glass-control, rgba(255,255,255,.64));
  --paper-card: rgba(255,255,255,.74);
  --ink: var(--ui-text-strong, #132b4f);
  --ink-2: var(--ui-text-primary, #314764);
  --ink-muted: var(--ui-text-muted, #62738c);
  --line: var(--ui-border-soft, rgba(145,169,207,.24));
  --line-soft: rgba(145,169,207,.16);
  --accent: var(--ui-brand-700, #1d548d);
  --accent-dim: var(--ui-brand-soft, rgba(70,118,190,.12));
  --accent-soft: rgba(70,118,190,.08);
  --accent-border: var(--ui-border-strong, rgba(93,126,177,.40));
  --user-color: #776b61;
  --user-dim: rgba(92,80,68,.10);
  --agent-color: var(--ui-brand-700, #1d548d);
  --agent-dim: var(--ui-brand-soft, rgba(70,118,190,.12));
  --system-color: #9b8570;
  --system-dim: rgba(155,133,112,.12);
  --success-bg: var(--ui-success-surface, #edf7f0);
  --success-text: var(--ui-success-text, #376f4b);
  --warning-bg: var(--ui-warning-surface, #fff6e8);
  --warning-text: var(--ui-warning-text, #8b5f21);
  --danger-bg: var(--ui-danger-surface, #fff0ef);
  --danger-text: var(--ui-danger-text, #984848);
  --radius-sm: var(--ui-radius-small, 12px);
  --radius: var(--ui-radius-control, 18px);
  --radius-lg: var(--ui-radius-panel, 22px);
  --radius-xl: var(--ui-radius-card, 28px);
  --radius-2xl: 32px;
  --shadow-soft: var(--ui-shadow-control, 0 7px 22px rgba(54,83,137,.06));
  --shadow: var(--ui-shadow-workspace, 0 30px 80px rgba(54,83,137,.12));
  --font-body: var(--ui-font-sans, "Microsoft YaHei", "PingFang SC", -apple-system, BlinkMacSystemFont, sans-serif);
  --font-heading: var(--ui-font-sans, "Microsoft YaHei", "PingFang SC", sans-serif);
  --font-mono: var(--ui-font-mono, "Cascadia Code", "Fira Code", "Consolas", monospace);
}
"""




# Global status bar CSS for teacher pages
GLOBAL_BAR_CSS = """
/* T0 Global Status Bar */
.teacher-status-shell .topbar {
  z-index: 101;
}
.teacher-status-shell .gs-bar {
  top: 64px;
}
.teacher-status-shell .teacher-page-nav {
  box-shadow: none;
}
.teacher-status-shell .teacher-page-nav .nav-title {
  min-width: 0;
}
.teacher-status-shell .teacher-page-nav .nav-user {
  flex-wrap: wrap;
  justify-content: flex-end;
}
.gs-bar {
  background: var(--surface-alpha);
  border-bottom: 1px solid var(--border-light);
  padding: 6px 0;
  position: sticky;
  top: 0;
  z-index: 99;
  backdrop-filter: blur(22px);
  -webkit-backdrop-filter: blur(22px);
  font-size: 12px;
  line-height: 1.4;
}
.gs-inner {
  width: min(1200px, calc(100vw - 32px));
  margin: 0 auto;
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.gs-item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  white-space: nowrap;
  flex-shrink: 0;
}
.gs-title-item { max-width: 160px; overflow: hidden; }
.gs-truncate { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 120px; display: inline-block; vertical-align: middle; }
.gs-label {
  color: var(--text-muted);
  font-weight: 600;
  margin-right: 2px;
}
.gs-divider {
  display: inline-block;
  width: 1px;
  height: 14px;
  background: var(--border-light);
  flex-shrink: 0;
}
.gs-badge {
  display: inline-flex;
  align-items: center;
  padding: 1px 7px;
  border-radius: 9999px;
  font-size: 11px;
  font-weight: 700;
  white-space: nowrap;
}
.gs-badge-on { background: var(--success-soft); color: var(--success-text); border: 1px solid var(--success-border); }
.gs-badge-off { background: var(--surface-soft); color: var(--text-muted); border: 1px solid var(--border-light); }
.gs-badge-running { background: var(--success-soft); color: var(--success-text); border: 1px solid var(--success-border); }
.gs-badge-draft { background: var(--primary-soft); color: var(--primary); border: 1px solid var(--primary-border); }
.gs-badge-ended { background: var(--warning-soft); color: var(--warning-text); border: 1px solid var(--warning-border); }
.gs-badge-archived { background: var(--surface-soft); color: var(--text-muted); border: 1px solid var(--border-light); }
.gs-badge-frozen { background: var(--warning-soft); color: var(--warning-text); border: 1px solid var(--warning-border); }
.gs-badge-warn { background: var(--warning-soft); color: var(--warning-text); border: 1px solid var(--warning-border); }
.gs-badge-critical { background: var(--danger-soft); color: var(--danger-text); border: 1px solid var(--danger-border); }
@media (max-width: 768px) {
  .teacher-status-shell .gs-bar { top: 56px; }
}
"""
def teacher_shell(title, body, script="", head=""):
    """Wrap teacher page body with the shared teacher navigation and status bar."""
    try:
        from auth import current_user
        user = dict(current_user() or {})
    except Exception:
        user = {}
    import re
    from html import escape as _escape

    body = re.sub(r'\s*<a class="btn small secondary" href="/logout">.*?</a>', '', body, flags=re.S)
    body = re.sub(
        r'(<div class="container">\s*)'
        r'<div class="nav"[^>]*>\s*'
        r'<div class="nav-title">.*?</div>\s*'
        r'<div class="nav-user">.*?</div>\s*'
        r'</div>\s*',
        r'\1',
        body,
        count=1,
        flags=re.S,
    )
    body = re.sub(r'\s*<!-- Row 1: Current Status Bar.*?<!-- Row 2:', '\n  <!-- Row 2:', body, flags=re.S)
    display_name = user.get("real_name") or user.get("username") or "教师"
    nav_title = (title or "教师端").replace(" - SSRL-ESP", "")
    bg_class = "ui-bg-analytics" if any(key in (title or "") for key in ("参与度统计", "情绪趋势")) else "ui-bg-workspace"
    nav_html = f'''<nav class="topbar teacher-page-nav">
  <div class="nav">
    <div class="nav-title">{_escape(nav_title)}</div>
    <div class="nav-user">
      <span>{_escape(display_name)}</span>
      <a class="btn small secondary" href="/teacher">教师端</a>
      <a class="btn small secondary" href="/logout">退出</a>
    </div>
  </div>
</nav>'''
    gs_html = '''<div id="gsBar" class="gs-bar">
  <div class="gs-inner" id="gsContent">
    <span class="gs-item"><span class="gs-label">加载中...</span></span>
  </div>
</div>'''
    all_script = script + '<script src="/static/teacher/global-status.js"></script>'
    combined_body = nav_html + "\n" + gs_html + "\n" + body
    return f'''
    <!doctype html>
    <html lang="zh-CN">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>{title}</title>
      <style>{BASE_CSS}</style>
      <style>{GLOBAL_BAR_CSS}</style>
      {UI_STYLESHEET_LINKS}
      {head}
    </head>
    <body class="teacher-status-shell ui-page-background {bg_class}">
      {combined_body}
      {all_script}
    </body>
    </html>
    '''



def collab_shell(title, body, script="", shell_class=""):
    extra = " " + shell_class.strip() if shell_class else ""
    return f"""
    <!doctype html>
    <html lang="zh-CN">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>{title}</title>
      <style>{BASE_CSS}</style>
      <style>{COLLAB_CSS}</style>
      {UI_STYLESHEET_LINKS}
    </head>
    <body class="collab-body ui-page-background ui-bg-workspace">
      <div class="bg-grid" aria-hidden="true"></div>
      <div class="collab-shell student-workspace ui-workspace{extra}">
        {body}
      </div>
      {script}
    </body>
    </html>
    """

