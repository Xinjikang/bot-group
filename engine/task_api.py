"""HTTP boundary for MCP task operations; database access stays in the engine."""

from __future__ import annotations

import inspect
from functools import lru_cache
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, ValidationError, validate_call

from . import db, mcp_operations, task_access, taskpool


# Public argument lists exclude actor identity and internal workflow parameters.
OPERATIONS = {
    "req_create": "project title version priority background scope approach acceptance undecided source_ref",
    "req_update": "project requirement_id title background scope approach acceptance undecided source_ref",
    "req_list": "project status",
    "req_status": "project requirement_id",
    "req_resume": "project requirement_id",
    "req_complete": "project requirement_id",
    "req_cancel": "project requirement_id reason",
    "task_create": "project requirement_id tasks credential idempotency_key complete",
    "task_claim": "project",
    "task_log": "project task_id type meta result comment next_bot expected_role_id",
    "task_complete": "project task_id type meta result comment",
    "task_block": "project task_id reason kind",
    "task_update": "project task_id title next_bot description design_basis acceptance_criteria prerequisites publish credential",
    "task_resume": "project task_id next_bot",
    "task_get": "project task_id",
    "task_list": "project status",
    "task_cancel": "project task_id reason",
}


class ToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    arguments: dict[str, Any]
    execution: dict[str, Any] | None = None


@lru_cache
def _validated_function(operation):
    return validate_call(config={"strict": True})(getattr(taskpool, operation))


def create_router(identity_dependency):
    router = APIRouter(prefix="/task-tools", tags=["Task tools"])

    @router.post("/{operation}")
    @mcp_operations.recorded
    def call(operation: str, body: ToolRequest, request: Request, identity=Depends(identity_dependency)):
        actor, kind = identity
        if kind != "bot" or not actor:
            raise HTTPException(401, "A registered bot identity is required")
        if operation != "project_list" and operation not in OPERATIONS:
            raise HTTPException(404, "Unknown task operation")
        arguments = dict(body.arguments)
        allowed = set(OPERATIONS.get(operation, "").split())
        if set(arguments) - allowed:
            raise HTTPException(422, "Unknown or protected task arguments")
        execution = task_access.execution_for(request, body.execution)
        conn = db.get_connection()
        try:
            if operation == "project_list":
                if not conn.execute("SELECT 1 FROM members WHERE profile=?", (actor,)).fetchone():
                    raise HTTPException(403, "Unknown member")
                projects = {row["project"] for row in conn.execute(
                    "SELECT project FROM project_members WHERE profile=? AND active=1", (actor,))}
                return {"projects": [row for row in taskpool.project_list()["projects"]
                                     if row["name"] in projects]}
            project = arguments.get("project")
            if not isinstance(project, str) or not project.strip():
                raise HTTPException(422, "project is required")
            member = conn.execute(
                "SELECT 1 FROM project_members pm JOIN members m ON m.profile=pm.profile "
                "JOIN projects p ON p.name=pm.project "
                "WHERE pm.project=? AND pm.profile=? AND pm.active=1 AND p.archived=0 AND p.removed=0",
                (project, actor),
            ).fetchone()
            if not member:
                return {"error": "Member is not active in this project", "code": "permission_denied"}
            task_access.validate(conn, actor, project, operation, execution)
            if operation in {"task_create", "task_update"}:
                arguments["credential"] = task_access.credential_for(execution, arguments.get("credential"))
        finally:
            conn.close()
        function = getattr(taskpool, operation)
        if "role" in inspect.signature(function).parameters:
            arguments["role"] = actor
        try:
            with task_access.scope(actor, project, operation, execution):
                return task_access.public_result(_validated_function(operation)(**arguments))
        except ValidationError as exc:
            raise HTTPException(422, detail=exc.errors(include_context=False, include_url=False)) from exc

    return router
