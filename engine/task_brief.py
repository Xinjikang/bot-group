"""Task publication gate and complete, project-scoped handoff packets."""
import json

FIELDS = ("description", "design_basis", "acceptance_criteria")


def ready(task):
    return task["briefing_status"] == "ready" and all(
        str(task[field] or "").strip() for field in FIELDS)


def dependency_blockers(conn, task):
    """Only completed prerequisites release work; invalid references fail closed."""
    try:
        deps = task["prerequisites"]
        deps = json.loads(deps) if isinstance(deps, str) else deps
        if not isinstance(deps, list) or any(type(tid) is not int or tid <= 0 for tid in deps):
            raise ValueError("Invalid prerequisites")
    except (ValueError, TypeError):
        return [{"id": None, "status": "invalid", "title": "Invalid prerequisites"}]
    blockers = []
    for tid in deps:
        row = conn.execute("SELECT id,title,status FROM tasks WHERE id=? AND project=?",
                           (tid, task["project"])).fetchone()
        if row is None or row["status"] != "done":
            blockers.append(dict(row) if row else {"id": tid, "status": "missing", "title": ""})
    return blockers


def eligible(conn, task):
    return ready(task) and not dependency_blockers(conn, task)


def validate(conn, project, values, task_id=None, partial=False):
    errors = {}
    for field in FIELDS:
        value = values.get(field)
        if not isinstance(value, str) or (not partial and not value.strip()):
            errors[field] = "请填写非空文本" if not partial else "必须为文本"
    deps = values.get("prerequisites")
    if not isinstance(deps, list) or any(type(x) is not int or x <= 0 for x in deps):
        errors["prerequisites"] = "必须为任务 ID 数组；无前置任务请显式填写 []"
    elif len(deps) != len(set(deps)):
        errors["prerequisites"] = "任务 ID 重复"
    else:
        for dep in deps:
            row = conn.execute("SELECT prerequisites FROM tasks WHERE id=? AND project=?", (dep, project)).fetchone()
            if dep == task_id or row is None:
                errors["prerequisites"] = "前置任务必须属于当前项目且不能引用自己"
                break
            seen, pending = set(), [dep]
            while pending:
                current = pending.pop()
                if current == task_id:
                    errors["prerequisites"] = "前置任务存在循环引用"
                    break
                if current in seen:
                    continue
                seen.add(current)
                ref = conn.execute("SELECT prerequisites FROM tasks WHERE id=? AND project=?", (current, project)).fetchone()
                if ref:
                    pending.extend(json.loads(ref["prerequisites"]))
    if errors:
        return {"error": "任务说明未通过发布校验", "code": "task_brief_incomplete", "fields": errors}
    return None


def context(conn, task):
    task = dict(task)
    blockers = dependency_blockers(conn, task)
    task["prerequisites"] = json.loads(task["prerequisites"])
    requirement = conn.execute("SELECT * FROM requirements WHERE id=? AND project=?",
                               (task["requirement_id"], task["project"])).fetchone()
    def nodes(tid):
        return [dict(r) for r in conn.execute(
            "SELECT * FROM nodes WHERE task_id=? AND project=? ORDER BY id", (tid, task["project"]))]
    prerequisites = []
    for tid in task["prerequisites"]:
        row = conn.execute("SELECT * FROM tasks WHERE id=? AND project=?", (tid, task["project"])).fetchone()
        if row:
            prerequisites.append({"task": dict(row), "nodes": nodes(tid)})
    return {"task": task, "requirement": dict(requirement) if requirement else None,
            "nodes": nodes(task["id"]), "prerequisite_results": prerequisites,
            "dependency_blockers": blockers,
            "missing_fields": [f for f in FIELDS if not str(task[f] or "").strip()],
            "instructions": "先完整阅读需求、任务说明、设计依据、验收标准及前置交付记录，再执行。设计引用需打开原文核对；内容缺失时先补全。"}
