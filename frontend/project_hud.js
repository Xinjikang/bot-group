// Project-scoped maintenance UI. Group messages and task conversations stay independent.
(() => {
  const host = document.createElement('section');
  host.id = 'project-hud'; host.setAttribute('aria-label', '项目助手');
  host.innerHTML = `<div class="hud-shell">
    <button class="hud-toggle" id="hud-toggle" aria-expanded="false" aria-controls="hud-panel"><span class="hud-light" aria-hidden="true"></span><span class="hud-caption"><span class="hud-name">项目助手</span><span class="hud-status"> · 准备就绪</span></span><span class="hud-chevron">展开</span></button>
    <div class="hud-panel" id="hud-panel" hidden>
      <div class="hud-context"><span id="hud-context"></span><nav aria-label="维护工具"><button class="hud-link hud-more" id="hud-more" aria-label="更多维护工具" aria-expanded="false">···</button></nav></div>
      <nav class="hud-tools" id="hud-tools" aria-label="更多维护工具" hidden><button class="hud-link" id="hud-history-toggle">项目备注</button><button class="hud-link" id="hud-settings-toggle">设置</button></nav>
      <button class="hud-link hud-back" id="hud-back" hidden>← 返回助手</button>
      <div class="hud-tabs" id="hud-tabs" role="tablist" aria-label="助手内容">
        <button id="hud-chat-tab" role="tab" aria-controls="hud-content" aria-selected="true">对话消息</button>
        <button id="hud-todos-tab" role="tab" aria-controls="hud-todos" aria-selected="false" tabindex="-1">待办事项</button>
      </div>
      <div class="hud-progress" id="hud-progress" role="status" hidden><span id="hud-progress-text"></span><time id="hud-progress-time"></time></div>
      <div class="hud-scroll">
        <div id="hud-settings" class="hud-settings" hidden></div>
        <div id="hud-history" class="hud-history" hidden></div>
        <div id="hud-content" role="tabpanel" aria-labelledby="hud-chat-tab" aria-live="polite"></div>
        <div id="hud-todos" role="tabpanel" aria-labelledby="hud-todos-tab" hidden></div>
        <p id="hud-error" class="hud-error-text" role="alert" hidden></p>
        <button class="hud-link" id="hud-retry" hidden>重新检查</button>
      </div>
      <button class="hud-link" id="hud-latest" hidden>查看最新回复 ↓</button>
      <form class="hud-compose" id="hud-compose">
        <label class="hud-input-label" for="hud-input">发消息给维护助手</label>
        <textarea id="hud-input" maxlength="12000" placeholder="输入回答，或补充说明…"></textarea>
        <div class="hud-actions"><span class="hud-hint" id="hud-hint">Ctrl + Enter 发送</span><button class="hud-primary" id="hud-send" type="submit">发送</button></div>
      </form>
    </div></div>`;
  document.body.append(host);
  const el = id => host.querySelector('#hud-' + id);
  let current = null, scope = '', version = 0, expanded = false, settings = false, busy = false, selected = '', refreshing = false;
  let draftMode = false, draftId = '', onApply = null, beforeConfirm = null, errorText = '', chain = Promise.resolve();
  let auxiliary = '', renderedKey = '', followNext = false;
  let activeTab='chat';
  const inputs = new Map(), silenced = new Set();
  const appliedConfigUpdates = new Map();
  const notificationKey = item => item?.state.notification?.id||item?.state.question?.id||item?.state.proposal?.id;
  function isSilenced(key) {
    if(!key)return false;
    try {return silenced.has(key)||sessionStorage.getItem('hud-silenced:'+key)==='1';}catch{return silenced.has(key);}
  }
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
    if (!value && notificationKey(current)) {
      const key=notificationKey(current);silenced.add(key);
      try {sessionStorage.setItem('hud-silenced:'+key,'1');}catch{}
    }
    placeBesideDialog();
  }
  function placeBesideDialog() {
    const dialog=host.parentElement===document.body
      ?document.querySelector('#dlg-project-detail'):host.closest('dialog');
    const rect=dialog?.open?dialog.getBoundingClientRect():null;
    const available=rect?innerWidth-rect.right-16:0;
    const docked=!!(expanded&&rect&&available>=300);
    host.classList.toggle('hud-docked',docked);
    host.classList.toggle('hud-compact',docked&&rect.height<420);
    if(docked){
      host.style.setProperty('--hud-left',`${rect.right+8}px`);
      host.style.setProperty('--hud-top',`${rect.top}px`);
      host.style.setProperty('--hud-width',`${Math.min(510,available)}px`);
      host.style.setProperty('--hud-height',`${rect.height}px`);
    }
  }
  el('toggle').onclick=()=>toggle(!expanded);
  el('input').addEventListener('input',()=>{remember(); updateSend();});
  el('input').addEventListener('keydown',e=>{if(e.key==='Enter'&&(e.ctrlKey||e.metaKey)&&!e.isComposing){e.preventDefault();el('compose').requestSubmit();}});
  host.addEventListener('keydown',e=>{if(e.key==='Escape'){e.preventDefault();e.stopPropagation();toggle(false);el('toggle').focus();}});
  function updateSend() {
    el('send').disabled=busy||!current?.bot||current?.status==='running'||!(selected||el('input').value.trim());
    host.querySelectorAll('#hud-todos button').forEach(button=>button.disabled=busy||!current?.bot||['running','queued'].includes(current?.status));
  }
  function setData(data) {
    const changed=current?.id!==data.id;
    if(changed) remember();
    current=data;
    const update=data.state.config_update;
    if(update&&appliedConfigUpdates.get(data.id)!==update.id){
      appliedConfigUpdates.set(data.id,update.id);
      if(draftMode&&update.draft)onApply?.(update.changes,update.before);
      else if(document.querySelector('#pj-detail-form')?.dataset.origName===data.project){
        const ids={display_name:'name',description:'desc',root:'root',directory_mode:'mode',worktree_root:'worktree'};
        for(const [key,value] of Object.entries(update.changes)){
          const input=document.querySelector('#pj-d-'+ids[key]);
          if(input&&input.value===(update.before[key]??'')){
            input.value=value;
            if(key==='directory_mode'){
              document.querySelectorAll('[name="project-directory-mode"]').forEach(radio=>radio.checked=radio.value===value);
              document.querySelector('#pj-worktree-options').hidden=value!=='worktree';
            }
          }
        }
      }
    }
    if(changed){
      let saved=''; try {saved=sessionStorage.getItem('hud-input:'+data.id)||'';}catch{}
      el('input').value=inputs.get(data.id)??saved;
    }
    render();
  }
  function renderProgress(){
    const active=current?.status==='running'||current?.status==='queued';
    el('progress').hidden=!active;
    if(!active)return;
    const progress=current.state.progress;
    const text=progress?.text||(current.status==='queued'?'等待开始检查':'正在等待模型响应');
    const line=el('progress-text');
    const label=(progress?.kind==='reasoning'?'推理摘要：':'')+text;
    if(line.textContent!==label){
      line.textContent=label;
      requestAnimationFrame(()=>{line.scrollLeft=line.scrollWidth;});
    }
    line.title=label;
    const elapsed=Math.max(0,Math.floor(Date.now()/1000-(progress?.started||current.checked||current.updated)));
    el('progress-time').textContent=elapsed+'秒';
    host.querySelector('.hud-status').textContent=' · '+text;
  }
  function render() {
    const item=current, q=item?.state.question, proposal=item?.state.proposal;
    const state=item?.status;
    const notification=item?.state.notification;
    const cards=item?.state.cards||[];
    const actionable=notification?.kind==='user_action';
    const pending=actionable||!!q||!!proposal||cards.some(card=>card.question||card.proposal);
    const entries=item?.entries||[];
    const userIndex=entries.findLastIndex(e=>e.role==='user');
    const lastUser=userIndex>=0?entries[userIndex]:null;
    const lastReply=entries.slice(userIndex+1).filter(e=>e.role==='assistant').at(-1);
    const scene=activeTab==='todos'?'cards':'chat';
    const changedScene=host.dataset.scene!==scene;
    host.dataset.scene=scene;
    el('input').placeholder='输入你的想法…';
    const label=!item?.bot?'选择 AI 开始维护':state==='paused'?'点击开始维护':state==='running'||state==='queued'?'正在检查项目':state==='error'?'检查未完成':actionable?'有事项需要你行动':pending?'有待处理事项':'随时为你维护项目';
    host.querySelector('.hud-name').textContent=item?.bot?.display_name||'项目助手';
    host.querySelector('.hud-status').textContent=' · '+label;
    host.classList.toggle('hud-attention',pending);host.classList.toggle('hud-running',state==='running'||state==='queued');host.classList.toggle('hud-error',state==='error');
    renderProgress();
    const project=typeof S!=='undefined'?S.projects?.find(p=>p.name===item?.project):null;
    el('context').textContent=(project?.display_name||item?.state.draft?.display_name|| (item?.project?'当前项目':'创建项目'))+' · '+(stages[item?.stage]||'项目维护');
    const scroll=host.querySelector('.hud-scroll'),oldTop=scroll.scrollTop;
    const nearBottom=scroll.scrollHeight-scroll.clientHeight-oldTop<70;
    const key=`${item?.id}/${item?.entries.at(-1)?.id}/${state}/${scene}`;
    const newTurn=key!==renderedKey,firstView=!renderedKey||!renderedKey.startsWith(item?.id+'/');
    const content=el('content'); content.replaceChildren();
    const todos=el('todos');todos.replaceChildren();
    for(const entry of [lastUser,lastReply].filter(Boolean)){
      const message=node('article',null,`hud-message hud-message-${entry.role}`);
      message.dataset.entry=entry.id;
      if(entry.role==='system')message.append(node('p',entry.content.text,'hud-system-note'));
      else {
        message.append(node('small',entry.role==='user'?'你':item.bot?.display_name||'助手','hud-speaker'),node('p',entry.content.text,'hud-message-text'));
        if(entry.role==='assistant'){
          const oldQuestion=entry.content.question;
          if(oldQuestion&&oldQuestion.id!==q?.id)message.append(node('p',oldQuestion.title,'hud-past-question'));
          if(entry.content.guide)message.append(guideContent(entry.content.guide));
        }
      }
      content.append(message);
    }
    if(!item?.bot){
      content.append(node('h3','为 HUD 选择一个 AI'),node('p','可以使用项目负责人，也可以选择其他 Bot。它会在这里帮你完善项目资料和配置。','hud-muted'));
      content.append(action('选择 AI',()=>showSettings(true),'hud-primary'));
      if(!item)content.append(action('重试连接',()=>attach(scope||null,draftMode?draftId:null)));
    }else if(state==='paused'){
      content.append(node('h3','开始维护这个项目'),node('p','启用后，所选 AI 将使用项目配置、模板和项目内文档帮助你完善配置，并在发生变化时主动检查。','hud-muted'),action('开始维护',()=>mutate('/retry'),'hud-primary'));
    }else{
      if(state!=='queued'&&state!=='running'&&!item.entries.length)content.append(node('p','告诉我你想完善什么，我们一起把项目准备好。','hud-muted'));
    }
    const questions=[...(q?[{question:q}]:[]),...cards.filter(card=>card.question&&card.question.id!==q?.id)];
    for(const card of questions){
      const question=card.question;
      const box=node('section',null,'hud-question hud-todo-card');box.dataset.card=question.id;
      box.append(node('p',card.text,'hud-text'),node('h3',question.title));
      const why=node('details',null,'hud-reason');why.append(node('summary','为什么问这个？'),node('p',question.reason));box.append(why);
      const input=node('textarea',null,'hud-card-answer');input.maxLength=12000;input.placeholder='填写这项待办的回答…';input.setAttribute('aria-label',question.title);
      input.value=inputs.get(question.id)||'';input.addEventListener('input',()=>inputs.set(question.id,input.value));
      const options=node('div',null,'hud-options');
      const send=async text=>{
        if(!text.trim())return;
        const data=await mutate('/answer',{text,question_id:question.id});
        if(data){inputs.delete(question.id);render();}
        else {
          inputs.set(question.id,text);
          const field=[...todos.querySelectorAll('[data-card]')].find(e=>e.dataset.card===question.id)?.querySelector('textarea');
          if(field)field.value=text;
        }
      };
      for(const option of question.options||[])options.append(action(option,()=>send([option,input.value.trim()].filter(Boolean).join('\n补充：')),'hud-option'));
      const submit=action('发送这项回答',()=>send(input.value),'hud-primary');submit.disabled=busy||state==='running'||state==='queued';
      box.append(options,input,submit);todos.append(box);
    }
    const proposals=[...(proposal?[{proposal}]:[]),...cards.filter(card=>card.proposal&&card.proposal.id!==proposal?.id)];
    for(const card of proposals){
      const proposal=card.proposal;
      const changes=Object.entries(proposal.changes),facts=proposal.facts,documents=proposal.documents||[];
      const box=node('section',null,'hud-proposal hud-todo-card');box.dataset.card=proposal.id;
      if(card.text)box.append(node('p',card.text,'hud-text'));
      box.append(node('strong','以下建议尚未保存'));
      if(proposal.reason)box.append(node('p',proposal.reason,'hud-text'));
      const display=(key,value)=>key==='directory_mode'?({shared:'共用项目目录',worktree:'每位成员使用独立工作区'}[value]||value):value;
      const compare=(title,before,after,empty)=>{
        const group=node('section',null,'hud-change');group.append(node('h4',title));
        const dl=node('dl');
        dl.append(node('dt','当前内容'),node('dd',before===undefined?'旧建议未记录当前值，请重新检查':before||empty),
          node('dt','建议改为'),node('dd',after));group.append(dl);return group;
      };
      if(changes.length){
        box.append(node('p',item.project?'修改位置：项目设置。确认后更新以下字段。':'修改位置：创建向导。确认后更新草稿，提交向导时创建项目。','hud-muted'));
        for(const [key,value] of changes){
          const before=proposal.before?.[key]??proposal.base_project?.[key]??item.state.draft?.[key];
          box.append(compare(fieldNames[key]||key,display(key,before),display(key,value),'尚未填写'));
        }
      }
      if(facts.length){
        box.append(node('p','保存位置：项目备注。本项目助手和成员 Bot 后续会参考这些约定，项目文件保持原样。','hud-muted'));
        for(const fact of facts){
          const before=proposal.before_facts?.[fact.label]??item.state.facts?.find(f=>f.label===fact.label)?.value??'';
          box.append(compare(fact.label,before,fact.value,'尚未保存这条备注'));
        }
      }
      if(documents.length){
        box.append(node('p','旧版文档建议保留以下文件记录。后续修改可直接告诉助手，完成后在对话页查看结果。','hud-muted'));
        for(const document of documents){
          box.append(node('p',document.path,'hud-muted'));
        }
      }
      const count=changes.length+facts.length+documents.length;
      box.append(action(documents.length?`确认保存以上 ${count} 项`:changes.length&&facts.length?`确认保存以上 ${count} 项`:changes.length?(item.project?'确认修改项目设置':'确认更新向导草稿'):'确认保存项目备注',async()=>{
        if(draftMode)await beforeConfirm?.();
        const data=await mutate('/accept',{proposal_id:proposal.id});if(data&&draftMode&&Object.keys(proposal.changes).length)onApply?.(data.state.draft);
        if(data&&item.project){
          const form=document.querySelector('#pj-detail-form');
          if(form?.dataset.origName===item.project){
            const ids={display_name:'name',description:'desc',root:'root',directory_mode:'mode',worktree_root:'worktree'};
            for(const [key,value] of changes){
              const input=document.querySelector('#pj-d-'+ids[key]);
              if(input&&input.value===(proposal.before?.[key]??'')){
                input.value=value;
                if(key==='directory_mode'){
                  document.querySelectorAll('[name="project-directory-mode"]').forEach(radio=>radio.checked=radio.value===value);
                  document.querySelector('#pj-worktree-options').hidden=value!=='worktree';
                }
              }
            }
          }
          if(changes.length)await loadProjects();
        }
      },'hud-primary'),action('暂不处理',()=>mutate('/answer',{text:'这项建议暂不处理，请继续当前问题。',question_id:null,dismiss_proposal:proposal.id})));todos.append(box);
    }
    const reminder=actionable?notification.action:'';
    if(reminder){
      const box=node('section',null,'hud-todo-card hud-reminder');
      box.append(node('strong',actionable?'需要你行动':'待处理提醒'),node('p',reminder,'hud-action-required hud-text'));todos.append(box);
    }
    const count=todos.children.length;
    el('todos-tab').textContent=count?`待办事项 (${count})`:'待办事项';
    if(!count)todos.append(node('p','当前项目暂无待办事项。','hud-muted'));
    syncTabs();
    el('error').textContent=errorText||item?.error||'';el('error').hidden=!el('error').textContent;
    el('retry').hidden=!item?.bot||state!=='error';
    el('hint').textContent=busy?'正在发送…':state==='running'?'正在回复，输入会为你保留':'Ctrl + Enter 发送';
    el('input').disabled=!item?.bot;
    const history=el('history');history.replaceChildren();
    history.append(node('h3','项目备注'),node('p','已保存的约定供本项目助手和成员 Bot 参考。','hud-muted'));
    for(const fact of item?.state.facts||[]){const p=node('article');p.append(node('strong',fact.label),node('p',fact.value,'hud-text'));history.append(p);}
    if(!item?.state.facts?.length)history.append(node('p','目前还没有保存项目备注。','hud-muted'));
    updateSend();
    if(actionable&&state!=='running'&&state!=='queued'&&!isSilenced(notificationKey(item)))toggle(true);
    renderedKey=key;
    const follow=followNext||firstView||nearBottom||changedScene;followNext=false;
    if(newTurn)requestAnimationFrame(()=>{
      if(renderedKey!==key)return;
      if(activeTab==='chat'&&follow&&!auxiliary)showLatest();
      else {scroll.scrollTop=oldTop;if(!auxiliary&&activeTab==='chat')el('latest').hidden=false;}
    });
    else scroll.scrollTop=oldTop;
  }
  function guideContent(guide){
    const box=node('section',null,'hud-guide');box.append(node('h3',guide.title));
    const list=node('ol');
    for(const step of guide.steps){const li=node('li');li.append(node('h4',step.title),node('p',step.body));list.append(li);}
    box.append(list);return box;
  }
  function showLatest(){
    const messages=[...el('content').querySelectorAll('.hud-message')];
    const target=host.dataset.scene==='chat'?(messages.filter(m=>m.classList.contains('hud-message-user')).at(-1)||messages.at(-1)):null;
    const scroll=host.querySelector('.hud-scroll');
    if(target)scroll.scrollTop+=target.getBoundingClientRect().top-scroll.getBoundingClientRect().top-12;
    else scroll.scrollTop=0;
    el('latest').hidden=true;
  }
  function showAuxiliary(name){
    el('tools').hidden=true;el('more').setAttribute('aria-expanded','false');
    auxiliary=name;settings=name==='settings';
    for(const panel of ['settings','history'])el(panel).hidden=panel!==name;
    syncTabs();el('compose').hidden=!!name;el('back').hidden=!name||settings;el('latest').hidden=true;
    for(const [id,panel] of [['settings','settings'],['history','history']])el(id+'-toggle').setAttribute('aria-pressed',String(panel===name));
    host.querySelector('.hud-scroll').scrollTop=0;
    if(!name)requestAnimationFrame(showLatest);
  }
  el('back').onclick=()=>showAuxiliary('');
  el('more').onclick=()=>{el('tools').hidden=!el('tools').hidden;el('more').setAttribute('aria-expanded',String(!el('tools').hidden));};
  function syncTabs(){
    el('tabs').hidden=!!auxiliary;
    for(const name of ['chat','todos']){
      const active=activeTab===name,tab=el(name+'-tab');
      tab.setAttribute('aria-selected',String(active));tab.tabIndex=active?0:-1;
      el(name==='chat'?'content':'todos').hidden=!!auxiliary||!active;
    }
  }
  function selectTab(name){
    activeTab=name;showAuxiliary('');render();host.querySelector('.hud-scroll').scrollTop=0;
  }
  for(const name of ['chat','todos'])el(name+'-tab').onclick=()=>selectTab(name);
  el('tabs').addEventListener('keydown',event=>{
    if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
    event.preventDefault();
    const name=event.key==='Home'?'chat':event.key==='End'?'todos':activeTab==='chat'?'todos':'chat';
    selectTab(name);el(name+'-tab').focus();
  });
  el('latest').onclick=showLatest;
  el('history-toggle').onclick=()=>showAuxiliary(auxiliary==='history'?'':'history');
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
    followNext=true;
    const data=await mutate('/answer',{text,question_id:q?.id||null});
    if(current?.id===sid){
      el('input').value=data?'':text;selected='';remember();render();
      if(data)el('input').focus();
    }
  });
  el('retry').onclick=()=>mutate('/retry');
  el('settings-toggle').onclick=()=>showSettings(!settings);
  async function showSettings(value){
    showAuxiliary(value?'settings':'');if(!value)return;
    const box=el('settings');box.replaceChildren(node('p','正在读取可用 AI…','hud-muted'));
    try{
      const bots=await MemberPicker.load();if(!settings)return;
      box.replaceChildren(node('p','HUD 独立选择 Bot；允许与项目负责人使用同一个。','hud-muted'));
      const label=node('label','维护 AI'),select=node('select');select.setAttribute('aria-label','维护 AI');select.append(new Option('请选择',''));
      bots.forEach((bot,i)=>{select.add(new Option(`${bot.display_name||bot.profile} · ${bot.host_type}`,String(i)));if(bot.bot_id===current?.bot_id)select.value=String(i);});label.append(select);
      const botTools=node('div',null,'hud-settings-tools');
      botTools.append(action('新建 profile',()=>openProfileCreate({onCreated:()=>showSettings(true)})),action('刷新可选 Bot',()=>showSettings(true)));
      const botField=node('div',null,'hud-settings-bot');botField.append(label,botTools);
      const checkLabel=node('label'),check=node('input');check.type='checkbox';check.checked=!current?.bot_id;checkLabel.append(check,node('span','下次预选这个 AI（各项目分别启用维护）'));
      const timeLabel=node('label','空闲多久后重置模型上下文'),timeSelect=node('select');timeSelect.setAttribute('aria-label','空闲重置时间');
      [[300,'5 分钟'],[1800,'30 分钟'],[3600,'1 小时'],[86400,'24 小时']].forEach(([v,t])=>timeSelect.add(new Option(t,String(v))));timeSelect.value=String(current?.idle_seconds||1800);timeLabel.append(timeSelect);
      const botCard=node('section',null,'hud-settings-card');botCard.setAttribute('aria-label','维护 AI 配置');botCard.append(botField,checkLabel);
      const sessionCard=node('section',null,'hud-settings-card');sessionCard.setAttribute('aria-label','上下文重置配置');
      sessionCard.append(timeLabel,node('p','保存后启用当前项目维护，将项目配置、模板和项目内文档提供给所选 AI。','hud-muted'));
      const saveButtons=node('div',null,'hud-settings-buttons');
      saveButtons.append(action('取消',()=>showAuxiliary(''),'hud-cancel'),action('保存并开始维护',async()=>{
        if(!select.value)return;let bot=bots[Number(select.value)];
        try{
          if(!bot.bot_id){const registered=await api('/bots/register',{method:'POST',body:JSON.stringify({host_type:bot.host_type,profile:bot.profile,config_root:bot.config_root})});bot={...bot,bot_id:registered.bot_id};}
          const result=await mutate('',{bot_id:bot.bot_id,make_default:check.checked,idle_seconds:Number(timeSelect.value)},'PATCH');
          if(result)showAuxiliary('');
        }catch(e){errorText=e.message;render();}
      },'hud-primary'));
      box.append(botCard,sessionCard,saveButtons);
    }catch(e){box.replaceChildren(node('p',e.message,'hud-error-text'),action('重试',()=>showSettings(true)));}
  }
  async function attach(project, id=null){
    showAuxiliary('');
    activeTab='chat';
    const stamp=++version;remember();scope=project||'';errorText='';current=null;selected='';el('input').value='';render();
    try{
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
      const data=await api('/hud/'+sid);
      if(stamp!==version)return;
      if(data.revision>=current.revision&&(data.revision!==current.revision||data.status!==current.status||data.state.config_update?.id!==current.state.config_update?.id))setData(data);
      else if(data.revision===current.revision){current.state.progress=data.state.progress;renderProgress();}
    }catch(e){if(stamp===version){errorText='维护连接暂时中断，输入已保留。'+e.message;render();}}
    finally{refreshing=false;}
  }
  window.ProjectHUD={
    async begin(dialog,apply,id,flush){draftMode=true;onApply=apply;beforeConfirm=flush;dialog.append(host);placeBesideDialog();const data=await attach(null,id||null);toggle(true);return data;},
    saveDraft(draft,stage){
      const id=draftId;
      chain=chain.catch(()=>{}).then(async()=>{if(!draftMode||current?.id!==id)return;while(busy)await new Promise(r=>setTimeout(r,100));await mutate('',{draft,stage},'PATCH');});return chain;
    },
    async bind(requestId){await chain;return await mutate('/bind',{request_id:requestId});},
    end(){chain.finally(()=>{draftMode=false;onApply=null;beforeConfirm=null;document.body.append(host);placeBesideDialog();attach(S.currentProject);});},
    changed(){refresh();},
  };
  setInterval(refresh,3000);
  setInterval(renderProgress,1000);
  for(const dialog of document.querySelectorAll('#dlg-project-detail,#dlg-project-import')){
    new ResizeObserver(placeBesideDialog).observe(dialog);
    new MutationObserver(placeBesideDialog).observe(dialog,{attributes:true,attributeFilter:['open','style']});
  }
  window.addEventListener('resize',placeBesideDialog);
  document.querySelector('#dlg-project-detail')?.addEventListener('close',()=>refresh());
  const authChanged=()=>{
    host.hidden=!document.querySelector('#login')?.hidden;
    if(host.hidden){++version;current=null;el('input').value='';inputs.clear();}
    else if(!current)attach(typeof S!=='undefined'?S.currentProject:null);
  };
  document.addEventListener('service-auth-changed',authChanged);
  authChanged();
})();
