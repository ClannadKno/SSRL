# -*- coding: utf-8 -*-


def test_student_sync_baseline_metrics_count_tracked_request(student_login):
    from services.student_sync_observability import snapshot_student_sync_baseline_metrics

    client, headers, _user_id, _group_id = student_login
    before = snapshot_student_sync_baseline_metrics()
    before_count = before.get("student_heartbeat", {}).get("count", 0)

    response = client.post("/api/heartbeat", headers=headers)

    assert response.status_code == 200
    after = snapshot_student_sync_baseline_metrics()
    assert after["student_heartbeat"]["count"] == before_count + 1
