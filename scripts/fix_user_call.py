content = open("D:/workspace/nqy/routes/pages.py","r",encoding="utf-8").read()
sm = "def teacher_dashboard():"
em = "@app.route(\"/teacher/roster\")"
start = content.find(sm)
end = content.find(em)
if start >= 0 and end >= 0:
    region = content[start:end]
    cb = region.count("user = current_user()")
    fixed = region.replace("user = current_user()","user = dict(current_user())")
    new_content = content[:start] + fixed + content[end:]
    open("D:/workspace/nqy/routes/pages.py","w",encoding="utf-8").write(new_content)
    print("Fixed:", cb, "occurrences")
else:
    print("Markers not found")
