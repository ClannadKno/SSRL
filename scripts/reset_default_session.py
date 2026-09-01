mport sys
ys.path.insert(0, "..")
rom db import now_str, execute, set_setting, db, ensure_database_ready
nsure_database_ready()
onn = db()
ry:
   # Already has data from reset script
   from werkzeug.security import generate_password_hash
   import os
   has_task = conn.execute("SELECT id FROM learning_tasks WHERE id=1").fetchone()
   if not has_task:
       tid = execute(
           "INSERT INTO learning_tasks(title,question,task_goal,output_requirement,time_limit_minutes,expected_dimensions_json,key_concepts_json,is_active,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
           ("默认协作任务","请与组内同学协作完成","协作完成学习目标","提交协作成果",30,"[]","[]",1,now_str())
       )
       print("[TASK] Created default learning task (id=%d)" % tid)
   else:
       tid = 1
       print("[TASK] Learning task already exists (id=1)")
   # Create running experiment session
   session = conn.execute(
       "SELECT id FROM experiment_sessions WHERE status='running'"
   ).fetchone()
   if not session:
       sid = execute(
           "INSERT INTO experiment_sessions(session_no,session_role,task_id,status,start_time,created_at) VALUES(?,?,?,?,?,?)",
           (1,"S1_baseline",tid,"running",now_str(),now_str())
       )
       print("[SESSION] Created running experiment session (id=%d)" % sid)
   else:
       print("[SESSION] Running session already exists (id=%d)" % session["id"])
   # Ensure setting
   set_setting("current_session_no","1")
   print("[SETTING] current_session_no=1")
   conn.close()
   print("[DONE] Collaborative editor should now work.")
xcept Exception as e:
   print("[ERROR] %s" % e)
   conn.close()
   sys.exit(1)
