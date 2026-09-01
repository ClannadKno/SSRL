import sys; sys.path.insert(0, ".")
import app, auth
from core import app as f
from db import query_one, list_questionnaires

print("=== 1. Direct DB check ===")
qs = list_questionnaires(include_inactive=True, include_items=True, include_summary=True)
print(f"Questionnaires in DB: {len(qs)}")
for q in qs:
    items = q.get("items", [])
    summary = q.get("response_summary", {})
    print(f"  ID {q['id']}: active={q['active']}, items={len(items)}, summary={summary}")

print()
print("=== 2. API endpoint test ===")
t = query_one("SELECT * FROM users WHERE role='teacher' LIMIT 1")
if not t:
    print("no teacher")
    exit()
t = dict(t)
tok = auth.create_client_session(t["id"], "teacher", "password")
print(f"Token: {tok[:16]}...")

with f.test_client() as c:
    with c.session_transaction() as s:
        s["user_id"] = t["id"]
        s["role"] = "teacher"
    
    # Test API
    r = c.get("/api/teacher/questionnaires", headers={"X-Tab-Token": tok})
    print(f"API status: {r.status_code}")
    data = r.get_json()
    if r.status_code == 200:
        qlist = data.get("questionnaires", [])
        print(f"API returned: {len(qlist)} questionnaires")
        for q in qlist[:3]:
            print(f"  ID {q['id']}: {q['title'][:30]}... summary={q.get('response_summary')}")

print()
print("=== 3. Page rendering check ===")
with f.test_client() as c:
    with c.session_transaction() as s:
        s["user_id"] = t["id"]
        s["role"] = "teacher"
    r = c.get("/teacher/questionnaire-admin", headers={"X-Tab-Token": tok}, follow_redirects=True)
    html = r.data.decode("utf-8", "replace")
    print(f"Page status: {r.status_code}, HTML: {len(html)} bytes")
    
    # Check what JS will do
    # The renderQCards function checks data.questionnaires length
    # If data has questionnaires, it renders cards
    # If not, it shows "暂无问卷"
    print()
    print("=== Key JS logic ===")
    print("data.questionnaires||[] → uses the API response array")
    print("list.length ? list.map(...) : '<div class=\"evidence\">暂无问卷。</div>'")
    print()
    print("If API returns empty list → shows '暂无问卷'")
    print("If API returns data → renders cards")
    print("If API throws → loadQAdmin rejects → nothing rendered (shows initial '0 份')")
