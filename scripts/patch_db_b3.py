import re
with open("D:\\workspace\\nqy\\db.py", "r", encoding="utf-8") as f:
    content = f.read()

old_insert = (
    "            INSERT INTO questionnaire_items(questionnaire_id, item_code, prompt_text,\n\n"
    "                dimension_label, sort_order, required, created_at)\n\n"
    "            VALUES(?,?,?,?,?,?,?)"
)
new_insert = (
    "            INSERT INTO questionnaire_items(questionnaire_id, item_code, prompt_text,\n\n"
    "                dimension_label, question_type, dimension_key, reverse_scored,\n"
    "                min_value, max_value, options_json, score_map_json,\n"
    "                include_in_score, help_text, sort_order, required, created_at)\n\n"
    "            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)
content = content.replace(old_insert, new_insert)
print("insert replaced:", old_insert not in content)

old_values = (
    "            questionnaire_id,\n\n"
    '            item.get("item_code", ""),\n\n'
    '            item.get("prompt_text", ""),\n\n'
    '            item.get("dimension_label", ""),\n\n'
    '            int(item.get("sort_order", 0)),\n\n'
    '            1 if item.get("required", True) else 0,\n\n'
    "            created,"
)
new_values = (
    "            questionnaire_id,\n\n"
    '            item.get("item_code", ""),\n\n'
    '            item.get("prompt_text", ""),\n\n'
    '            item.get("dimension_label", ""),\n\n'
    '            item.get("question_type", "likert"),\n'
    '            item.get("dimension_key", ""),\n'
    '            1 if item.get("reverse_scored", False) else 0,\n'
    '            int(item.get("min_value") or 1),\n'
    '            item.get("max_value"),\n'
    '            item.get("options_json"),\n'
    '            item.get("score_map_json"),\n'
    '            1 if item.get("include_in_score", True) else 0,\n'
    '            item.get("help_text", ""),\n\n'
    '            int(item.get("sort_order", 0)),\n\n'
    '            1 if item.get("required", True) else 0,\n\n'
    "            created,"
)
cnt_before = content.count(old_values)
content = content.replace(old_values, new_values)
print(f"values replaced: {cnt_before} -> {content.count(old_values)}")

with open("D:\\workspace\\nqy\\db.py", "w", encoding="utf-8") as f:
    f.write(content)
print("Done")
