import sys, os, re

FILE = 'D:/workspace/nqy/routes/pages.py'

with open(FILE, 'r', encoding='utf-8') as f:
    content = f.read()

# Find boundaries
old_start = content.find('def teacher_dashboard():')
old_end = content.find('@app.route("/teacher/roster")')

if old_start < 0 or old_end < 0:
    raise RuntimeError(f"Could not find boundaries")

# Read template files
def read_template(name):
    with open(f'D:/workspace/nqy/templates/teacher/{name}', 'r', encoding='utf-8') as f:
        return f.read()

def extract_body(t):
    s = t.find('<div class="container"')
    if s < 0:
        s = t.find('<div class="container')
    e = t.find('<script src=')
    if e < 0:
        e = len(t)
    return t[s:e].strip() if s >= 0 else t

def extract_scripts(t):
    s = t.find('<script src=')
    return t[s:].strip() if s >= 0 else ''

DASH = read_template('dashboard.html')
SC = read_template('session_control.html')
AS = read_template('assignment.html')
ST = read_template('statistics.html')
AU = read_template('audit.html')
DQ = read_template('data_quality.html')
EX = read_template('export.html')

# Build the new code - use triple double-quote raw strings
# Escape any existing triple quotes in template content  
def esc(s):
    return s.replace('"""', '\\"\\"\\"')

bodies = {
    'DASHBOARD_BODY': esc(extract_body(DASH)),
    'DASHBOARD_SCRIPT': esc(extract_scripts(DASH)),
    'SESSION_CONTROL_BODY': esc(extract_body(SC)),
    'SESSION_CONTROL_SCRIPT': esc(extract_scripts(SC)),
    'ASSIGNMENT_BODY': esc(extract_body(AS)),
    'ASSIGNMENT_SCRIPT': esc(extract_scripts(AS)),
    'STATISTICS_BODY': esc(extract_body(ST)),
    'STATISTICS_SCRIPT': esc(extract_scripts(ST)),
    'AUDIT_BODY': esc(extract_body(AU)),
    'AUDIT_SCRIPT': esc(extract_scripts(AU)),
    'DATA_QUALITY_BODY': esc(extract_body(DQ)),
    'DATA_QUALITY_SCRIPT': esc(extract_scripts(DQ)),
    'EXPORT_BODY': esc(extract_body(EX)),
    'EXPORT_SCRIPT': esc(extract_scripts(EX)),
}

# Build the function body with proper escaping
new_func = '''
def teacher_dashboard():
    """New research console dashboard with T0-T8 module entry grid."""
    user = current_user()
    tab_token = get_tab_token_from_request()
    if not tab_token:
        tab_token = create_client_session(user["id"], user["role"], login_method="password")
        return redirect(url_for("teacher_dashboard", tab_token=tab_token))
    real_name = user.get("real_name") or user.get("username") or ""
    body = DASHBOARD_BODY_TEMPLATE.replace("{real_name}", real_name)
    script = DASHBOARD_SCRIPT_TEMPLATE
    return render_template_string(page_shell("\\u7814\\u7a76\\u63a7\\u5236\\u53f0 - SSRL-ESP", body, script))

# Sub-pages: T1-T8 module pages
@app.route("/teacher/session/control")
@login_required("teacher")
def teacher_session_control():
    """T1: Experiment session control page."""
    user = current_user()
    tab_token = get_tab_token_from_request()
    if not tab_token:
        tab_token = create_client_session(user["id"], user["role"], login_method="password")
        return redirect(url_for("teacher_session_control", tab_token=tab_token))
    real_name = user.get("real_name") or user.get("username") or ""
    body = SESSION_CONTROL_BODY_TEMPLATE.replace("{real_name}", real_name)
    script = SESSION_CONTROL_SCRIPT_TEMPLATE
    return render_template_string(page_shell("\\u5b9e\\u9a8c\\u63a7\\u5236 - SSRL-ESP", body, script))

@app.route("/teacher/assignment")
@login_required("teacher")
def teacher_assignment():
    """T2: Condition assignment page."""
    user = current_user()
    tab_token = get_tab_token_from_request()
    if not tab_token:
        tab_token = create_client_session(user["id"], user["role"], login_method="password")
        return redirect(url_for("teacher_assignment", tab_token=tab_token))
    real_name = user.get("real_name") or user.get("username") or ""
    body = ASSIGNMENT_BODY_TEMPLATE.replace("{real_name}", real_name)
    script = ASSIGNMENT_SCRIPT_TEMPLATE
    return render_template_string(page_shell("Condition \\u5206\\u914d - SSRL-ESP", body, script))

@app.route("/teacher/statistics")
@login_required("teacher")
def teacher_statistics():
    """T3/T4: Participation & Emotion statistics (placeholder)."""
    user = current_user()
    tab_token = get_tab_token_from_request()
    if not tab_token:
        tab_token = create_client_session(user["id"], user["role"], login_method="password")
        return redirect(url_for("teacher_statistics", tab_token=tab_token))
    real_name = user.get("real_name") or user.get("username") or ""
    body = STATISTICS_BODY_TEMPLATE.replace("{real_name}", real_name)
    script = STATISTICS_SCRIPT_TEMPLATE
    return render_template_string(page_shell("\\u6570\\u636e\\u7edf\\u8ba1 - SSRL-ESP", body, script))

@app.route("/teacher/audit")
@login_required("teacher")
def teacher_audit():
    """T5: Agent audit log page (placeholder)."""
    user = current_user()
    tab_token = get_tab_token_from_request()
    if not tab_token:
        tab_token = create_client_session(user["id"], user["role"], login_method="password")
        return redirect(url_for("teacher_audit", tab_token=tab_token))
    real_name = user.get("real_name") or user.get("username") or ""
    body = AUDIT_BODY_TEMPLATE.replace("{real_name}", real_name)
    script = AUDIT_SCRIPT_TEMPLATE
    return render_template_string(page_shell("Agent \\u5ba1\\u8ba1 - SSRL-ESP", body, script))

@app.route("/teacher/data-quality")
@login_required("teacher")
def teacher_data_quality():
    """T6: Data quality & scoring page (placeholder)."""
    user = current_user()
    tab_token = get_tab_token_from_request()
    if not tab_token:
        tab_token = create_client_session(user["id"], user["role"], login_method="password")
        return redirect(url_for("teacher_data_quality", tab_token=tab_token))
    real_name = user.get("real_name") or user.get("username") or ""
    body = DATA_QUALITY_BODY_TEMPLATE.replace("{real_name}", real_name)
    script = DATA_QUALITY_SCRIPT_TEMPLATE
    return render_template_string(page_shell("\\u6570\\u636e\\u8d28\\u91cf - SSRL-ESP", body, script))

@app.route("/teacher/export")
@login_required("teacher")
def teacher_export_page():
    """T7: Export page."""
    user = current_user()
    tab_token = get_tab_token_from_request()
    if not tab_token:
        tab_token = create_client_session(user["id"], user["role"], login_method="password")
        return redirect(url_for("teacher_export_page", tab_token=tab_token))
    real_name = user.get("real_name") or user.get("username") or ""
    body = EXPORT_BODY_TEMPLATE.replace("{real_name}", real_name)
    script = EXPORT_SCRIPT_TEMPLATE
    return render_template_string(page_shell("\\u6570\\u636e\\u5bfc\\u51fa - SSRL-ESP", body, script))
'''

