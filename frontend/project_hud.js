// Project-scoped maintenance UI. Group messages and task conversations stay independent.
(() => {
  const host = document.createElement('section');
  host.id = 'project-hud'; host.setAttribute('aria-label', '项目维护助手');
  host.innerHTML = `<div class="hud-shell">
    <button class="hud-toggle" id="hud-toggle" aria-expanded="false" aria-controls="hud-panel"><span class="hud-light" aria-hidden="true"></span><span class="hud-caption">项目维护助手 · 准备就绪</span><span class="hud-chevron">展开</span></button>
    <div class="hud-panel" id="hud-panel" hidden>
      <div class="hud-context"><span id="hud-context"></span><button class="hud-link" id="hud-settings-toggle">设置</button></div>
      <div class="hud-scroll">
        <div id="hud-settings" class="hud-settings" hidden></div>
        <div id="hud-notices" class="hud-notices"></div>
        <div id="hud-content" aria-live="polite"></div>
        <p id="hud-error" class="hud-error-text" role="alert" hidden></p>
        <button class="hud-link" id="hud-retry" hidden>重新检查</button>
        <details class="hud-history"><summary>维护记录与已确认资料</summary><div id="hud-history"></div></details>
      </div>
      <form class="hud-compose" id="hud-compose">
        <label for="hud-input">你的回答或补充</label>
        <textarea id="hud-input" maxlength="12000" placeholder="可以选择上方选项，也可以直接输入你的想法…"></textarea>
        <div class="hud-actions"><span class="hud-hint" id="hud-hint">Ctrl + Enter 发送</span><button class="hud-primary" id="hud-send" type="submit">发送</button></div>
      </form>
    </div></div>`;
  document.body.append(host);
  const el = id => host.querySelector('#hud-' + id);
  let current = null, scope = '', version = 0, expanded = false, settings = false, busy = false, selected = '', refreshing = false;
  let draftMode = false, draftId = '', onApply = null, beforeConfirm = null, errorText = '', chain = Promise.resolve();
  const inputs = new Map(), selections = new Map(), silenced = new Set(), seenOthers = new Set();
  const stages = {purpose:'项目目标',collaboration:'协作方式',roles:'角色设置',rules:'协作规则',members:'成员分配',resources:'工作区资源',review:'确认创建',maintenance:'项目维护'};
  const fieldNames = {display_name:'项目名称',description:'项目简介',root:'项目目录',template:'项目模板',directory_mode:'协作方式',worktree_root:'工作区根目录'};
  const node = (tag, text, cls) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (cls) n.className = cls; return n; };
  const action = (text, fn, cls='hud-link') => { const b=node('button',text,cls); b.type='button'; b.addEventListener('click',fn); return b; };
  function remember() {
    if (!current) return;
    inputs.set(current.id, el('input').value);
    try { sessionStorage.setItem('hud-input:' + current.id, el('input').value); } catch {}
  }
  function toggle(value) {
    expanded=value; host.classList.toggle('expanded',value); el('panel').hidden=!value;
    el('toggle').setAttribute('aria-expanded',String(value)); host.querySelector('.hud-chevron').textContent=value?'收起':'展开';
    if (!value && current?.state.question) silenced.add(current.state.question.id);
    if (!value && current?.state.proposal) silenced.add(current.state.proposal.id);
  }
  el('toggle').onclick=()=>toggle(!expanded);
  el('input').addEventListener('input',()=>{remember(); updateSend();});
  el('input').addEventListener('keydown',e=>{if(e.key==='Enter'&&(e.ctrlKey||e.metaKey)&&!e.isComposing){e.preventDefault();el('compose').requestSubmit();}});
  host.addEventListener('keydown',e=>{if(e.key==='Escape'){e.preventDefault();e.stopPropagation();toggle(false);el('toggle').focus();}});
  function updateSend() { el('send').disabled=busy||!current?.bot||current?.status==='running'||!(selected||el('input').value.trim()); }
  function setData(data) {
    const changed=current?.id!==data.id;
    if(changed) remember();
    current=data;
    if(changed){
      let saved=''; try {saved=sessionStorage.getItem('hud-input:'+data.id)||'';}catch{}
      el('input').value=inputs.get(data.id)??saved;
    }
    let choice='';try{choice=sessionStorage.getItem('hud-choice:'+data.state.question?.id)||'';}catch{}
    selected=selections.get(data.state.question?.id)||choice;
    render();
  }
  function render() {
    const item=current, q=item?.state.question, proposal=item?.state.proposal;
    const state=item?.status;
    const label=!item?.bot?'选择 AI 开始维护':state==='paused'?'点击开始维护':state==='running'||state==='queued'?'正在检查项目':state==='error'?'检查未完成':q?'有 1 项需要你确认':proposal?'有配置建议待确认':'随时为你维护项目';
    host.querySelector('.hud-caption').textContent=(item?.bot?.display_name||'项目维护助手')+' · '+label;
    host.classList.toggle('hud-attention',!!q||!!proposal);host.classList.toggle('hud-running',state==='running'||state==='queued');host.classList.toggle('hud-error',state==='error');
    const project=typeof S!=='undefined'?S.projects?.find(p=>p.name===item?.project):null;
    el('context').textContent=(project?.display_name||item?.state.draft?.display_name|| (item?.project?'当前项目':'创建项目'))+' · '+(stages[item?.stage]||'项目维护');
    const content=el('content'); content.replaceChildren();
    if(!item?.bot){
      content.append(node('h3','为 HUD 选择一个 AI'),node('p','可以使用项目负责人，也可以选择其他 Bot。它会在这里帮你完善项目资料和配置。','hud-muted'));
      content.append(action('选择或连接 AI',()=>showSettings(true),'hud-primary'));
      if(!item)content.append(action('重试连接',()=>attach(scope||null,draftMode?draftId:null)));
    }else if(state==='paused'){
      content.append(node('h3','开始维护这个项目'),node('p','启用后，所选 AI 将使用项目配置、模板和项目内文档帮助你完善配置，并在发生变化时主动检查。','hud-muted'),action('开始维护',()=>mutate('/retry'),'hud-primary'));
    }else if(q){
      const box=node('div',null,'hud-question');box.append(node('h3',q.title),node('p',q.reason,'hud-reason'));
      const options=node('div',null,'hud-options');options.setAttribute('role','radiogroup');options.setAttribute('aria-label','建议回答');
      for(const option of q.options||[]){
        const b=action('',()=>{selected=selected===option?'':option;selections.set(q.id,selected);try{sessionStorage.setItem('hud-choice:'+q.id,selected);}catch{}render();host.querySelectorAll('.hud-option')[q.options.indexOf(option)]?.focus();},'hud-option');
        b.setAttribute('role','radio');b.setAttribute('aria-checked',String(selected===option));
        b.append(node('span',selected===option?'●':'○','hud-radio'),node('span',option)); options.append(b);
      }
      options.addEventListener('keydown',e=>{if(!['ArrowDown','ArrowUp','ArrowLeft','ArrowRight'].includes(e.key))return;e.preventDefault();const buttons=[...options.children],i=buttons.indexOf(document.activeElement),d=['ArrowDown','ArrowRight'].includes(e.key)?1:-1;buttons[(i+d+buttons.length)%buttons.length]?.focus();});
      box.append(options);content.append(box);
    }else{
      const latest=item.entries.filter(e=>e.role==='assistant').at(-1);
      content.append(node('p',state==='queued'||state==='running'?'正在结合项目资料与模板检查当前阶段…':latest?.content.text||'告诉我你想完善什么，我会结合项目资料协助你。','hud-text'));
    }
    if(proposal){
      const box=node('div',null,'hud-proposal');box.append(node('strong','待确认的配置建议'));const dl=node('dl');
      for(const [key,value] of Object.entries(proposal.changes))dl.append(node('dt',fieldNames[key]||key),node('dd',value));
      for(const fact of proposal.facts)dl.append(node('dt',fact.label),node('dd',fact.value));
      box.append(dl,action(Object.keys(proposal.changes).length?(item.project?'确认更新项目':'确认写入向导'):'确认保存维护资料',async()=>{
        if(draftMode)await beforeConfirm?.();
        const data=await mutate('/accept',{proposal_id:proposal.id});if(data&&draftMode&&Object.keys(proposal.changes).length)onApply?.(data.state.draft);
      },'hud-primary'));content.append(box);
    }
    el('error').textContent=errorText||item?.error||'';el('error').hidden=!el('error').textContent;
    el('retry').hidden=!item?.bot||['running','queued','paused','waiting'].includes(state);
    el('hint').textContent=state==='running'?'正在处理，输入会为你保留':selected?'已选答案，可补充文字后发送':'Ctrl + Enter 发送';
    el('input').disabled=!item?.bot;
    const history=el('history');history.replaceChildren();
    for(const fact of item?.state.facts||[]){const p=node('article');p.append(node('strong',fact.label),node('p',fact.value,'hud-text'));history.append(p);}
    for(const entry of item?.entries||[]){const p=node('article');p.append(node('small',entry.role==='user'?'你':entry.role==='assistant'?(item.bot?.display_name||'助手'):'维护记录'),node('p',entry.content.text,'hud-text'));history.append(p);}
    updateSend();
    if((q&&!silenced.has(q.id))||proposal&&!silenced.has(proposal.id))toggle(true);
  }
  async function mutate(path,body={},method='POST') {
    if(!current||busy)return null;
    const sid=current.id,stamp=version;busy=true;errorText='';updateSend();
    try{
      const data=await api('/hud/'+sid+path,{method,body:JSON.stringify({revision:current.revision,...body})});
      if(stamp===version)setData(data);return data;
    }catch(error){if(stamp===version){errorText=error.message;try{setData(await api('/hud/'+sid));}catch{}render();}return null;}
    finally{busy=false;updateSend();}
  }
  el('compose').addEventListener('submit',async e=>{
    e.preventDefault();if(el('send').disabled)return;
    const original=el('input').value,sid=current.id,q=current.state.question;
    const text=[selected,original.trim()].filter(Boolean).join('\n补充：');
    const data=await mutate('/answer',{text,question_id:q?.id||null});
    if(data&&current.id===sid){el('input').value='';inputs.delete(sid);selections.delete(q?.id);selected='';remember();render();}
  });
  el('retry').onclick=()=>mutate('/retry');
  el('settings-toggle').onclick=()=>showSettings(!settings);
  async function showSettings(value){
    settings=value;el('settings').hidden=!value;if(!value)return;
    const box=el('settings');box.replaceChildren(node('p','正在读取可用 AI…','hud-muted'));
    try{
      const bots=await MemberPicker.load();if(!settings)return;
      box.replaceChildren(node('p','HUD 独立选择 Bot；允许与项目负责人使用同一个。','hud-muted'));
      const label=node('label','维护 AI'),select=node('select');select.setAttribute('aria-label','维护 AI');select.append(new Option('请选择',''));
      bots.forEach((bot,i)=>{select.add(new Option(`${bot.display_name||bot.profile} · ${bot.host_type}`,String(i)));if(bot.bot_id===current?.bot_id)select.value=String(i);});label.append(select);
      const checkLabel=node('label'),check=node('input');check.type='checkbox';check.checked=!current?.bot_id;checkLabel.append(check,document.createTextNode(' 下次预选这个 AI（各项目分别启用维护）'));
      const timeLabel=node('label','空闲多久后重置模型上下文'),timeSelect=node('select');timeSelect.setAttribute('aria-label','空闲重置时间');
      [[300,'5 分钟'],[1800,'30 分钟'],[3600,'1 小时'],[86400,'24 小时']].forEach(([v,t])=>timeSelect.add(new Option(t,String(v))));timeSelect.value=String(current?.idle_seconds||1800);timeLabel.append(timeSelect);
      box.append(label,checkLabel,timeLabel,node('p','保存后启用当前项目维护，将项目配置、模板和项目内文档提供给所选 AI。','hud-muted'),action('保存并开始维护',async()=>{
        if(!select.value)return;let bot=bots[Number(select.value)];
        try{
          if(!bot.bot_id){const registered=await api('/bots/register',{method:'POST',body:JSON.stringify({host_type:bot.host_type,profile:bot.profile,config_root:bot.config_root})});bot={...bot,bot_id:registered.bot_id};}
          const result=await mutate('',{bot_id:bot.bot_id,make_default:check.checked,idle_seconds:Number(timeSelect.value)},'PATCH');
          if(result){settings=false;box.hidden=true;}
        }catch(e){errorText=e.message;render();}
      },'hud-primary'),action('创建 AI 配置',()=>openProfileCreate({onCreated:()=>showSettings(true)})),action('刷新列表',()=>showSettings(true)));
    }catch(e){box.replaceChildren(node('p',e.message,'hud-error-text'),action('重试',()=>showSettings(true)));}
  }
  async function attach(project, id=null){
    const stamp=++version;remember();scope=project||'';errorText='';current=null;selected='';el('input').value='';render();
    try{
      if(!project&&!id&&!draftMode){const index=await api('/hud');id=index.sessions.find(s=>!s.project)?.id;}
      const data=id?await api('/hud/'+id):await api('/hud',{method:'POST',body:JSON.stringify({project:project||null})});
      if(stamp!==version)return null;setData(data);if(!project)draftId=data.id;return data;
    }catch(e){if(stamp===version){errorText=e.message;render();}return null;}
  }
  async function refresh(){
    if(document.hidden||busy||host.hidden||refreshing)return;
    const detail=document.querySelector('#dlg-project-detail');
    const project=detail?.open?document.querySelector('#pj-detail-form')?.dataset.origName:S.currentProject;
    if(!draftMode&&(project||'')!==scope){await attach(project);return;}
    if(!current)return;
    const sid=current.id,stamp=version;
    refreshing=true;
    try{
      const [data,index]=await Promise.all([api('/hud/'+sid),api('/hud')]);
      if(stamp!==version)return;
      if(data.revision>=current.revision&&(data.revision!==current.revision||data.status!==current.status))setData(data);
      const notices=el('notices');notices.replaceChildren();
      const others=index.sessions.filter(x=>x.id!==sid&&(x.question||!x.project&&x.title!=='创建项目草稿'));
      const pending=others.filter(x=>x.question);
      host.querySelector('.hud-caption').textContent=host.querySelector('.hud-caption').textContent.replace(/ · 另有 \d+ 项待确认$/,'');
      if(pending.length)host.querySelector('.hud-caption').textContent+=` · 另有 ${pending.length} 项待确认`;
      for(const other of others.slice(0,5)){
        const project=S.projects?.find(p=>p.name===other.project);
        notices.append(action(other.project?`${project?.display_name||other.project} · 待确认`:`继续：${other.title}`,()=>{
          if(other.project&&!draftMode)selectProject(other.project);
          else if(!other.project){window.openProjectImport({mode:other.mode||'new',hudDraftId:other.id});}
        }));
        if(other.question&&!seenOthers.has(other.question.id)){seenOthers.add(other.question.id);if(!el('input').value&&!draftMode)toggle(true);}
      }
    }catch(e){if(stamp===version){errorText='维护连接暂时中断，输入已保留。'+e.message;render();}}
    finally{refreshing=false;}
  }
  window.ProjectHUD={
    async begin(dialog,apply,id,flush){draftMode=true;onApply=apply;beforeConfirm=flush;dialog.append(host);const data=await attach(null,id||null);toggle(true);return data;},
    saveDraft(draft,stage){
      const id=draftId;
      chain=chain.catch(()=>{}).then(async()=>{if(!draftMode||current?.id!==id)return;while(busy)await new Promise(r=>setTimeout(r,100));await mutate('',{draft,stage},'PATCH');});return chain;
    },
    async bind(requestId){await chain;return await mutate('/bind',{request_id:requestId});},
    end(){chain.finally(()=>{draftMode=false;onApply=null;beforeConfirm=null;document.body.append(host);attach(S.currentProject);});},
    changed(){refresh();},
  };
  setInterval(refresh,3000);
  document.querySelector('#dlg-project-detail')?.addEventListener('close',()=>refresh());
  const authChanged=()=>{
    host.hidden=!document.querySelector('#login')?.hidden;
    if(host.hidden){++version;current=null;el('input').value='';inputs.clear();selections.clear();}
    else if(!current)attach(typeof S!=='undefined'?S.currentProject:null);
  };
  document.addEventListener('service-auth-changed',authChanged);
  authChanged();
})();
