"""Verify HUD public gateway calls with a dedicated synthetic conversation."""
import argparse
import json
from pathlib import Path
import sys
import uuid
from urllib.parse import urlsplit
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home',type=Path,required=True)
    parser.add_argument('--profile',default='default')
    parser.add_argument('--expected-model',required=True)
    parser.add_argument('--expected-provider',required=True)
    parser.add_argument('--expected-host',required=True)
    parser.add_argument('--evidence',type=Path,required=True)
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--live',action='store_true')
    mode.add_argument('--inspect-only',action='store_true')
    args=parser.parse_args()
    if not args.home.is_absolute() or not args.evidence.is_absolute():
        parser.error('absolute home/evidence paths are required')
    if args.evidence.exists():
        parser.error('evidence must be a new file')
    args.evidence.parent.mkdir(parents=True,exist_ok=True)
    import yaml
    home=args.home if args.profile=='default' else args.home/'profiles'/args.profile
    config=yaml.safe_load((home/'config.yaml').read_text(encoding='utf-8')) or {}
    model=config.get('model') or {}
    if (model.get('default') or model.get('model'))!=args.expected_model or model.get('provider')!=args.expected_provider:
        raise RuntimeError('Model route differs from the requested test configuration')
    if urlsplit(model.get('base_url','')).hostname != args.expected_host:
        raise RuntimeError('Model endpoint differs from the requested test configuration')
    from engine import hud_runtime, hud_gateway, hermes_gateway, hermes_gateway_sessions
    if not hermes_gateway.enabled():
        raise RuntimeError('Selected Hermes installation does not use the group gateway path')
    result={'passed':False,'mode':'inspect-only' if args.inspect_only else 'live','model':args.expected_model,'provider':args.expected_provider,
            'host':args.expected_host,'reasoning_events':0,'stages':[],'calls':[]}
    original=hermes_gateway.connection
    def connect(*values,**options):
        client,root,command=original(*values,**options)
        rpc=client.rpc
        def observed(method,**params):
            result['calls'].append(method)
            response=rpc(method,**params)
            if method=='tools.show':result['tools_total']=response.get('total')
            if method=='session.create':result['session_id']=response['session_id']
            return response
        client.rpc=observed
        return client,root,command
    def progress(kind,text):
        if kind=='reasoning':result['reasoning_events']+=1
        elif kind=='stage':result['stages'].append(text)
    def saved(record):
        result['execution']={k:record.get(k) for k in ('session_id','admission_id','terminal')}
    try:
        if args.inspect_only:
            client,_,_=connect(args.profile,home=home)
            try:
                description=client.rpc('runtime.describe')
                result['durable_admission']='durable-admission-v1' in description.get('capabilities',[])
                assert result['durable_admission'], 'Gateway lacks durable admissions'
                snapshot=hermes_gateway_sessions.new(client,title='HUD 接口验证 · '+uuid.uuid4().hex,
                    workdir=str(args.evidence.parent),
                    system_prompt='',model_args=[],launch_options={'toolsets':['no_mcp'], 'ignore_rules':True,
                        'max_turns':2,'checkpoints':False,'accept_hooks':False})
                view=client.rpc('tools.show',session_id=snapshot['session_id'])
                result['reasoning_display']=client.rpc('config.set',session_id=snapshot['session_id'],key='show_reasoning',value='1')
                assert view.get('total')==0,view
                assert 'prompt.submit' not in result['calls']
                result['passed']=True
                print(json.dumps({'passed':True,'mode':'inspect-only','tools_total':view['total']},ensure_ascii=False))
                return
            finally:
                client.close()
        with patch.object(hermes_gateway,'connection',side_effect=connect), \
             patch.object(hud_gateway,'_save',side_effect=saved), \
             patch.object(hud_runtime,'_run',side_effect=AssertionError('Unexpected legacy subprocess')), \
             hud_runtime.progress_sink(progress):
            result['response']=hud_runtime.respond(
                {'runtime':'hermes','config_root':str(args.home),'profile':args.profile},
                {'stage':'purpose','project_created':False,'project':{'display_name':'合成网关验证项目',
                 'description':'只验证维护助手响应，无需读取文件或调用工具。'},'template':{},
                 'materials':{},'history':[{'text':'用一句话确认已收到合成测试资料。'}],
                 'facts':[],'allowed_changes':[]})
        assert result.get('tools_total')==0,result
        assert result['calls'].count('prompt.submit')==1,result
        assert result['execution'].get('terminal'),result
        result['passed']=True
    except Exception as exc:
        result['error']=str(exc)
        raise
    finally:
        args.evidence.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed':True,'reasoning_events':result['reasoning_events'],'tools_total':result['tools_total']},ensure_ascii=False))


if __name__=='__main__':main()
