"""Explicitly authorized HUD model verification using only synthetic projects."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true',required=True)
    parser.add_argument('--hermes-home',type=Path,required=True)
    parser.add_argument('--expected-provider',required=True)
    parser.add_argument('--expected-host',required=True)
    parser.add_argument('--expected-model',required=True)
    parser.add_argument('--evidence',type=Path,required=True)
    args=parser.parse_args()
    home=args.hermes_home.resolve()
    import yaml
    model=(yaml.safe_load((home/'config.yaml').read_text(encoding='utf-8')) or {}).get('model') or {}
    if not isinstance(model,dict) or (model.get('provider'),urlsplit(model.get('base_url','')).hostname,model.get('default')) != (args.expected_provider,args.expected_host,args.expected_model):
        raise SystemExit('Configured model route differs from the explicitly approved route')
    if args.evidence.exists():
        raise SystemExit('Use a new evidence filename')
    args.evidence.parent.mkdir(parents=True,exist_ok=True)
    def fingerprints():
        return {name:hashlib.sha256((home/name).read_bytes()).hexdigest() for name in ('config.yaml','.env','auth.json') if (home/name).is_file()}
    before=fingerprints()
    records=[]
    with tempfile.TemporaryDirectory(prefix='hud-live-') as folder:
        root=Path(folder)
        os.environ.update(GROUP_DATA_DIR=str(root/'data'),GROUP_DB=str(root/'data/group.db'))
        from engine import config,db,project_hud as hud
        config.apply_cli(data_dir=str(root/'data'))
        with closing(db.get_connection()) as conn, conn:
            db.init_schema(conn)
            conn.execute("INSERT INTO bots(id,host_type,profile,config_root,runtime,display_name) VALUES(900002,'hermes','default',?,'hermes','维护验证')",(str(home),))
        def session(template,description,stage='purpose',document=''):
            project=root/(template+'-project');project.mkdir(exist_ok=True)
            (project/'README.md').write_text(document or description,encoding='utf-8')
            item=hud.create()
            return hud.update(item['id'],{'revision':item['revision'],'bot_id':900002,'stage':stage,'draft':{
                'display_name':{'research':'光伏行业观察','software':'社区图书馆','blank':'新项目'}[template],
                'template':template,'description':description,'root':str(project)}})
        def run(item,label):
            started=time.monotonic()
            hud.run_one(item['id'])
            result=hud.view(item['id'])
            record={'scenario':label,'seconds':round(time.monotonic()-started,2),'status':result['status'],
                    'generation':result['generation'],'error':result['error'],'response':result['entries'][-1]['content'] if result['entries'] else None}
            records.append(record)
            args.evidence.write_text(json.dumps({'route':{'provider':args.expected_provider,'host':args.expected_host,'model':args.expected_model},
                'records':records,'configuration_unchanged':before==fingerprints()},ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(record,ensure_ascii=False),flush=True)
            if result['status']=='error':raise RuntimeError(result['error'])
            return result
        item=run(session('research','研究光伏设备市场'),'research-purpose')
        assert item['state'].get('question'), 'A vague research goal should require clarification'
        assert len(item['state']['question']['options'])>=2
        text='服务内部产品团队，决定是否进入工商业光伏逆变器市场。范围为中国大陆，时间为2025至2026年。原始资料目录叫素材仓，最终报告放决策包。这些都已确定，请先登记这几个约定。'
        item=hud.answer(item['id'],{'revision':item['revision'],'question_id':item['state']['question']['id'],'text':text})
        item=run(item,'research-custom-answer')
        assert item['state'].get('proposal'), 'Explicit confirmed inputs should produce a reviewable proposal'
        proposal=item['state']['proposal']
        assert '素材仓' in json.dumps(proposal,ensure_ascii=False)
        assert '决策包' in json.dumps(proposal,ensure_ascii=False)
        item=hud.accept(item['id'],{'revision':item['revision'],'proposal_id':proposal['id']})
        previous_generation=item['generation']
        with hud.connection() as conn:
            conn.execute('UPDATE hud_sessions SET activity=? WHERE id=?',(time.time()-3600,item['id']))
        item=run(item,'research-after-confirmation-and-timeout')
        assert item['generation']==previous_generation+1, 'Idle reset must advance exactly one generation'
        assert item['state'].get('facts') or item['state']['draft'].get('description')!= '研究光伏设备市场'
        run(session('software','为社区图书馆开发借阅系统','resources',
                    '团队与规则均已确认。项目需要一个网页和一个 HTTP API，使用独立测试数据。现有本机服务已占用 8080。尚未选择浏览器调试端口和独立测试数据目录。'),
            'software-resources')
        item=run(session('blank',''),'blank-project-purpose')
        assert item['state'].get('question'), 'A blank project should ask about its purpose'
        with closing(db.get_connection()) as conn:
            assert all(conn.execute('SELECT count(*) FROM '+table).fetchone()[0]==0 for table in ('messages','threads','wake_jobs','session_bindings'))
        assert before==fingerprints(), 'Source Bot configuration must remain unchanged'
        assert not list((root/'data/hud-runtime').glob('turn-*')), 'Disposable homes must be cleaned'
    result=json.loads(args.evidence.read_text(encoding='utf-8'))
    result.update(passed=True,group_state_untouched=True,temporary_homes_cleaned=True)
    args.evidence.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':main()
