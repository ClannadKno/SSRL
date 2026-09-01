import sys

def read_template(name):
    with open('D:/workspace/nqy/templates/teacher/' + name, 'r', encoding='utf-8') as f:
        return f.read()

def extract_body(t):
    s = t.find('<div class="container"')
    if s < 0:
        s = t.find('<div class="container')
    e = t.find('<script src=')
    if e < 0:
        e = len(t)
    return t[s:e].strip().replace('\r\n', '\n') if s >= 0 else t.replace('\r\n', '\n')

def extract_scripts(t):
    s = t.find('<script src=')
    return t[s:].strip().replace('\r\n', '\n') if s >= 0 else ''

ta = read_template('task_admin.html')
qa = read_template('questionnaire_admin.html')

with open('D:/workspace/nqy/routes/pages.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Find location after EXPORT_SCRIPT_TEMPLATE
marker_mid = 'EXPORT_SCRIPT_TEMPLATE = '
idx = content.find(marker_mid)
if idx < 0:
    print('Marker not found!')
    sys.exit(1)

# Find the end of this line
end_of_line = content.find('\n', idx)
idx_end = end_of_line + 1

ta_body = extract_body(ta)
ta_script = extract_scripts(ta)
qa_body = extract_body(qa)
qa_script = extract_scripts(qa)

# Create new constants
new_constants = '\n\n'
new_constants += 'TASK_ADMIN_BODY_TEMPLATE = """' + ta_body + '"""\n'
new_constants += 'TASK_ADMIN_SCRIPT_TEMPLATE = """' + ta_script + '"""\n\n'
new_constants += 'QUESTIONNAIRE_ADMIN_BODY_TEMPLATE = """' + qa_body + '"""\n'
new_constants += 'QUESTIONNAIRE_ADMIN_SCRIPT_TEMPLATE = """' + qa_script + '"""\n'

new_content = content[:idx_end] + new_constants + content[idx_end:]

with open('D:/workspace/nqy/routes/pages.py', 'w', encoding='utf-8') as f:
    f.write(new_content)

print('Template constants added successfully')
print('File size:', len(new_content), 'chars')