# Now write the template holders - write them as proper Python strings
# We need to be careful with encoding. Write inline using repr-like escaping.

new_content = content[:old_start] + new_func + content[old_end:]

# Add the template constants after all the route definitions but before the end of file
# Find a good insertion point - after all route defs and before any remaining code
all_done = new_content.rfind('# API ')
if all_done < 0:
    all_done = new_content.rfind('\\n\\n') 
    if all_done < 0:
        all_done = len(new_content)

# Build template constant section
template_section = '''

# ============================================================
# Teacher page template bodies (inlined from templates/teacher/)
# ============================================================

DASHBOARD_BODY_TEMPLATE = """'''
template_section += bodies['DASHBOARD_BODY']
template_section += '"""\nDASHBOARD_SCRIPT_TEMPLATE = """'
template_section += bodies['DASHBOARD_SCRIPT']
template_section += '"""\n\nSESSION_CONTROL_BODY_TEMPLATE = """'
template_section += bodies['SESSION_CONTROL_BODY']
template_section += '"""\nSESSION_CONTROL_SCRIPT_TEMPLATE = """'
template_section += bodies['SESSION_CONTROL_SCRIPT']
template_section += '"""\n\nASSIGNMENT_BODY_TEMPLATE = """'
template_section += bodies['ASSIGNMENT_BODY']
template_section += '"""\nASSIGNMENT_SCRIPT_TEMPLATE = """'
template_section += bodies['ASSIGNMENT_SCRIPT']
template_section += '"""\n\nSTATISTICS_BODY_TEMPLATE = """'
template_section += bodies['STATISTICS_BODY']
template_section += '"""\nSTATISTICS_SCRIPT_TEMPLATE = """'
template_section += bodies['STATISTICS_SCRIPT']
template_section += '"""\n\nAUDIT_BODY_TEMPLATE = """'
template_section += bodies['AUDIT_BODY']
template_section += '"""\nAUDIT_SCRIPT_TEMPLATE = """'
template_section += bodies['AUDIT_SCRIPT']
template_section += '"""\n\nDATA_QUALITY_BODY_TEMPLATE = """'
template_section += bodies['DATA_QUALITY_BODY']
template_section += '"""\nDATA_QUALITY_SCRIPT_TEMPLATE = """'
template_section += bodies['DATA_QUALITY_SCRIPT']
template_section += '"""\n\nEXPORT_BODY_TEMPLATE = """'
template_section += bodies['EXPORT_BODY']
template_section += '"""\nEXPORT_SCRIPT_TEMPLATE = """'
template_section += bodies['EXPORT_SCRIPT']
template_section += '"""\n'

# Insert template constants before the API section comment
new_content = new_content[:all_done] + template_section + new_content[all_done:]

with open(FILE, 'w', encoding='utf-8') as f:
    f.write(new_content)

print("Rewrite complete!")
print(f"New file length: {len(new_content)} chars")
