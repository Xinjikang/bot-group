/* Execute the shipped HUD synchronizer and wizard applyHud against API snapshots. */
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const root=path.resolve(__dirname,'..');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
function section(file,start,end){
 const source=fs.readFileSync(path.join(root,file),'utf8');
 const a=source.indexOf(start),b=source.indexOf(end,a+start.length);
 if(a<0||b<0)throw new Error('Frontend function boundary missing: '+file);
 return source.slice(a,b);
}
const sync=section('frontend/project_hud.js','  function applyConfigUpdates(data) {','  function setData(data) {');
const apply=section('frontend/project_import.js','  function applyHud(draft, before) {',"  dialog.addEventListener('input'");
const context=vm.createContext({input,Map,JSON});
vm.runInContext(`
 let roles=input.initial?.roles||[], members=new Map((input.initial?.members||[]).map(m=>[m.bot_id,m]));
 let preview=null,payload=null,hudRestored=null,nameEdited=false,templateExplicit=false;
 const dialog={open:true},draftMode=input.draft!==false,appliedConfigUpdates=new Map();
 let applied=0;
 const fields={};
 for(const key of ['display_name','description','root','template','directory_mode','worktree_root'])fields[key]={value:input.initial?.basic?.[key]||''};
 const ids={name:'display_name',description:'description',root:'root',template:'template',mode:'directory_mode',worktree:'worktree_root',desc:'description'};
 const el=id=>fields[ids[id]]||{value:''};
 const document={querySelector:selector=>selector==='#pj-detail-form'?{dataset:{origName:input.project||'p'}}:selector.startsWith('#pj-d-')?el(selector.slice(6)):{hidden:false},querySelectorAll:()=>[]};
 const invalidateRules=()=>{},updateModeHelp=()=>{},renderRoles=()=>{},renderMembers=()=>{},showStep=()=>{},status=()=>{};
 ${apply}
 const onApply=(draft,before)=>{applied++;applyHud(draft,before)};
 ${sync}
 for(const [index,data] of input.snapshots.entries()){
   applyConfigUpdates(data);
   for(const edit of input.edits||[])if(edit.after===index){
     if(edit.roles)roles=edit.roles;
     for(const [key,value] of Object.entries(edit.basic||{}))fields[key].value=value;
   }
 }
 result=JSON.stringify({roles,members:[...members.values()],basic:Object.fromEntries(Object.entries(fields).map(([k,v])=>[k,v.value])),applied});
`,context);
process.stdout.write(context.result);
