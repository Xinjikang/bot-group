"""Isolated, local-only HUD interaction preview; never calls a model service."""
import argparse
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def fixture_response(bot, context):
    latest=(context.get('history') or [{}])[-1].get('text','')
    if '怎么更换维护助手' in latest:
        return {'text':'可以直接在 HUD 里更换，当前项目的维护记录会保留。','guide':{
            'title':'更换维护助手','steps':[
                {'title':'打开设置','body':'点击右上角的更多按钮，再选择“设置”。'},
                {'title':'选择维护 AI','body':'从已有 Bot 中选择一个，可以与项目负责人相同。'},
                {'title':'保存并开始维护','body':'确认选择后点击“保存并开始维护”，由新的助手继续维护当前项目。'}]}}
    decisions = context.get('decisions', [])
    if decisions and not context.get('facts'):
        return {'text': '我把你的回答整理成了资料登记建议，请确认。', 'proposal': {
            'changes': {}, 'facts': [{'label': '需求资料来源', 'value': decisions[-1]['answer']}]}}
    if context.get('facts'):
        return {'text': '资料约定已经保存。接下来想完善哪一部分？'}
    return {'text': '我们先把需求资料准备好，再确定下一步。', 'question': {
        'title': '这次项目的需求资料准备到哪一步了？',
        'reason': '确认资料来源后，才能把目录用途和后续准备工作整理清楚。你也可以直接输入自己的情况。',
        'options': ['已有完整文档，直接登记位置（推荐）', '资料分散，需要先整理', '还没有资料，先讨论项目目标']}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--port', type=int, required=True)
    args = parser.parse_args()
    data = args.data_dir.resolve()
    data.mkdir(parents=True, exist_ok=True)
    marker = data / 'HUD_LOCAL_FIXTURE'
    if (data / 'group.db').exists() and not marker.exists():
        raise SystemExit('Preview requires a fresh directory or its own marked fixture')
    marker.touch()
    os.environ.update(GROUP_DATA_DIR=str(data), GROUP_DB=str(data/'group.db'),
                      HERMES_HOME=str(data/'hermes'), CODEX_HOME=str(data/'codex'), GROUP_ENGINE_PORT=str(args.port))
    from engine import config, db, hud_runtime, project_hud
    config.apply_cli(host='127.0.0.1',port=args.port,data_dir=str(data))
    hud_runtime.respond = fixture_response
    (data/'project').mkdir(exist_ok=True)
    with db.get_connection() as conn:
        db.init_schema(conn)
        conn.execute("INSERT OR IGNORE INTO projects(name,root,display_name,description,directory_mode,created_at) VALUES('hud-preview',?,'HUD 交互预览','本地验证数据；此预览使用固定响应，不连接模型服务。','shared',?)", (str(data/'project'),db.now_iso()))
        conn.execute("INSERT OR IGNORE INTO bots(id,host_type,profile,config_root,runtime,display_name) VALUES(900001,'hermes','hud-fixture',?,'hermes','维护助手 · 本地验证')", (str(data/'hermes'),))
        conn.execute("INSERT INTO settings(key,project,value,updated_at) VALUES('hud_bot','','900001',?) ON CONFLICT(key,project) DO UPDATE SET value='900001'", (db.now_iso(),))
    session=project_hud.create('hud-preview')
    if session['status']=='paused':
        project_hud.update(session['id'],{'revision':session['revision'],'bot_id':900001})
    from engine.main import app
    @app.post('/hud-preview/reset')
    def reset_fixture():
        with project_hud.connection() as conn:
            ids=[r[0] for r in conn.execute('SELECT id FROM hud_sessions')]
            for sid in ids:
                conn.execute('DELETE FROM hud_entries WHERE session=?',(sid,))
            conn.execute("DELETE FROM hud_sessions WHERE project IS NULL")
            conn.execute("UPDATE hud_sessions SET state='{}',status='queued',revision=revision+1 WHERE project='hud-preview'")
        return {'fixture':True}
    import uvicorn
    uvicorn.run(app,host='127.0.0.1',port=args.port,log_level='warning')


if __name__=='__main__':
    main()
