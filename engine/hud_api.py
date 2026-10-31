"""Administrator-only HUD API with optimistic writes and durable receipts."""
from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field
from . import project_hud as hud


class Revision(BaseModel):
    revision: int = Field(ge=0)


class Update(Revision):
    bot_id: int | None = Field(default=None, ge=1)
    make_default: bool = False
    idle_seconds: int | None = Field(default=None, ge=300, le=86400)
    draft: dict | None = None
    stage: str | None = None


class Create(BaseModel):
    project: str | None = Field(default=None, max_length=200)


class Answer(Revision):
    text: str = Field(min_length=1, max_length=12000)
    question_id: str | None = None
    dismiss_proposal: str | None = None


class Proposal(Revision):
    proposal_id: str


class Bind(BaseModel):
    request_id: str = Field(min_length=1, max_length=100)


class ToolConfig(BaseModel):
    changes: dict[str, str]
    expected_version: str = Field(min_length=64, max_length=64)


def router(identity, admin):
    routes = APIRouter()

    def allowed(who=Depends(identity)):
        admin(who, '项目维护 HUD 仅限管理员使用')

    protected = APIRouter(prefix='/hud', dependencies=[Depends(allowed)])

    @routes.post('/hud/{sid}/tool-config')
    def tool_config(sid: str, body: ToolConfig, authorization: str = Header(default='')):
        from . import hud_tools
        return hud_tools.update(sid, authorization.removeprefix('Bearer '), body.changes, body.expected_version)

    @protected.get('')
    def index():
        with hud.connection() as conn:
            sessions = [dict(r) for r in conn.execute('SELECT id,project,bot_id,status,state,updated FROM hud_sessions ORDER BY updated DESC')]
            for session in sessions:
                state = hud.json.loads(session.pop('state'))
                session['question'] = state.get('question')
                session['proposal'] = state.get('proposal')
                session['notification'] = state.get('notification')
                session['cards'] = state.get('cards', [])
                session['title'] = state.get('draft', {}).get('display_name', '创建项目草稿')
            return {'sessions': sessions, 'default_bot_id': hud.default_bot(conn)}

    @protected.post('')
    def create(body: Create):
        return hud.create(body.project)

    @protected.get('/{sid}')
    def get(sid: str):
        return hud.view(sid)

    @protected.patch('/{sid}')
    def update(sid: str, body: Update):
        return hud.update(sid, body.model_dump(exclude_unset=True))

    @protected.post('/{sid}/answer')
    def answer(sid: str, body: Answer):
        return hud.answer(sid, body.model_dump())

    @protected.post('/{sid}/accept')
    def accept(sid: str, body: Proposal):
        return hud.accept(sid, body.model_dump())

    @protected.post('/{sid}/retry')
    def retry(sid: str, body: Revision):
        with hud._LOCK, hud.connection() as conn:
            item = hud.checked(conn, sid, body.revision)
            return hud.store(conn, item, queue=True)

    @protected.post('/{sid}/bind')
    def bind(sid: str, body: Bind):
        return hud.bind(sid, body.request_id)

    routes.include_router(protected)
    return routes
