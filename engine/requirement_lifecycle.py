"""Shared lifecycle gate for creating and advancing requirement work."""


def check(requirement):
    if requirement is None or requirement["status"] not in ("open", "blocked") or requirement["archived"]:
        return {"error": "Requirement is closed, cancelled, archived or missing",
                "code": "requirement_inactive"}
    return None


def check_task(conn, task):
    row = conn.execute("SELECT status,archived FROM requirements WHERE id=? AND project=?",
                       (task["requirement_id"], task["project"])).fetchone()
    return check(row)
