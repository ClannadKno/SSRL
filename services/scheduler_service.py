# -*- coding: utf-8 -*-
"""
Deprecated V1 background scheduler.
V2 pipeline uses Huey for all background tasks.
This module kept for import compatibility.
All scheduling functions are no-ops.
"""
_scheduler_started = False

def list_schedulable_groups(now=None):
    return []

def get_group_scheduler_snapshot(group_id, now=None):
    return {}

def build_scheduler_plan(group_id, now=None):
    return None

def run_scheduler_tick(now=None):
    return []

def start_scheduler():
    return False

def stop_scheduler(wait=True):
    return False

def is_scheduler_running():
    return False
