import sys
with open('D:/workspace/nqy/routes/pages.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Fix teacher_interventions function
# Find the exact pattern and replace
old = 'def teacher_interventions():\n    """干预策略管理页面 - 列出可用策略供教师选择推送."""\n    user = current_user()'
new = 'def teacher_interventions():\n    """干预策略管理页面 - 列出可用策略供教师选择推送."""\n    user = dict(current_user())'

if old in content:
    content = content.replace(old, new)
    with open('D:/workspace/nqy/routes/pages.py', 'w', encoding='utf-8') as f:
        f.write(content)
    print('Fixed teacher_interventions')
else:
    # Try without the docstring
    alt_old = 'def teacher_interventions():\n    user = current_user()'
    if alt_old in content:
        content = content.replace(alt_old, 'def teacher_interventions():\n    user = dict(current_user())')
        with open('D:/workspace/nqy/routes/pages.py', 'w', encoding='utf-8') as f:
            f.write(content)
        print('Fixed teacher_interventions (alt)')
    else:
        print('Pattern not found')
        # Search for the function
        idx = content.find('def teacher_interventions')
        if idx >= 0:
            print('Found at:', idx)
            print(repr(content[idx:idx+200]))
