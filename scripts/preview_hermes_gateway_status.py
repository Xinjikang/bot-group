"""Local browser fixture with production topbar/assets and deterministic status assertions."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parents[1] / 'frontend'


def frame(theme):
    source = (ROOT/'index.html').read_text(encoding='utf-8')
    styles = '\n'.join(re.findall(r'<style[^>]*>(.*?)</style>', source, re.S))
    topbar = source[source.index('  <div id="topbar">'):source.index('  <!-- 登录 -->')]
    return f'''<!doctype html><html data-theme="{theme}"><head><meta charset="utf-8">
<style>{styles}</style><link rel="stylesheet" href="/app/appearance.css">
<link rel="stylesheet" href="/app/hermes_gateway_status.css"></head>
<body><div id="app">{topbar}<div id="login" hidden></div><pre id="results">RUNNING</pre></div>
<script>
const S={{isUserIdentity:true}};
let fixture={{mode:'gateway',state:'connected'}}, clock=Date.now(), interval;
Date.now=()=>clock;
window.setInterval=fn=>{{interval=fn;return 1;}};
async function api(){{return fixture;}}
document.querySelector('#conn').dataset.state='connected';
document.querySelector('#conn-text').textContent='引擎已连接';
</script><script src="/app/hermes_gateway_status.js"></script>
<script>
(async()=>{{
 const results=[],element=document.querySelector('#hermes-gateway-status');
 const settle=()=>new Promise(resolve=>setTimeout(resolve,20));
 const assert=(condition,name)=>{{if(!condition)throw Error(name);results.push(name);}};
 await settle();
 for(const state of ['waiting','connecting','connected','offline','error','restart_required','unknown']){{
  fixture={{mode:'gateway',state}};HermesGatewayStatus.engineConnection('connected');await settle();
  assert(!element.hidden&&element.dataset.state===state,'state '+state);
  const r=element.getBoundingClientRect();assert(r.right<=innerWidth&&r.left>=0,'fits '+state);
 }}
 fixture={{mode:'subprocess',state:'connected'}};HermesGatewayStatus.engineConnection('connected');await settle();
 assert(element.hidden&&element.getBoundingClientRect().width===0,'subprocess hidden without space');
 fixture={{mode:'gateway',state:'connected'}};HermesGatewayStatus.engineConnection('connected');await settle();
 HermesGatewayStatus.engineConnection('disconnected');
 assert(element.dataset.state==='unknown','engine disconnect');
 HermesGatewayStatus.engineConnection('connected');await settle();
 clock+=16000;Object.defineProperty(document,'hidden',{{configurable:true,get:()=>true}});interval();
 assert(element.dataset.state==='unknown','stale status');
 Object.defineProperty(document,'hidden',{{configurable:true,get:()=>false}});
 S.isUserIdentity=false;interval();assert(element.hidden,'logout hides status');
 S.isUserIdentity=true;HermesGatewayStatus.engineConnection('connected');await settle();
 assert(element.tagName==='SPAN'&&!element.hasAttribute('tabindex')&&!element.onclick,'display only');
 assert(!element.querySelector('button,a'),'no action or detail');
 const r=element.getBoundingClientRect();assert(r.right<=innerWidth&&r.left>=0,'fits viewport');
 document.querySelector('#results').textContent='PASS '+results.length+' checks\\n'+results.join('\\n');
}})().catch(error=>document.querySelector('#results').textContent='FAIL '+error.message);
</script></body></html>'''


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        if url.path == '/':
            content = '''<!doctype html><meta charset="utf-8"><title>Hermes gateway status verification</title>
<style>body{margin:0;background:#999}iframe{border:0;height:440px;display:block;margin-bottom:12px}</style>
<iframe title="desktop dark" width="1200" src="/frame?theme=dark"></iframe>
<iframe title="mobile light" width="390" src="/frame?theme=light"></iframe>
<iframe title="mobile dark" width="390" src="/frame?theme=dark"></iframe>'''
            mime = 'text/html'
        elif url.path == '/frame':
            theme = 'light' if parse_qs(url.query).get('theme') == ['light'] else 'dark'
            content, mime = frame(theme), 'text/html'
        elif url.path.startswith('/app/') and Path(url.path).name in (
                'appearance.css', 'hermes_gateway_status.css', 'hermes_gateway_status.js'):
            content = (ROOT/Path(url.path).name).read_text(encoding='utf-8')
            mime = 'text/javascript' if url.path.endswith('.js') else 'text/css'
        else:
            self.send_error(404); return
        self.send_response(200); self.send_header('Content-Type', mime+'; charset=utf-8')
        self.end_headers(); self.wfile.write(content.encode('utf-8'))

    def log_message(self, *_):
        pass


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=0,help='Isolated port; zero asks the OS for an available port')
    args=parser.parse_args()
    with ThreadingHTTPServer(('127.0.0.1',args.port),Handler) as server:
        print('http://127.0.0.1:'+str(server.server_port),flush=True)
        try:server.serve_forever()
        except KeyboardInterrupt:pass
