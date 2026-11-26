import cytoscape from './vendor/cytoscape-3.30.4.esm.min.js';
// dagre (layered graph layout) is loaded by index.html as a classic script and sets window.dagre.
const $=s=>document.querySelector(s);
const el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;};
let workflows=[],presets=[],assets=[],selected=null,metadata=null,favorites=false,offset=0,total=0,lastSignature='',busy=false;
let sourceAsset=null,endAsset=null,toolCapabilities={};
let activeJobs=[],latestId='';
// The queued job the Generate form is editing, if any; the user's own draft waits in stashedDraft meanwhile.
let editingJob=null,editLocked=false,stashedDraft=null;
const profiles=['fast','balanced','quality','maximum'];
const titles={gallery:'Your image collection',live:'Watch it take shape',generate:'Give an idea its shape',branches:'Shots that keep going',history:'Every step, remembered',settings:'Make yourself at home'};
async function api(path,method='GET',body){const options={method,headers:{'X-PixAnvil':'local'}};if(body instanceof FormData)options.body=body;else if(body!==undefined){options.headers['Content-Type']='application/json';options.body=JSON.stringify(body);}const r=await fetch('/api'+path,options);const data=await r.json();if(!r.ok)throw new Error(typeof data.detail==='string'?data.detail:JSON.stringify(data.detail));return data;}
function notice(message){$('#notice').hidden=!message;$('#notice').textContent=message;}
function run(fn){return async(...args)=>{try{await fn(...args);}catch(e){notice(e.message);}};}
function tab(name){if(!titles[name])name='gallery';
  // The newest clip on Activity plays only while that page is showing.
  if(name!=='live')$('#latest-preview video')?.pause();
  if(name!=='branches')stopBranchVideo();
  document.querySelectorAll('.page').forEach(p=>p.hidden=p.id!==name);document.querySelectorAll('.nav').forEach(b=>b.classList.toggle('active',b.dataset.tab===name));$('#section-label').textContent=name.toUpperCase();$('#page-title').replaceChildren(document.createTextNode(titles[name]),el('span','orange','.'));$('#new-image').hidden=name==='generate';location.hash=name;
  if(name==='branches')run(showBranches)();}
document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>tab(b.dataset.tab));
$('#new-image').onclick=$('#first-image').onclick=()=>tab('generate');
window.addEventListener('hashchange',()=>{const name=location.hash.slice(1);if(document.getElementById(name)?.hidden!==false)tab(name);});
const imageCanvases=[{width:1024,height:1024,label:'Square · 1024 × 1024'},{width:832,height:1216,label:'Portrait · 832 × 1216'},{width:1216,height:832,label:'Landscape · 1216 × 832'},{width:768,height:768,label:'Draft · 768 × 768'},{width:512,height:512,label:'Small · 512 × 512'}];
let optionsWorkflow='';
const isVideo=a=>a.recipe.media==='video';
const clipSeconds=r=>Math.round(r.frames/r.fps);
// The "What to make" menu holds a mode. Each mode lists only the workflows that can run it, and the mode
// never changes unless the user changes it. The API operation is derived from the mode and the attached images.
const mode=()=>$('#operation').value;
function videoMode(){return mode()==='video';}
function apiOperation(){const m=mode();return m==='image'?'txt2img':m==='video'?(sourceAsset||endAsset?'img2vid':'txt2img'):m;}
function modeOf(r){const op=r.operation||'txt2img';if(op==='img2vid'||workflows.find(w=>w.id===r.workflow)?.media==='video')return 'video';return ['txt2img','import','frame'].includes(op)?'image':op;}
function fits(w,m){return m==='video'?w.media==='video':m==='edit'?!!w.source_image:w.media!=='video'&&!w.source_image&&!(m==='refine'&&w.id==='flux-schnell');}
function setMode(m,workflow){
  $('#operation').querySelector('[value=variation]').disabled=false;$('#operation').value=m;
  const list=workflows.filter(w=>fits(w,m)),current=workflow||$('#workflow').value;
  $('#workflow').replaceChildren(...list.map(w=>{const o=el('option','',w.name+(w.available?'':' — unavailable'));o.value=w.id;return o;}));
  const chosen=list.find(w=>w.id===current)||list.find(w=>w.available)||list[0];if(chosen)$('#workflow').value=chosen.id;
  if(m==='image')sourceAsset=null;if(m!=='video')endAsset=null;
}
function fitRefineCanvas(asset){const scale=Math.min(1,1536/Math.max(asset.width,asset.height));setDimensions(Math.max(256,Math.round(asset.width*scale/64)*64),Math.max(256,Math.round(asset.height*scale/64)*64));}
// Video workflow packages list their own canvases and clip lengths; image workflows share one canvas list.
function setWorkflowOptions(w){
  const key=w?.canvases?w.id:'image';if(key===optionsWorkflow)return;optionsWorkflow=key;
  $('#dimensions').replaceChildren(...(w?.canvases||imageCanvases).map(c=>{const o=el('option','',c.label);o.value=`${c.width}x${c.height}`;return o;}));
  if(w?.defaults?.width)$('#dimensions').value=`${w.defaults.width}x${w.defaults.height}`;
  setLengths(w);
}
function data(){
  const operation=apiOperation(),upscale=operation==='upscale',video=videoMode();
  const [width,height]=upscale?[1024,1024]:$('#dimensions').value==='auto'?[null,null]:$('#dimensions').value.split('x').map(Number);
  return {prompt:upscale?$('#prompt').value||'Upscale source image':$('#prompt').value,negative:upscale?'':$('#negative').value,workflow:$('#workflow').value||'flux-dev',model:$('#model').value,quality:profiles[Number($('#quality').value)],seed:$('#seed').value,width,height,count:upscale?1:Number($('#count').value),steps:upscale?null:$('#steps').value?Number($('#steps').value):null,cfg:upscale?null:$('#cfg').value?Number($('#cfg').value):null,emphasis:!upscale&&$('#emphasis-text').value?[{text:$('#emphasis-text').value,weight:Number($('#emphasis-weight').value)}]:[],
    frames:video?Number($('#duration').value):null,fps:video?24:null,
    operation,source_asset_id:operation==='txt2img'?null:sourceAsset?.id||null,end_asset_id:operation==='img2vid'?endAsset?.id||null:null,
    variation_seed:$('#variation-seed').value,variation_strength:Number($('#variation-strength').value),denoise:Number($('#denoise').value),scale:Number($('#upscale-factor').value),upscale_model:$('#upscale-model').value,
    refinement:!upscale&&!video&&$('#refinement-enabled').checked?{workflow:$('#refinement-workflow').value,model:$('#refinement-model').value,steps:Number($('#refinement-steps').value),denoise:Number($('#refinement-denoise').value)}:null};
}
function saveDraft(){if(!editingJob)localStorage.setItem('pixanvil-draft',JSON.stringify(data()));}
function setModelOptions(workflow,model){const w=workflows.find(w=>w.id===workflow);$('#model').replaceChildren(...(w?.models||[]).map(m=>{const o=el('option','',m);o.value=m;return o;}));if(model&&w?.models.includes(model))$('#model').value=model;$('#workflow-description').textContent=w?(w.available?w.description:w.issues.join('; ')):'Connect to SPARK to discover workflows.';$('#negative-label').hidden=!w?.negative;$('#emphasis-section').hidden=!w?.emphasis;$('#negative').disabled=!w?.negative;$('#emphasis-text').disabled=!w?.emphasis;if(!w?.negative)$('#negative').value='';if(!w?.emphasis)$('#emphasis-text').value='';setWorkflowOptions(w);updateOperation();}
// Every clip length a video model accepts: its frame rule allows offset + n × step frames between min and max.
function videoLengths(w){
  const rule=w?.frames;if(!rule)return [];
  const lengths=[];
  for(let n=Math.ceil((rule.min-rule.offset)/rule.step);rule.offset+n*rule.step<=rule.max;n++)lengths.push(rule.offset+n*rule.step);
  return lengths;
}
// The length slider stops only on lengths the model accepts: its step is the frame rule's step, starting at the shortest.
// Every accepted length gets a tick, and whole multiples of 5 seconds get a label.
// Switching models keeps the length closest in time to the one chosen before; the first video model starts at its default.
function setLengths(w){
  const lengths=videoLengths(w),range=$('#duration');
  if(!lengths.length)return;
  const before=range.dataset.fps?Number(range.value)/Number(range.dataset.fps):null;
  const first=lengths[0],last=lengths.at(-1),fps=w.defaults.fps;
  range.min=first;range.max=last;range.step=w.frames.step;range.dataset.fps=fps;range.dataset.default=w.defaults.frames;
  const at=frames=>`${(frames-first)/(last-first)*100}%`;
  const ticks=lengths.map(frames=>{const tick=el('i');tick.style.left=at(frames);return tick;});
  const labels=[];
  for(let seconds=5;seconds*fps<=last;seconds+=5){
    if(seconds*fps<first)continue;
    const label=el('span','',`${seconds} s`);label.style.left=at(seconds*fps);labels.push(label);
  }
  $('#duration-ticks').replaceChildren(...ticks,...labels);
  range.value=before===null?w.defaults.frames:lengths.reduce((best,f)=>Math.abs(f/fps-before)<Math.abs(best/fps-before)?f:best);
  showLength();
}
function showLength(){
  const range=$('#duration'),frames=Number(range.value),fps=Number(range.dataset.fps||24);
  $('#duration-value').textContent=`${(frames/fps).toFixed(1)} s · ${frames} frames`;
  const fallback=Number(range.dataset.default);
  $('#duration-default').disabled=frames===fallback;
  $('#duration-default').title=`Set to this model's default length, ${(fallback/fps).toFixed(1)} s · ${fallback} frames`;
}
$('#duration').oninput=showLength;
$('#duration-default').onclick=()=>{$('#duration').value=$('#duration').dataset.default;showLength();};
function loadRecipe(r){let wf=r.workflow&&workflows.some(w=>w.id===r.workflow)?r.workflow:null;if(!wf&&r.model&&r.operation!=='upscale'){const match=workflows.find(w=>w.models.includes(r.model));if(match)wf=match.id;else notice('The imported model is unavailable. Select a compatible installed model before generating.');}const source=sourceAsset,end=endAsset;setMode(modeOf({...r,workflow:wf}),wf);sourceAsset=r.operation&&r.operation!=='txt2img'?source:null;endAsset=r.operation==='img2vid'?end:null;setModelOptions($('#workflow').value,r.model);$('#prompt').value=r.prompt||'';const w=workflows.find(w=>w.id===$('#workflow').value);$('#negative').value=w?.negative?(r.negative||''):'';$('#seed').value=r.seed??'-1';if(r.width&&r.height&&r.operation!=='edit')setDimensions(r.width,r.height);if(r.frames){$('#duration').value=r.frames;showLength();}$('#quality').value=Math.max(0,profiles.indexOf(r.quality||'quality'));$('#steps').value=r.operation==='upscale'?'':r.steps??'';$('#cfg').value=r.operation==='upscale'?'':r.cfg??'';$('#count').value=r.count||1;$('#emphasis-text').value=w?.emphasis?(r.emphasis?.[0]?.text||''):'';$('#emphasis-weight').value=r.emphasis?.[0]?.weight??1;
  $('#variation-seed').value=r.variation_seed??'-1';$('#variation-strength').value=r.variation_strength??.15;$('#denoise').value=r.denoise??.25;$('#upscale-factor').value=r.scale||2;if(r.upscale_model)$('#upscale-model').value=r.upscale_model;
  $('#refinement-enabled').checked=!!r.refinement;$('#refinement-workflow').value=r.refinement?.workflow||'sdxl';refinementModels(r.refinement?.model);$('#refinement-steps').value=r.refinement?.steps||20;$('#refinement-denoise').value=r.refinement?.denoise??.25;updateOperation();if(r.operation==='img2vid'&&r.width&&r.height)setDimensions(r.width,r.height);updateLabels();saveDraft();
}
function setDimensions(width,height){const dims=`${width}x${height}`;if(![...$('#dimensions').options].some(o=>o.value===dims)){const o=el('option','',`${width} × ${height}`);o.value=dims;$('#dimensions').append(o);}$('#dimensions').value=dims;}
function refinementModels(model){const w=workflows.find(w=>w.id===$('#refinement-workflow').value);$('#refinement-model').replaceChildren(...(w?.models||[]).map(m=>{const o=el('option','',m);o.value=m;return o;}));if(model&&w?.models.includes(model))$('#refinement-model').value=model;}
function showSlot(name,asset,show,title,empty){
  $(`#${name}-card`).hidden=!show;$(`#${name}-title`).textContent=title;$(`#${name}-preview`).hidden=!asset;$(`#clear-${name}`).hidden=!asset;
  if(asset)$(`#${name}-preview`).src=`/api/assets/${asset.id}/thumbnail`;
  $(`#${name}-description`).textContent=asset?`${asset.width} × ${asset.height} · ${asset.recipe.prompt.slice(0,110)}`:empty;
}
function updateOperation(){
  const m=mode(),w=workflows.find(w=>w.id===$('#workflow').value),video=videoMode();
  // A video workflow that takes no start or end image drops any that were attached.
  if(video&&w){if(!w.start_image)sourceAsset=null;if(!w.end_image)endAsset=null;}
  const op=apiOperation(),upscale=op==='upscale',variation=op==='variation';
  // A seed variation needs a generated image, so it is offered only when one is the source.
  $('#operation').querySelector('[value=variation]').disabled=!(variation||(sourceAsset&&!isVideo(sourceAsset)&&['txt2img','variation'].includes(sourceAsset.recipe.operation||'txt2img')));
  $('#video-controls').hidden=!video;$('#refinement-section').hidden=video;
  // Animating defaults to a canvas with the source image's shape; the server picks the size.
  const auto=$('#dimensions').querySelector('[value=auto]');
  if(op==='img2vid'&&!auto){const o=el('option','','Match the source image');o.value='auto';$('#dimensions').prepend(o);$('#dimensions').value='auto';}else if(op!=='img2vid'&&auto)auto.remove();
  $('#prompt').placeholder=op==='img2vid'?'Describe the motion and the sound: what happens after this frame…':op==='edit'?'Describe the change: what to alter, and what must stay the same…':video?'Describe the clip you want to make, including sounds and speech…':'Describe the image you want to make…';
  showSlot('source',sourceAsset,video?!!w?.start_image:m!=='image',video?'START IMAGE · OPTIONAL':op==='edit'?'IMAGE TO CHANGE':'SOURCE IMAGE',video?'The clip opens on this image. Choose or drop a file here, or open an image in Gallery and choose Animate.':'Choose or drop a file here, or open an image in Gallery and use its buttons.');
  showSlot('end',endAsset,video&&!!w?.end_image,'END IMAGE · OPTIONAL','The clip finishes on this image. Choose or drop a file here; use it alone or with a start image.');
  $('#variation-controls').hidden=!variation;$('#denoise-controls').hidden=op!=='refine';$('#upscale-controls').hidden=!upscale;
  $('#direction-controls').hidden=upscale;$('#sampler-controls').hidden=upscale;$('#prompt').disabled=upscale;
  for(const id of ['seed','random-seed','workflow','model','dimensions'])$('#'+id).disabled=variation;
  $('#count').disabled=!!editingJob;
  $('#dimensions').disabled=variation||op==='edit';
  $('#refinement-controls').hidden=!$('#refinement-enabled').checked;
  const labels={txt2img:'Generate image ↗',variation:'Generate variation ↗',refine:'Refine image ↗',upscale:'Upscale image ↗',img2vid:'Animate image ↗',edit:'Edit image ↗'};
  $('#generate-button').textContent=editingJob?'Save changes to queued job ↗':video?'Generate video ↗':labels[op];$('#operation-caption').textContent=op==='txt2img'?'Create something new':video?'Source images preserved · clip saved separately':'Original preserved · result saved separately';
  const feature=toolCapabilities[op],ref=workflows.find(w=>w.id===$('#refinement-workflow').value);
  $('#generate-button').disabled=busy||editLocked||(op!=='txt2img'&&(!(sourceAsset||endAsset)||!feature?.available))||(!upscale&&!w?.available)||(op==='refine'&&(w?.id==='flux-schnell'||w?.media==='video'))||(op==='img2vid'&&!w?.start_image)||(op==='edit'&&!w?.source_image)||(['txt2img','refine'].includes(op)&&!!w?.source_image)||(!upscale&&!video&&$('#refinement-enabled').checked&&!ref?.available);
  $('#upscale-description').textContent=sourceAsset?`Output: ${sourceAsset.width*Number($('#upscale-factor').value)} × ${sourceAsset.height*Number($('#upscale-factor').value)}. RealESRGAN renders at 4×, then resizes for a 2× result.`:'';
  for(const [id,label] of [['variation-strength','variation-label'],['denoise','denoise-label'],['refinement-denoise','refinement-label']])$('#'+label).textContent=Number($('#'+id).value).toFixed(2);
}
// A source that has since left the library is dropped; the rest of the settings still load.
const findAsset=id=>id?api('/assets/'+id).catch(()=>null):null;
async function restoreRecipe(r){sourceAsset=await findAsset(r.source_asset_id);endAsset=await findAsset(r.end_asset_id);loadRecipe(r);}
async function startImageTool(op,asset=selected){
  endEditing(false);sourceAsset=asset;endAsset=null;const r={...asset.recipe,operation:op,source_asset_id:asset.id,end_asset_id:null,variation_seed:'-1',count:1};
  if(op==='refine'){r.refinement=null;if(['flux-schnell','upscale','import'].includes(r.workflow)||r.operation==='frame'||workflows.find(w=>w.id===r.workflow)?.media==='video'||workflows.find(w=>w.id===r.workflow)?.source_image){r.workflow='sdxl';r.model='';r.steps=null;r.cfg=null;}const scale=Math.min(1,1536/Math.max(asset.width,asset.height));r.width=Math.max(256,Math.round(asset.width*scale/64)*64);r.height=Math.max(256,Math.round(asset.height*scale/64)*64);}
  if(op==='edit'){const w=workflows.find(w=>w.source_image&&w.available)||workflows.find(w=>w.source_image);Object.assign(r,{workflow:w?.id||r.workflow,model:'',steps:null,cfg:null,refinement:null,negative:'',emphasis:[],seed:'-1',prompt:''});}
  if(op==='img2vid'){const w=workflows.find(w=>w.start_image&&w.available)||workflows.find(w=>w.start_image);Object.assign(r,{workflow:w?.id||r.workflow,model:'',steps:null,cfg:null,width:null,height:null,frames:null,refinement:null,negative:'',emphasis:[],seed:'-1'});if(asset.recipe.operation==='import')r.prompt='';}
  loadRecipe(r);$('#viewer').close();tab('generate');
  if(!toolCapabilities[op]?.available)notice(toolCapabilities[op]?.issues?.join('; ')||'Connect to SPARK to use this operation.');else notice('');
}
$('#operation').onchange=run(async()=>{
  const m=mode(),before=$('#workflow').value;notice('');
  if(m==='variation'){await startImageTool('variation',sourceAsset);return;}
  // A new image starts from the workflow's own canvas list, not a size left over from a reused result.
  if(m==='image')optionsWorkflow='';
  setMode(m);
  if($('#workflow').value!==before){$('#steps').value='';$('#cfg').value='';}
  setModelOptions($('#workflow').value);
  if(m==='refine'&&sourceAsset)fitRefineCanvas(sourceAsset);
  saveDraft();});
async function attachFile(file,slot){
  const form=new FormData();form.append('file',file);const asset=await api('/assets/import','POST',form);
  if(slot==='end')endAsset=asset;
  else{sourceAsset=asset;if(mode()==='refine')fitRefineCanvas(asset);}
  updateOperation();saveDraft();await refreshGallery();
}
// The file in a picker's change event; the picker is cleared so choosing the same file again still fires.
function pickedFile(e){const file=e.target.files[0];e.target.value='';return file;}
// Any box that takes an image also takes a file dragged onto it from Explorer.
function dropZone(node,onFile){
  const hasFiles=e=>e.dataTransfer.types.includes('Files');
  node.addEventListener('dragover',e=>{if(!hasFiles(e))return;e.preventDefault();e.dataTransfer.dropEffect='copy';node.classList.add('drop-target');});
  node.addEventListener('dragleave',e=>{if(!node.contains(e.relatedTarget))node.classList.remove('drop-target');});
  node.addEventListener('drop',run(async e=>{
    if(!hasFiles(e))return;e.preventDefault();node.classList.remove('drop-target');
    const file=e.dataTransfer.files[0];
    if(!file.type.startsWith('image/'))throw new Error(`${file.name} is not an image file`);
    await onFile(file);
  }));
}
// A file dropped beside a box would otherwise make the browser leave PixAnvil to display it.
for(const type of ['dragover','drop'])window.addEventListener(type,e=>{if(e.dataTransfer.types.includes('Files'))e.preventDefault();});
$('#source-file').onchange=run(e=>{const file=pickedFile(e);if(file)return attachFile(file,'source');});$('#end-file').onchange=run(e=>{const file=pickedFile(e);if(file)return attachFile(file,'end');});
dropZone($('#source-card'),file=>attachFile(file,'source'));dropZone($('#end-card'),file=>attachFile(file,'end'));
// Any image slot can also be filled from the gallery, without exporting and re-importing a file.
let pickerSlot='source',pickerOffset=0,pickerTimer=null;
async function openPicker(slot){
  pickerSlot=slot;
  $('#picker-title').textContent=slot==='end'?'Choose the end image':$('#source-title').textContent.startsWith('START')?'Choose the start image':'Choose the source image';
  $('#picker-search').value='';
  await loadPicker();
  $('#picker-dialog').showModal();
  $('#picker-search').focus();
}
async function loadPicker(append=false){
  if(!append){pickerOffset=0;$('#picker-grid').replaceChildren();}
  const response=await api(`/assets?media=image&limit=48&offset=${pickerOffset}&q=${encodeURIComponent($('#picker-search').value)}`);
  pickerOffset+=response.items.length;
  for(const a of response.items){
    const item=el('button','picker-item');item.type='button';item.title=`${a.recipe.prompt} · ${a.width} × ${a.height}`;
    const img=el('img');img.src=`/api/assets/${a.id}/thumbnail`;img.alt=a.recipe.prompt;img.loading='lazy';item.append(img);
    item.onclick=()=>pickImage(a);
    $('#picker-grid').append(item);
  }
  $('#picker-empty').hidden=pickerOffset>0;
  $('#picker-more').hidden=pickerOffset>=response.total;
}
function pickImage(asset){
  if(pickerSlot==='end')endAsset=asset;
  else{sourceAsset=asset;if(mode()==='refine')fitRefineCanvas(asset);}
  $('#picker-dialog').close();
  updateOperation();saveDraft();
}
$('#pick-source').onclick=run(()=>openPicker('source'));
$('#pick-end').onclick=run(()=>openPicker('end'));
$('#close-picker').onclick=()=>$('#picker-dialog').close();
$('#picker-more').onclick=run(()=>loadPicker(true));
$('#picker-search').oninput=()=>{clearTimeout(pickerTimer);pickerTimer=setTimeout(run(()=>loadPicker()),250);};
$('#clear-end').onclick=()=>{endAsset=null;updateOperation();saveDraft();};
$('#clear-source').onclick=()=>{sourceAsset=null;if(mode()==='variation'){setMode('image');setModelOptions($('#workflow').value);}updateOperation();saveDraft();};
for(const id of ['variation-strength','denoise','refinement-denoise','upscale-factor','refinement-enabled'])$('#'+id).oninput=()=>{updateOperation();saveDraft();};
$('#refinement-workflow').onchange=()=>{refinementModels();updateOperation();saveDraft();};
function updateLabels(){$('#quality-label').textContent=profiles[+$('#quality').value].replace(/^./,c=>c.toUpperCase());$('#weight-label').textContent=Number($('#emphasis-weight').value).toFixed(2)+'×';}
$('#quality').oninput=()=>{updateLabels();$('#steps').value='';$('#cfg').value='';saveDraft();};$('#emphasis-weight').oninput=()=>{updateLabels();saveDraft();};$('#workflow').onchange=()=>{if(videoMode())$('#refinement-enabled').checked=false;setModelOptions($('#workflow').value);$('#steps').value='';$('#cfg').value='';saveDraft();};
$('#generation-form').addEventListener('input',()=>{if(workflows.length)saveDraft();});
$('#random-seed').onclick=()=>{$('#seed').value='-1';saveDraft();};
// Editing a queued job reuses the Generate form. The server refuses the save once the job has started,
// and the form locks as soon as the job is seen leaving the queue.
async function editJob(j){
  if(!editingJob)stashedDraft=localStorage.getItem('pixanvil-draft');
  editingJob=j.id;editLocked=false;
  await restoreRecipe(j.recipe);$('#count').value=1;
  $('#job-dialog').close();tab('generate');showEditing();
}
function showEditing(){
  $('#edit-banner').hidden=!editingJob;$('#edit-banner').classList.toggle('locked',editLocked);
  $('#edit-status').textContent=editLocked?'This job has started, so its settings are locked. Stop editing to return to your draft.':'Editing a queued job. Changes apply only when you save them.';
  $('#generation-fields').disabled=editLocked;
  if(workflows.length)updateOperation();
}
// Leave edit mode. With restore, the draft the user had before editing comes back.
async function endEditing(restore=true){
  if(!editingJob)return;
  editingJob=null;editLocked=false;showEditing();
  const draft=stashedDraft;stashedDraft=null;
  if(restore&&draft)await restoreRecipe(JSON.parse(draft));
}
async function saveJobEdit(){
  busy=true;updateOperation();
  try{await api('/jobs/'+editingJob,'PUT',data());}
  catch(e){await refreshJobs();throw e;}
  finally{busy=false;updateOperation();}
  await endEditing();notice('Saved changes to the queued job.');tab('live');await refreshJobs();
}
// The form locks once the job being edited is no longer waiting in the queue.
function checkEditing(jobs){
  if(!editingJob||editLocked)return;
  if(jobs.find(j=>j.id===editingJob)?.state!=='queued'){editLocked=true;showEditing();}
}
$('#stop-editing').onclick=run(()=>endEditing());
$('#example-prompt').onclick=()=>{$('#prompt').value='Create a high quality image of a young, handsome teenage water knight casting a powerful water spell. Sleeveless armor, cinematic lighting';saveDraft();};
$('#generation-form').onsubmit=run(async e=>{e.preventDefault();if(editingJob)return saveJobEdit();busy=true;$('#generate-button').disabled=true;try{const response=await api('/jobs','POST',data());notice(`Queued ${response.jobs.length} ${response.jobs[0].recipe.media==='video'?'video':'image'}(s). You can keep browsing; results save automatically.`);await refreshJobs();}finally{busy=false;setModelOptions($('#workflow').value,$('#model').value);}});
async function refreshGallery(append=false){if(!append)offset=0;const response=await api(`/assets?limit=48&offset=${offset}&favorites=${favorites}&q=${encodeURIComponent($('#search').value)}`);total=response.total;assets=append?[...assets,...response.items]:response.items;offset=assets.length;$('#image-count').textContent=`${total} image${total===1?'':'s'}`;$('#empty-gallery').hidden=total>0;$('#load-more').hidden=offset>=total;const grid=$('#gallery-grid');grid.replaceChildren();for(const a of assets){const card=el('button','card');card.setAttribute('aria-label',a.recipe.prompt);const imgWrap=el('div','card-image');const img=el('img');img.src=`/api/assets/${a.id}/thumbnail`;img.alt=a.recipe.prompt;img.loading='lazy';imgWrap.append(img);if(a.favorite)imgWrap.append(el('span','heart','♥'));if(isVideo(a))imgWrap.append(el('span','play',`▶ ${clipSeconds(a.recipe)}s`));const text=el('div','card-text');text.append(el('strong','',a.recipe.prompt),el('span','',`${a.width} × ${a.height}`));card.append(imgWrap,text,el('small','',`${a.recipe.workflow} / ${new Date(a.created).toLocaleDateString()}`));card.onclick=()=>openAsset(a);grid.append(card);}renderPending();if(!favorites&&!$('#search').value){if(assets.length)showLatest(assets[0]);else if(latestId===''){$('#latest-preview').replaceChildren(...latestPlaceholder.map(n=>n.cloneNode(true)));$('#latest-caption').textContent='';}}}
// The Live tab's empty state, restored when its last result is deleted.
const latestPlaceholder=[...$('#latest-preview').childNodes].map(n=>n.cloneNode(true));
// The Activity tab shows the newest result: a clip plays in place, an image opens in the viewer.
function showLatest(a){
  if(a.id===latestId)return;latestId=a.id;let node;
  if(isVideo(a)){node=el('video');node.controls=true;node.loop=true;node.playsInline=true;node.poster=`/api/assets/${a.id}/thumbnail`;node.src=`/api/assets/${a.id}/file`;}
  else{node=el('img');node.src=`/api/assets/${a.id}/image`;node.alt=a.recipe.prompt;node.onclick=()=>openAsset(a);}
  $('#latest-preview').replaceChildren(node);$('#latest-caption').textContent=`Latest · ${a.recipe.workflow} · ${a.recipe.prompt.slice(0,160)}`;
}
// Small pictures of the images a job was given: its source, or a clip's start and end image.
function inputThumbs(r){
  const box=el('div','job-inputs');
  for(const [id,label] of [[r.source_asset_id,r.operation==='img2vid'?'START':'SOURCE'],[r.end_asset_id,'END']]){
    if(!id)continue;
    const figure=el('figure'),img=el('img');img.src=`/api/assets/${id}/thumbnail`;img.alt=label.toLowerCase()+' image';img.loading='lazy';
    // An image that has since left the library simply drops out.
    img.onerror=()=>figure.remove();
    figure.append(img,el('figcaption','',label));box.append(figure);
  }
  return box;
}
// Jobs that have not produced a result yet appear at the front of the gallery as placeholders.
function renderPending(){
  const grid=$('#gallery-grid');grid.querySelectorAll('.card.pending').forEach(n=>n.remove());
  if(favorites||$('#search').value)return;
  const cards=activeJobs.map(j=>{
    const card=el('div','card pending'),frame=el('div','card-image'),source=j.recipe.source_asset_id||j.recipe.end_asset_id;
    card.tabIndex=0;card.setAttribute('role','button');card.setAttribute('aria-label','Show job details: '+j.recipe.prompt);
    card.onclick=()=>openJob(j);card.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();openJob(j);}};
    if(source){const img=el('img');img.src=`/api/assets/${source}/thumbnail`;img.alt='';img.onerror=()=>img.remove();frame.append(img);}
    frame.append(el('span','state',j.state==='queued'?'Queued':j.state==='unknown'?'Needs attention':'Rendering'),progressView(j));
    const text=el('div','card-text');text.append(el('strong','',j.recipe.prompt),el('span','',j.recipe.media==='video'?'video':'image'));
    card.append(frame,text,el('small','',`${j.recipe.workflow} / on its way`));return card;
  });
  grid.prepend(...cards);$('#empty-gallery').hidden=total>0||cards.length>0;
}
let searchTimer;$('#search').oninput=()=>{clearTimeout(searchTimer);searchTimer=setTimeout(run(()=>refreshGallery()),250);};$('#favorites').onclick=run(async()=>{favorites=true;$('#favorites').classList.add('selected');$('#all-images').classList.remove('selected');await refreshGallery();});$('#all-images').onclick=run(async()=>{favorites=false;$('#all-images').classList.add('selected');$('#favorites').classList.remove('selected');await refreshGallery();});$('#load-more').onclick=run(()=>refreshGallery(true));
function openAsset(a){selected=a;const video=isVideo(a),file=`/api/assets/${a.id}/file`;$('#full-image').hidden=video;$('#full-video').hidden=!video;
  if(video){if(!$('#full-video').src.endsWith(file)){$('#full-video').poster=`/api/assets/${a.id}/thumbnail`;$('#full-video').src=file;}}else{$('#full-video').removeAttribute('src');$('#full-image').src=file;$('#full-image').alt=a.recipe.prompt;}
  $('#image-prompt').textContent=a.recipe.prompt;$('#image-details').replaceChildren();const details={Operation:a.recipe.operation||'txt2img',Model:a.recipe.model,Seed:a.recipe.seed,Size:`${a.width} × ${a.height}`,Steps:a.recipe.steps,Guidance:a.recipe.cfg,Workflow:a.recipe.workflow,File:a.path};if(video)details.Length=`${clipSeconds(a.recipe)} s · ${a.recipe.frames} frames at ${a.recipe.fps} fps`;if(a.recipe.operation==='variation'){details['Variation seed']=a.recipe.variation_seed;details.Strength=a.recipe.variation_strength;}if(a.recipe.operation==='refine')details.Denoise=a.recipe.denoise;if(a.recipe.refinement)details['Second model']=a.recipe.refinement.model;for(const [k,v]of Object.entries(details)){ $('#image-details').append(el('dt','',k),el('dd','',String(v)));}$('#image-recipe').textContent=JSON.stringify(a.recipe,null,2);$('#download-image').href=file;$('#download-image').download=a.id+(video?'.mp4':'.png');$('#download-image').textContent=video?'Download MP4':'Download PNG';$('#refine-image').disabled=$('#upscale-image').disabled=$('#animate-image').disabled=$('#edit-image').disabled=video;$('#frame-image').hidden=!video;$('#reuse-image').disabled=a.recipe.operation==='sequence';$('#favorite-image').textContent=a.favorite?'♥ Favorited':'♡ Favorite';$('#parent-image').hidden=!a.recipe.parent_asset_id;$('#vary-image').disabled=video||!['txt2img','variation'].includes(a.recipe.operation||'txt2img');run(updateBranchButton)(a);if(!$('#viewer').open)$('#viewer').showModal();}
// Left and right arrows step through the gallery in its displayed order, loading more as needed.
$('#viewer').addEventListener('keydown',run(async e=>{
  if(!['ArrowLeft','ArrowRight'].includes(e.key)||e.target.closest('input,textarea,select'))return;
  e.preventDefault();
  const i=assets.findIndex(a=>a.id===selected?.id);if(i<0)return;
  const next=i+(e.key==='ArrowRight'?1:-1);
  if(next>=assets.length&&offset<total)await refreshGallery(true);
  if(next>=0&&next<assets.length)openAsset(assets[next]);
}));
$('#close-viewer').onclick=()=>$('#viewer').close();$('#viewer').addEventListener('close',()=>$('#full-video').pause());$('#reuse-image').onclick=run(async()=>{endEditing(false);await restoreRecipe(selected.recipe);$('#viewer').close();tab('generate');});$('#favorite-image').onclick=run(async()=>{selected=await api('/assets/'+selected.id,'PUT',{favorite:!selected.favorite,rating:selected.rating});openAsset(selected);await refreshGallery();});
$('#vary-image').onclick=run(()=>startImageTool('variation'));$('#refine-image').onclick=run(()=>startImageTool('refine'));$('#upscale-image').onclick=run(()=>startImageTool('upscale'));$('#animate-image').onclick=run(()=>startImageTool('img2vid'));$('#edit-image').onclick=run(()=>startImageTool('edit'));
// A clip's first frame becomes an ordinary image, ready for Edit and then Animate.
$('#frame-image').onclick=run(async()=>{const frame=await api(`/assets/${selected.id}/first-frame`,'POST');await refreshGallery();openAsset(frame);});
$('#parent-image').onclick=run(async()=>openAsset(await api('/assets/'+selected.recipe.parent_asset_id)));
// Deleting a generated output and deleting its job are the same action: both remove the record and the files.
async function deleteRecord(path,question){
  if(!confirm(question))return false;
  const result=await api(path,'DELETE');
  for(const id of result.deleted_assets){if(sourceAsset?.id===id)sourceAsset=null;if(endAsset?.id===id)endAsset=null;if(latestId===id)latestId='';}
  if(workflows.length){updateOperation();saveDraft();}
  notice(result.leftover_files.length?`Deleted, but these files could not be removed: ${result.leftover_files.join(', ')}`:'');
  await Promise.all([refreshGallery(),refreshJobs()]);
  return true;
}
// Return the Generate form, the gallery search and the saved draft to a fresh visit's defaults.
function resetForm(){
  editingJob=null;editLocked=false;stashedDraft=null;showEditing();
  localStorage.removeItem('pixanvil-draft');
  sourceAsset=null;endAsset=null;optionsWorkflow='';
  $('#generation-form').reset();$('#search').value='';
  if(workflows.length){setMode('image','flux-dev');setModelOptions($('#workflow').value);refinementModels();}
  updateLabels();
}
// Start over: list everything the library holds, then delete it all once the user confirms.
function count(n,one,many){return `${n} ${n===1?one:many}`;}
function fileSize(bytes){const units=['bytes','KB','MB','GB','TB'];let i=0;while(bytes>=1024&&i<units.length-1){bytes/=1024;i++;}return i?`${bytes.toFixed(1)} ${units[i]}`:`${bytes} bytes`;}
$('#open-wipe').onclick=run(async()=>{
  const s=await api('/library/summary');
  $('#wipe-summary').replaceChildren(...[
    count(s.jobs,'history entry','history entries')+', including their prompts and recipes',
    count(s.generated,'generated image or video','generated images and videos'),
    count(s.imported,'imported image','imported images'),
    count(s.inputs,'saved source image','saved source images')+' used by image and video tools',
    `${fileSize(s.bytes)} of files in total`,
  ].map(t=>el('li','',t)));
  const empty=!s.jobs&&!s.generated&&!s.imported&&!s.inputs;
  $('#wipe-blocked').hidden=!s.busy&&!empty;
  $('#wipe-blocked').textContent=s.busy?`${count(s.busy,'job','jobs')} may still be running on ComfyUI. Wait for ${s.busy===1?'it':'them'} to finish, or abandon ${s.busy===1?'it':'them'} in History, then try again.`:'There is nothing to delete.';
  $('#confirm-wipe').disabled=Boolean(s.busy)||empty;
  $('#wipe-dialog').showModal();
});
$('#close-wipe').onclick=$('#cancel-wipe').onclick=()=>$('#wipe-dialog').close();
$('#confirm-wipe').onclick=run(async()=>{
  const r=await api('/library','DELETE');
  latestId='';resetForm();
  $('#wipe-dialog').close();
  notice(r.leftover_files.length?`Deleted, but these files could not be removed: ${r.leftover_files.join(', ')}`:`Deleted ${count(r.deleted_jobs,'history entry','history entries')}, ${count(r.deleted_assets,'output','outputs')} and ${count(r.deleted_inputs,'saved source image','saved source images')}.`);
  await Promise.all([refreshGallery(),refreshJobs()]);
});
$('#delete-image').onclick=run(async()=>{
  const a=selected,kind=isVideo(a)?'video':'image';
  const others=a.job_id?((await api('/jobs/'+a.job_id).catch(()=>null))?.outputs.length||1)-1:0;
  const question=a.job_id?`Delete this ${kind} and its history entry${others?`, along with ${others} other output${others===1?'':'s'} from the same job`:''}? The files are removed from your library.`:`Delete this imported ${kind}? The file is removed from your library.`;
  if(await deleteRecord('/assets/'+a.id,question))$('#viewer').close();
});
async function refreshPresets(){presets=(await api('/presets')).items;const selectedId=$('#preset-select').value;$('#preset-select').replaceChildren(el('option','','Load a preset…'));$('#preset-select').firstChild.value='';for(const p of presets){const option=el('option','',p.name);option.value=p.id;$('#preset-select').append(option);}$('#preset-select').value=selectedId;}
$('#save-preset').onclick=run(async()=>{const name=prompt('Name this prompt preset');if(!name)return;const id=$('#preset-select').value;await api('/presets','POST',{name,data:data(),id:presets.find(p=>p.id===id)?.name===name?id:null});await refreshPresets();notice('Preset saved.');});$('#preset-select').onchange=()=>{const p=presets.find(p=>p.id===$('#preset-select').value);if(p)loadRecipe({...data(),prompt:p.data.prompt,negative:p.data.negative,emphasis:p.data.emphasis});};$('#delete-preset').onclick=run(async()=>{const id=$('#preset-select').value;if(!id)return;await api('/presets/'+id,'DELETE');await refreshPresets();});
function progressView(j){
  const box=el('div','render-progress'),p=j.progress;
  if(!p)return box;
  const measured=p.connected&&Number.isFinite(p.max)&&p.max>0;
  const label=!p.connected?'Live progress reconnecting…':measured?`${p.stage} · ${p.value} / ${p.max} · ${p.percent}%`:p.stage;
  box.append(el('small','progress-label',label));
  if(measured){const bar=el('progress');bar.max=p.max;bar.value=p.value;bar.setAttribute('aria-label',p.stage+' progress');box.append(bar);}
  return box;
}
// Job details for the Live tab: what was asked for, when, where it stands in the queue, and how far it has got.
let openJobId=null;
function openJob(j){openJobId=j.id;showJob(j,activeJobs);$('#job-dialog').showModal();}
const jobStatus={queued:'Waiting in the queue',submitting:'Sending to ComfyUI',running:'Rendering now',collecting:'Saving results',unknown:'Needs attention: ComfyUI may still be working on it',complete:'Finished',failed:'Failed',cancelled:'Cancelled',abandoned:'Abandoned'};
function showJob(j,active){
  const r=j.recipe,ahead=active.filter(a=>a.created<j.created).length;
  const details={
    Status:jobStatus[j.state]||j.state,
    Queue:j.state==='queued'?(ahead?`${count(ahead,'job','jobs')} ahead of it`:'Next up'):null,
    Submitted:new Date(j.created).toLocaleString(),
    Operation:r.operation||'txt2img',Workflow:r.workflow,Model:r.model,Seed:r.seed,
    Size:r.width&&r.height?`${r.width} × ${r.height}`:null,
    Length:r.media==='video'&&r.frames?`${clipSeconds(r)} s · ${r.frames} frames at ${r.fps} fps`:null,
    Steps:r.steps,Guidance:r.cfg,
    Denoise:['refine','edit'].includes(r.operation)?r.denoise:null,
    Strength:r.operation==='variation'?r.variation_strength:null,
    Scale:r.operation==='upscale'?`${r.scale}×`:null,
    Negative:r.negative,
  };
  const list=el('dl');
  for(const [k,v] of Object.entries(details))if(v!==null&&v!==undefined&&v!=='')list.append(el('dt','',k),el('dd','',String(v)));
  $('#job-title').textContent=r.prompt||'Untitled job';
  $('#job-body').replaceChildren(progressView(j),...(j.error?[el('p','error',j.error)]:[]),inputThumbs(r),list);
  const actions=[];
  const reuse=el('button','quiet','Reuse settings');reuse.onclick=run(async()=>{endEditing(false);await restoreRecipe(r);$('#job-dialog').close();tab('generate');});actions.push(reuse);
  if(j.state==='queued'){const edit=el('button','quiet','Edit settings');edit.onclick=run(()=>editJob(j));actions.push(edit);}
  if(j.state==='queued'||j.state==='unknown'){
    const b=el('button','quiet',j.state==='queued'?'Cancel queued':'Abandon local job');
    b.onclick=run(async()=>{await api(`/jobs/${j.id}/${j.state==='queued'?'cancel':'abandon'}`,'POST');await refreshJobs();});actions.push(b);
  }
  const close=el('button','quiet','Close');close.onclick=()=>$('#job-dialog').close();actions.push(close);
  $('#job-actions').replaceChildren(...actions);
}
$('#close-job').onclick=()=>$('#job-dialog').close();
let galleryJobSignature='';
// Each job's last seen state, so a job that fails while the page is open is announced instead of silently vanishing.
let jobStates=null;
async function refreshJobs(){
  const jobs=(await api('/jobs')).items;
  checkEditing(jobs);
  const sig=JSON.stringify(jobs.map(j=>[j.id,j.state,j.outputs,j.error,j.progress]));
  if(sig===lastSignature)return;
  lastSignature=sig;
  const failed=jobStates?jobs.filter(j=>j.state==='failed'&&jobStates[j.id]&&jobStates[j.id]!=='failed'):[];
  jobStates=Object.fromEntries(jobs.map(j=>[j.id,j.state]));
  if(failed.length)notice(`${failed.length===1?'A job':count(failed.length,'job','jobs')} failed: ${(failed[0].error||'no reason given').split('\n')[0]} Details are in History.`);
  const gallerySig=JSON.stringify(jobs.map(j=>[j.id,j.outputs]));
  const changed=galleryJobSignature!==''&&galleryJobSignature!==gallerySig;
  galleryJobSignature=gallerySig;
  const container=$('#history-list');container.replaceChildren();
  if(!jobs.length)container.append(el('p','muted','Your generation history will appear here.'));
  for(const j of jobs){
    const row=el('div','job-row'),status=el('div','badge '+j.state,j.state),body=el('div');
    body.append(el('p','',j.recipe.prompt),el('small','',`${j.recipe.workflow} · seed ${j.recipe.seed} · ${new Date(j.created).toLocaleString()}`),inputThumbs(j.recipe),progressView(j));
    if(j.error)body.append(el('p','error',j.error));
    const actions=el('div'),reuse=el('button','quiet','Reuse settings');
    reuse.onclick=run(async()=>{endEditing(false);await restoreRecipe(j.recipe);tab('generate');});actions.append(reuse);
    if(j.state==='queued'){const edit=el('button','quiet','Edit settings');edit.onclick=run(()=>editJob(j));actions.append(edit);}
    if(j.state==='queued'||j.state==='unknown'){
      const b=el('button','quiet',j.state==='queued'?'Cancel queued':'Abandon local job');
      b.onclick=run(async()=>{await api(`/jobs/${j.id}/${j.state==='queued'?'cancel':'abandon'}`,'POST');await refreshJobs();});actions.append(b);
    }
    // Work that may still be running on ComfyUI cannot be deleted until it finishes or is abandoned.
    if(!['submitting','running','collecting','unknown'].includes(j.state)){
      const remove=el('button','quiet danger','Delete'),n=j.outputs.length;
      remove.onclick=run(()=>deleteRecord('/jobs/'+j.id,n?`Delete this job and its ${n} output${n===1?'':'s'}? The files are removed from your library.`:'Delete this job from your history?'));
      actions.append(remove);
    }
    row.append(status,body,actions);container.append(row);
  }
  const active=jobs.filter(j=>['queued','submitting','running','collecting','unknown'].includes(j.state));
  $('#activity').replaceChildren();
  if(!active.length)$('#activity').textContent='Ready for your next idea.';
  for(const j of active){
    const item=el('div','activity-job');item.tabIndex=0;item.setAttribute('role','button');item.setAttribute('aria-label','Show job details: '+j.recipe.prompt);
    item.onclick=()=>openJob(j);
    item.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();item.click();}};
    item.append(el('p','',`${j.state.toUpperCase()} · ${j.recipe.prompt.slice(0,85)}${j.error?' — '+j.error:''}`),inputThumbs(j.recipe),progressView(j));
    $('#activity').append(item);
  }
  $('#footer-status').textContent=active.length?`${active.length} active job(s) · progress updates every 2 seconds`:'Your library stays on this machine.';
  // Newest first, matching the gallery's order.
  activeJobs=active;renderPending();
  // An open job details dialog follows the job until it finishes.
  const shown=$('#job-dialog').open&&jobs.find(j=>j.id===openJobId);
  if(shown)showJob(shown,active);
  if(changed)await refreshGallery();
  if(!$('#branches').hidden)run(showBranches)();
}
// ---- Branches: images are nodes, and clips are the connections between them. ----
// The server derives the graph from recipes; this page lays it out left to right and remembers where nodes sit.
const NODE_W=128,NODE_H=84,COLUMN=250,ROW=124;
// The share of an image's width, at each side, that starts a new connection when dragged instead of moving it.
const SIDE=.25;
let cy=null,branchTree=null,branchOpenId=null,branchSignature='',positionQueue={},positionTimer=null;
// The clip a playing chain is on, kept so a redraw can highlight it again; redrawingTree hides the redraw's own reselection.
let chainPlaying=null,redrawingTree=false,chainTimer=null,branchFocus=[],branchChainToOpen=null;
// Takes: the clips between the same two nodes form one pair, drawn as one connection whose id is its preferred take's.
let branchPairs=new Map(),pairByEdge=new Map(),pairByClip=new Map(),clipById=new Map();
// Selections: the order items were clicked in (Cytoscape does not keep it), and which items a box selected.
let selectionOrder=[],boxedIds=new Set();
// The chain builder, while open: its start node and its steps as clip ids, in order, repeats allowed.
let builder=null;
// Loading and drawing run one at a time, so a poll arriving mid-draw cannot build the graph twice.
let branchWork=Promise.resolve();
function branchTask(fn){branchWork=branchWork.catch(()=>{}).then(fn);return branchWork;}
function showBranches(){return branchTask(()=>branchOpenId?loadTree(branchOpenId).catch(()=>loadTrees()):loadTrees());}
function openInBranches(id,focus=[],chain=null){branchOpenId=id;branchFocus=focus;branchChainToOpen=chain;tab('branches');}
async function loadTrees(){
  if(builder){builder=null;cy?.autounselectify(false);}
  branchOpenId=null;branchTree=null;branchSignature='';
  const {items}=await api('/branches');
  $('#tree-view').hidden=true;$('#tree-picker').hidden=false;$('#empty-branches').hidden=items.length>0;
  $('#tree-grid').replaceChildren(...items.map(t=>{
    const card=el('button','card');card.setAttribute('aria-label','Open tree: '+t.name);
    const frame=el('div','card-image');
    if(t.thumbnail){const img=el('img');img.src=t.thumbnail;img.alt='';img.loading='lazy';frame.append(img);}
    if(t.pending)frame.append(el('span','play',`${t.pending} rendering`));
    const text=el('div','card-text');text.append(el('strong','',t.name),el('span','',`${count(t.clips,'clip','clips')} · ${t.seconds} s of footage`));
    card.append(frame,text,el('small','',`${count(t.nodes,'image','images')} / ${new Date(t.updated).toLocaleDateString()}`));
    card.onclick=()=>{branchOpenId=t.id;run(showBranches)();};
    return card;
  }));
}
async function loadTree(id){
  const tree=await api('/branches/'+encodeURIComponent(id));
  const fresh=branchTree?.id!==tree.id;
  branchOpenId=tree.id;branchTree=tree;
  $('#tree-picker').hidden=true;$('#tree-view').hidden=false;
  if(document.activeElement!==$('#tree-name'))$('#tree-name').value=tree.name;
  setPairs(tree);
  const finished=tree.clips.filter(c=>!c.pending).length;
  $('#tree-summary').textContent=`${count(tree.connections,'connection','connections')} · ${count(finished,'clip','clips')} · ${tree.seconds} s of footage`+(tree.pending?` · ${tree.pending} on the way`:'');
  const signature=JSON.stringify([tree.nodes,tree.clips]);
  if(fresh||signature!==branchSignature){branchSignature=signature;await drawTree(tree,fresh);}
  else cy?.resize();
  // Opened to show particular items, such as the clips a joined chain was made from.
  if(branchFocus.length&&cy){
    const focus=branchFocus;branchFocus=[];
    cy.elements().unselect();for(const item of focus)cy.getElementById(pairByClip.get(item)?.id||item).select();
  }
  if(branchChainToOpen&&cy){const chain=branchChainToOpen;branchChainToOpen=null;openChain(chain.steps,chain.saved);}
  // A gap in an opened chain fills itself in when a take for that connection arrives.
  else if(builder?.gaps.size)renderBuilder();
}
// Saved positions win. A tree seen for the first time is laid out left to right by dagre; after that a new node
// goes one column right of its start, or one column left of its end for a clip built backwards, and nothing
// already placed moves.
async function placeNodes(tree){
  const known={...tree.positions},placed={};
  const missing=tree.nodes.filter(n=>!known[n.id]);
  if(!missing.length)return known;
  const at=id=>known[id]||placed[id];
  if(!Object.keys(known).length){
    const graph=new dagre.graphlib.Graph({multigraph:true});
    graph.setGraph({rankdir:'LR',nodesep:40,ranksep:COLUMN-NODE_W});graph.setDefaultEdgeLabel(()=>({}));
    for(const n of tree.nodes)graph.setNode(n.id,{width:NODE_W,height:NODE_H});
    for(const c of tree.clips)graph.setEdge(c.source,c.target,{},c.id);
    dagre.layout(graph);
    for(const n of tree.nodes){const {x,y}=graph.node(n.id);placed[n.id]={x,y};}
  }else{
    let waiting=missing;
    while(waiting.length){
      const later=[];
      for(const n of waiting){
        const forward=tree.clips.find(c=>c.target===n.id&&at(c.source));
        const backward=!forward&&tree.clips.find(c=>c.source===n.id&&at(c.target));
        if(!forward&&!backward){later.push(n);continue;}
        const anchor=forward?at(forward.source):at(backward.target);
        placed[n.id]=freeSpot(anchor.x+(forward?COLUMN:-COLUMN),anchor.y,{...known,...placed});
      }
      // Nothing placed connects to what is left: start the first of it on a new row below.
      if(later.length===waiting.length){
        const spots=Object.values({...known,...placed});
        placed[later[0].id]=freeSpot(Math.min(...spots.map(p=>p.x)),Math.max(...spots.map(p=>p.y))+ROW,{...known,...placed});
        later.shift();
      }
      waiting=later;
    }
  }
  // Placeholders for clips still rendering are replaced when they finish, so their spots are not kept.
  const keep=Object.fromEntries(Object.entries(placed).filter(([id])=>!id.startsWith('job:')));
  if(Object.keys(keep).length)await api('/branches/layout','PUT',{positions:keep});
  Object.assign(tree.positions,keep);
  return {...known,...placed};
}
// The nearest free spot in a column, trying alternately below and above.
function freeSpot(x,y,taken){
  const spots=Object.values(taken);
  for(let i=0;;i++){
    const candidate={x,y:y+(i%2?1:-1)*Math.ceil(i/2)*ROW};
    if(!spots.some(p=>Math.abs(p.x-x)<NODE_W&&Math.abs(p.y-candidate.y)<NODE_H+16))return candidate;
  }
}
const SELECTED='#8ec5f0',GENERATING='#b8d68e';
function branchStyle(){
  const accent=getComputedStyle(document.documentElement).getPropertyValue('--accent').trim();
  return [
    {selector:'node',style:{shape:'round-rectangle',width:NODE_W,height:NODE_H,'background-color':'#252922','border-width':2,'border-color':'#3b4235',
      label:'data(label)',color:'#cbd0c3','font-size':10,'text-valign':'bottom','text-margin-y':6,'text-wrap':'ellipsis','text-max-width':NODE_W}},
    {selector:'node[thumb]',style:{'background-image':'data(thumb)','background-fit':'cover'}},
    {selector:'node.virtual',style:{opacity:.55,'border-style':'dashed'}},
    {selector:'node.pending',style:{'border-style':'dashed','text-valign':'center','text-margin-y':0}},
    {selector:'node.missing',style:{'border-style':'dashed','border-color':'#a5523f','text-valign':'center','text-margin-y':0}},
    // Orange and green mean queued and generating here, as on the History tab, so selection is blue.
    {selector:'node:selected',style:{'border-color':SELECTED,'border-width':3,opacity:1}},
    {selector:'edge',style:{'curve-style':'bezier','control-point-step-size':40,width:3,'line-color':'#5b6450','target-arrow-shape':'triangle','target-arrow-color':'#5b6450',
      label:'data(label)',color:'#cbd0c3','font-size':10,'text-background-color':'#101210','text-background-opacity':1,'text-background-padding':3}},
    // Dotted: no finished take yet. Orange: takes queued. Blinking: a take generating now, green when nothing is
    // queued after it. Together the orange lines show the whole queue and the blinking one shows what runs now.
    {selector:'edge.pending',style:{'line-style':'dashed'}},
    // Both ways between two images: straight, with endpoints set by placeTwoWay so the pair runs side by side.
    {selector:'edge.two-way',style:{'curve-style':'straight'}},
    // A clip that starts and ends on the same image: a loop over the top of the image.
    {selector:'edge.self',style:{'loop-direction':'0deg','loop-sweep':'-100deg','control-point-step-size':110,'source-endpoint':'50% -25%','target-endpoint':'-50% -25%'}},
    {selector:'edge.rendering',style:{'line-color':GENERATING,'target-arrow-color':GENERATING}},
    {selector:'edge.queued',style:{'line-color':accent,'target-arrow-color':accent}},
    {selector:'edge.rendering.blink',style:{'line-opacity':.3}},
    // In the chain builder, the connections that can be added next.
    {selector:'edge.next-step',style:{'line-color':SELECTED,'target-arrow-color':SELECTED,width:5,'line-opacity':1}},
    {selector:'edge:selected',style:{'line-color':SELECTED,'target-arrow-color':SELECTED,width:4}},
    {selector:'edge.playing',style:{'line-color':'#f4f1e8','target-arrow-color':'#f4f1e8',width:6}},
    // Dragging a new connection off an image's side: the line follows the pointer, and every side it can be
    // dropped on is lit, the one under the pointer brightest.
    {selector:'node.drop-zone',style:{shape:'rectangle',width:NODE_W*SIDE,height:NODE_H,'background-color':SELECTED,'background-opacity':.3,
      'border-width':0,label:'',events:'no','z-index':10}},
    {selector:'node.drop-zone.hover',style:{'background-opacity':.7}},
    {selector:'node.drag-end',style:{width:1,height:1,opacity:0,label:'',events:'no'}},
    {selector:'edge.drag-line',style:{'curve-style':'straight','line-style':'dashed','line-color':SELECTED,'target-arrow-color':SELECTED,
      label:'',events:'no','z-index':11}},
    {selector:'edge.drag-line.from-right',style:{'source-endpoint':'50% 0%'}},
    {selector:'edge.drag-line.from-left',style:{'target-endpoint':'-50% 0%'}},
  ];
}
// The blink of rendering connections. A system asking for reduced motion gets a steady line; the label still says rendering.
const calmMotion=matchMedia('(prefers-reduced-motion: reduce)');
setInterval(()=>{if(cy&&!calmMotion.matches&&!$('#branches').hidden)cy.edges('.rendering').toggleClass('blink');},700);
// The text under a node: its label, and its state when it has no picture.
function nodeCaption(n){
  const state={pending:'Rendering…',missing:'Deleted image'}[n.kind]||(n.available?'':'No saved frame');
  return [n.label||n.default_label,state].filter(Boolean).join(' · ');
}
// ---- Takes: every clip between the same two nodes is one pair, drawn as one connection however many there are. ----
function setPairs(tree){
  branchPairs=new Map();
  for(const c of tree.clips){
    const key=c.source+'>'+c.target;
    if(!branchPairs.has(key))branchPairs.set(key,{key,source:c.source,target:c.target,takes:[],pending:[],labelMode:'stats',label:''});
    const pair=branchPairs.get(key);
    (c.pending?pair.pending:pair.takes).push(c);
    pair.labelMode=c.pair_label_mode||'stats';pair.label=c.pair_label||'';
  }
  pairByEdge=new Map();pairByClip=new Map();clipById=new Map(tree.clips.filter(c=>!c.pending).map(c=>[c.id,c]));
  for(const [key,pair] of branchPairs){
    pair.takes.sort((a,b)=>a.take-b.take);
    pair.preferred=pair.takes.find(c=>c.preferred)||pair.takes[0]||null;
    // The connection's graph id is its pair, not a take, so it stays the same when the preferred take changes.
    pair.id='pair:'+key;
    pair.rendering=pair.pending.filter(c=>c.state!=='queued').length;
    pair.queued=pair.pending.length-pair.rendering;
    pairByEdge.set(pair.id,pair);
    for(const c of [...pair.takes,...pair.pending])pairByClip.set(c.id,pair);
  }
}
// The stats label: the preferred take's length and its number among the takes, then what is still on the way.
// A custom label replaces all of it.
function pairCaption(p){
  if(p.labelMode==='custom'&&p.label)return p.label;
  const parts=[];
  if(p.preferred)parts.push(`${p.preferred.seconds} s`+(p.takes.length>1?` · take ${p.preferred.take} · ${p.takes.length} takes`:''));
  if(p.rendering)parts.push('rendering');
  if(p.queued)parts.push(p.preferred||p.rendering?`+${p.queued} queued`:p.queued>1?`${p.queued} queued`:'queued');
  return parts.join(' · ');
}
function pairName(p){return `${nodeName(p.source)} → ${nodeName(p.target)}`;}
// Connections both ways between two images are two straight parallel lines, each shifted to the right of its own
// direction of travel, so the pair sits side by side. Cytoscape draws them through explicit endpoints on the node
// borders, worked out here and again whenever a node moves.
const TWO_WAY_GAP=12;
function placeTwoWay(edge){
  const s=edge.source().position(),t=edge.target().position();
  const length=Math.hypot(t.x-s.x,t.y-s.y);
  if(length<1)return;
  const ux=(t.x-s.x)/length,uy=(t.y-s.y)/length,ox=-uy*TWO_WAY_GAP,oy=ux*TWO_WAY_GAP;
  // From the shifted point inside a node, travelling along (dx, dy): where the line leaves the node's rectangle.
  const exit=(dx,dy)=>{
    const across=dx?((dx>0?NODE_W:-NODE_W)/2-ox)/dx:Infinity,down=dy?((dy>0?NODE_H:-NODE_H)/2-oy)/dy:Infinity;
    const k=Math.min(across,down);return `${ox+dx*k}px ${oy+dy*k}px`;
  };
  edge.style({'source-endpoint':exit(ux,uy),'target-endpoint':exit(-ux,-uy)});
}
async function drawTree(tree,fit){
  if(connectDrag)endConnectDrag();
  const positions=await placeNodes(tree);
  const elements=[
    ...tree.nodes.map(n=>({group:'nodes',classes:n.kind,position:{...positions[n.id]},
      data:{id:n.id,label:nodeCaption(n),...(n.thumbnail?{thumb:n.thumbnail}:{})}})),
    ...[...branchPairs.values()].map(p=>({group:'edges',
      classes:[p.preferred?'':'pending',p.rendering?'rendering':'',p.queued?'queued':'',p.source===p.target?'self':branchPairs.has(p.target+'>'+p.source)?'two-way':''].join(' ').trim(),
      data:{id:p.id,source:p.source,target:p.target,label:pairCaption(p)}})),
  ];
  if(!cy){
    cy=cytoscape({container:$('#branch-graph'),elements,layout:{name:'preset'},style:branchStyle(),boxSelectionEnabled:true,maxZoom:3,minZoom:.1});
    // Cytoscape remembers where the graph sits on screen and rechecks only on scrolls and on the graph's own resizes.
    // A message appearing above the graph moves it without either, and clicks would then land off target.
    new ResizeObserver(()=>cy.resize()).observe($('main'));
    // While the chain builder is open, clicking a connection adds it as the next step instead of opening its panel.
    cy.on('tap','node',e=>{if(!builder)showBranchNode(branchTree.nodes.find(n=>n.id===e.target.id()));});
    cy.on('tap','edge',e=>{const pair=pairByEdge.get(e.target.id());if(builder)addBuilderStep(pair);else showBranchPair(pair);});
    cy.on('tap',e=>{if(e.target===cy&&!builder)clearBranchPanel();});
    // Dragging off an image's side makes a new connection (see startConnectDrag).
    $('#branch-graph').addEventListener('mousedown',startConnectDrag,true);
    cy.on('tapdrag',e=>{if(connectDrag)moveConnectDrag(e.position);});
    cy.on('tapend',e=>{if(connectDrag)run(dropConnectDrag)(e.position);});
    cy.on('mousemove','node',e=>{
      const n=branchNode(e.target.id());
      $('#branch-graph').style.cursor=!builder&&!connectDrag&&canConnect(n)&&sideAt(e.target,e.position)?'crosshair':'';
    });
    cy.on('mouseout','node',()=>{if(!connectDrag)$('#branch-graph').style.cursor='';});
    cy.on('select',e=>{if(redrawingTree)return;const id=e.target.id();selectionOrder=[...selectionOrder.filter(i=>i!==id),id];});
    cy.on('unselect',e=>{if(redrawingTree)return;const id=e.target.id();selectionOrder=selectionOrder.filter(i=>i!==id);boxedIds.delete(id);});
    cy.on('boxselect',e=>boxedIds.add(e.target.id()));
    // Selecting several items offers them as a chain. Runs after the tap handlers, so it wins over a single item's panel.
    cy.on('select unselect',()=>{if(redrawingTree||builder)return;clearTimeout(chainTimer);chainTimer=setTimeout(showChainSelection,0);});
    // Dropped nodes keep their place; a dragged selection saves in one request.
    cy.on('position','node',e=>e.target.connectedEdges('.two-way').forEach(placeTwoWay));
    cy.on('dragfree','node',e=>{
      const id=e.target.id();if(id.startsWith('job:'))return;
      positionQueue[id]={...e.target.position()};branchTree.positions[id]=positionQueue[id];
      clearTimeout(positionTimer);positionTimer=setTimeout(run(async()=>{const positions=positionQueue;positionQueue={};await api('/branches/layout','PUT',{positions});}),250);
    });
  }else{
    const viewport={zoom:cy.zoom(),pan:{...cy.pan()}},chosen=cy.$(':selected').map(e=>e.id());
    redrawingTree=true;
    try{
      cy.resize();cy.elements().remove();cy.add(elements);
      for(const id of chosen)cy.getElementById(id).select();
    }finally{redrawingTree=false;}
    if(chainPlaying)cy.getElementById(chainPlaying).addClass('playing');
    if(builder)markBuilder();
    if(!fit)cy.viewport(viewport);
  }
  cy.edges('.two-way').forEach(placeTwoWay);
  if(fit){clearBranchPanel();cy.fit(undefined,40);}
}
const BRANCH_HINT='Click an image or a connection. Drag off an image\'s right or left edge to make a new clip after or before it: drop on an image to connect the two, or on empty space to extend. For a chain, shift-click connections in order, click two images, or shift-drag a box; Build chain does anything else. Drag the middle of an image to move it, scroll to zoom, F to fit.';
function clearBranchPanel(){
  stopBranchVideo();
  $('#branch-preview').replaceChildren(el('p','muted',BRANCH_HINT));
  $('#branch-info').replaceChildren();$('#branch-actions').replaceChildren();
  const chains=branchTree?.chains||[];
  if(!chains.length)return;
  const list=el('div','take-list');
  for(const chain of chains){
    const row=el('div','take-row'),changed=chain.steps.filter(s=>!clipById.has(s.clip)).length;
    const seconds=Math.round(chain.steps.reduce((sum,s)=>sum+(clipById.get(s.clip)?.seconds||0),0)*10)/10;
    const load=el('button','take-play',[chain.name,count(chain.steps.length,'step','steps'),changed?`${changed} changed`:`${seconds} s`].join(' · '));
    load.type='button';load.title='Open in the chain builder';load.onclick=()=>openChain(chain.steps,chain);
    const remove=el('button','take-icon','🗑');remove.type='button';remove.title='Delete this saved chain';
    remove.setAttribute('aria-label',`Delete saved chain ${chain.name}`);
    remove.onclick=run(async()=>{
      if(!confirm(`Delete the saved chain "${chain.name}"? Its clips are kept.`))return;
      await api('/branches/chains/'+chain.id,'DELETE');
      branchTree.chains=branchTree.chains.filter(c=>c.id!==chain.id);clearBranchPanel();
    });
    row.append(load,remove);list.append(row);
  }
  $('#branch-info').append(el('span','eyebrow',`SAVED CHAINS · ${chains.length}`),list);
}
function stopVideos(container){container.querySelectorAll('video').forEach(v=>{v.onended=null;v.pause();});}
function stopBranchVideo(){
  stopVideos($('#branch-preview'));
  cy?.edges().removeClass('playing');chainPlaying=null;
}
function playOne(container,clip){
  stopVideos(container);
  const video=el('video');video.controls=true;video.loop=true;video.playsInline=true;video.poster=clip.thumbnail;video.src=clip.file;
  container.replaceChildren(video);video.play().catch(()=>{});
}
// Plays clips back to back and loops. Two players take turns: while one plays, the other loads the next clip,
// so each join waits as little as possible. caption(i, pass) gets the pass number, so a loop can say it has looped.
function playClips(container,clips,{captionEl,caption,onShow}){
  stopVideos(container);
  const players=[0,1].map(()=>{const v=el('video');v.controls=true;v.playsInline=true;v.preload='auto';v.hidden=true;return v;});
  container.replaceChildren(...players);
  let active=0,index=0,pass=1;
  const load=(player,i)=>{player.poster=clips[i].thumbnail;player.src=clips[i].file;};
  const show=()=>{
    const current=players[active],other=players[1-active];
    other.pause();other.hidden=true;current.hidden=false;
    current.play().catch(()=>{});
    load(other,(index+1)%clips.length);
    captionEl.textContent=caption(index,pass);
    onShow?.(clips[index],index);
  };
  for(const player of players)player.onended=()=>{
    if(player!==players[active])return;
    active=1-active;index=(index+1)%clips.length;if(index===0)pass++;show();
  };
  load(players[0],0);show();
}
function highlightPlaying(clip){
  const edge=pairByClip.get(clip.id)?.id||clip.id;
  cy.edges().removeClass('playing');cy.getElementById(edge).addClass('playing');chainPlaying=edge;
}
// ---- Chains: clips that each start where the previous one ended, played back to back. ----
// Quick chains from a selection. A box runs forward from its leftmost image to its rightmost; two clicked images run
// from the first clicked to the second; clicked connections run in click order. Anything a selection cannot say,
// such as visiting an image twice, is what the chain builder is for.
function chainFromSelection(tree,ids,reversed=false){
  const order=selectionOrder.filter(id=>ids.includes(id)).concat(ids.filter(id=>!selectionOrder.includes(id)));
  const pairs=order.map(id=>pairByEdge.get(id)).filter(Boolean);
  const nodes=order.map(id=>tree.nodes.find(n=>n.id===id)).filter(Boolean);
  if(pairs.some(p=>!p.preferred))return {error:'A selected connection has no finished take yet.'};
  if(ids.some(id=>boxedIds.has(id))&&nodes.length>=2){
    const at=n=>cy.getElementById(n.id).position();
    const sorted=[...nodes].sort((a,b)=>at(a).x-at(b).x||at(a).y-at(b).y);
    const route=routeBetween(sorted[0].id,sorted.at(-1).id,pairs.length?pairs.map(p=>p.preferred):undefined);
    return route?{clips:route}:{error:'No route runs forward from the leftmost selected image to the rightmost along the selected connections.'};
  }
  if(pairs.length){
    const clips=pairs.map(p=>p.preferred);
    // In click order when that joins up; otherwise in the one order they can join in, if there is one.
    if(clips.every((c,i)=>i===0||clips[i-1].target===c.source))return {clips};
    return orderClips(clips);
  }
  if(nodes.length===2){
    const [first,second]=reversed?[nodes[1],nodes[0]]:nodes;
    const route=routeBetween(first.id,second.id),reversible=!!routeBetween(second.id,first.id);
    return route?{clips:route,reversible}:{error:`No finished clips lead from ${nodeName(first.id)} to ${nodeName(second.id)}.`,reversible};
  }
  return {error:'To play a chain, click connections that follow on from each other, click two images, or drag a box over part of the tree. For anything else, use Build chain.'};
}
// The single order the clips can be played in so each starts where the previous one ended, if there is one.
function orderClips(clips){
  const broken={error:'The selected connections do not join end to start. Click them in order, or use Build chain.'};
  const from=new Map();
  for(const c of clips){
    if(from.has(c.source))return {error:'The selected connections split into more than one path from one image. Click them in order, or use Build chain.'};
    from.set(c.source,c);
  }
  const targets=new Set(clips.map(c=>c.target)),starts=clips.filter(c=>!targets.has(c.source));
  if(starts.length!==1)return broken;
  let clip=starts[0];
  const order=[];
  while(clip&&!order.includes(clip)){order.push(clip);clip=from.get(clip.target);}
  return order.length===clips.length?{clips:order}:broken;
}
// The shortest route from one node to another through the given steps (by default every connection's preferred
// take), or null. Equally short routes are settled by taking the older clips first.
function routeBetween(start,end,steps=[...branchPairs.values()].filter(p=>p.preferred).map(p=>p.preferred)){
  if(start===end)return null;
  const ordered=[...steps].sort((a,b)=>a.created.localeCompare(b.created));
  const via=new Map(),seen=new Set([start]),queue=[start];
  while(queue.length){
    const node=queue.shift();
    for(const c of ordered.filter(c=>c.source===node)){
      if(seen.has(c.target))continue;
      seen.add(c.target);via.set(c.target,c);queue.push(c.target);
    }
  }
  if(!seen.has(end))return null;
  const order=[];
  for(let node=end;node!==start;node=via.get(node).source)order.unshift(via.get(node));
  return order;
}
function nodeName(id){
  const n=branchTree?.nodes.find(n=>n.id===id);
  return n?.label||n?.default_label||'image';
}
function chainPath(start,clips){return [nodeName(start),...clips.map(c=>nodeName(c.target))].join(' → ');}
function showChainSelection(){
  if(!cy||!branchTree||builder)return;
  const ids=cy.$(':selected').map(e=>e.id());
  if(ids.length<2)return;
  renderChainSelection(ids,false);
}
function renderChainSelection(ids,reversed){
  stopBranchVideo();
  const chain=chainFromSelection(branchTree,ids,reversed);
  const reverse=chain.reversible?[actionButton('⇄ Reverse',()=>renderChainSelection(ids,!reversed))]:[];
  $('#branch-actions').replaceChildren(...reverse);
  if(chain.error){
    $('#branch-preview').replaceChildren(el('p','muted',chain.error));
    $('#branch-info').replaceChildren(el('span','eyebrow',`${ids.length} SELECTED`));
    return;
  }
  // The steps start on each connection's preferred take; a step with several takes can be swapped for this chain only.
  const chosen=[...chain.clips];
  const poster=el('img');poster.src=chosen[0].thumbnail;poster.alt=chosen[0].prompt;
  $('#branch-preview').replaceChildren(poster);
  let heading=chainHeading(chosen);
  const caption=el('p','take-caption'),steps=stepList(chosen,()=>{const next=chainHeading(chosen);heading.replaceWith(next);heading=next;});
  $('#branch-info').replaceChildren(heading,caption,el('p','chain-path',chainPath(chosen[0].source,chosen)),
    el('p','muted','Plays the clips back to back. Each join may hitch slightly in the browser.'));
  if(chosen.some(c=>pairByClip.get(c.id).takes.length>1))$('#branch-info').append(steps);
  const play=()=>playClips($('#branch-preview'),chosen,{captionEl:caption,
    caption:i=>`Clip ${i+1} of ${chosen.length} · ${nodeName(chosen[i].source)} → ${nodeName(chosen[i].target)}`,
    onShow:(clip,i)=>{highlightPlaying(clip);[...steps.children].forEach((item,n)=>item.classList.toggle('playing',n===i));}});
  const grid=el('div','branch-grid');
  grid.append(actionButton('▶ Play chain',()=>{stopBranchVideo();play();}),exportChainButton(chosen),...reverse,
    actionButton('Build chain…',()=>startBuilder(chosen[0].source,chosen)));
  $('#branch-actions').replaceChildren(grid);
}
// Each step of a chain, with a take dropdown where the step has several takes. Changing one changes `clips` in place.
function stepList(clips,changed){
  const steps=el('ol','chain-steps');
  clips.forEach((clip,i)=>{
    const pair=pairByClip.get(clip.id),item=el('li','',`${nodeName(clip.source)} → ${nodeName(clip.target)} `);
    if(pair.takes.length>1){
      const pick=el('select');
      for(const take of pair.takes){const o=el('option','',`Take ${take.take} · ${take.seconds} s`+(take===pair.preferred?' ★':''));o.value=take.id;pick.append(o);}
      pick.value=clip.id;
      pick.onchange=()=>{clips[i]=pair.takes.find(t=>t.id===pick.value);changed(i);};
      item.append(pick);
    }else item.append(el('span','muted',`Take ${clip.take}`));
    steps.append(item);
  });
  return steps;
}
// ---- Chain builder: a chain made one step at a time, in any direction, with loops and repeats. ----
function quietUnselect(){redrawingTree=true;try{cy.elements().unselect();}finally{redrawingTree=false;}selectionOrder=[];boxedIds.clear();}
// builder.steps holds a clip id per step, or null for a step of an opened chain that has no take left; builder.gaps
// says which two images such a step joins. chainId and name are set once the chain is saved, or when it was opened
// from the saved list.
function startBuilder(start,steps=[]){beginBuilder({start,steps:steps.map(c=>c.id),gaps:new Map(),notes:[],chainId:null,name:''});}
function beginBuilder(state){
  stopBranchVideo();quietUnselect();
  // Clicks build the chain, so they select nothing while the builder is open.
  cy.autounselectify(true);
  builder=state;renderBuilder();
}
// Opens saved steps ({clip, start_key, end_key}) in the builder. A deleted take is replaced by the preferred take
// between the same two images; with none left the step stays as a gap. `saved` is the saved chain, if it is one.
function openChain(steps,saved=null){
  const nodeByKey=new Map(branchTree.nodes.filter(n=>n.label_key).map(n=>[n.label_key,n.id]));
  const ids=[],gaps=new Map(),notes=[];
  for(const [i,step] of steps.entries()){
    const clip=clipById.get(step.clip);
    if(clip){ids.push(clip.id);continue;}
    const source=nodeByKey.get(step.start_key),target=nodeByKey.get(step.end_key);
    if(!source||!target){notes.push(`Steps ${i+1} to ${steps.length} could not be opened: step ${i+1}'s clip was deleted and its images are no longer in this tree.`);break;}
    const pair=branchPairs.get(source+'>'+target);
    if(pair?.preferred){ids.push(pair.preferred.id);notes.push(`Step ${i+1}'s take was deleted, so it uses take ${pair.preferred.take} instead.`);}
    else{ids.push(null);gaps.set(i,{source,target});}
  }
  const first=clipById.get(ids[0])?.source||gaps.get(0)?.source;
  if(!first){notice('This chain cannot be opened: its first clip was deleted and its images are no longer in this tree.');return;}
  // A saved chain opened exactly as it was saved needs no saving.
  beginBuilder({start:first,steps:ids,gaps,notes,chainId:saved?.id||null,name:saved?.name||'',saved:!!saved&&!notes.length&&!gaps.size});
}
// The clip for each step, undefined for a gap.
function builderClips(){return builder.steps.map(id=>id&&clipById.get(id));}
function builderEnd(){
  const last=builder.steps.length-1;
  return last<0?builder.start:clipById.get(builder.steps[last])?.target||builder.gaps.get(last)?.target;
}
function markBuilder(){
  cy?.edges().removeClass('next-step');
  if(!builder)return;
  const end=builderEnd();
  for(const p of branchPairs.values())if(p.preferred&&p.source===end)cy.getElementById(p.id).addClass('next-step');
}
function addBuilderStep(p){
  quietUnselect();
  if(!p?.preferred){builderWarning('That connection has no finished take yet.');return;}
  if(p.source!==builderEnd()){builderWarning('The next step has to start where the chain ends. The connections you can add next are highlighted in blue.');return;}
  builder.steps.push(p.preferred.id);builderChanged();
}
// Shown in the builder's panel rather than at the top of the page, so the graph never shifts under the cursor.
function builderWarning(message){const box=$('#branch-info .builder-warning');box.textContent=message;box.hidden=!message;}
function stopBuilder(){builder=null;cy?.autounselectify(false);markBuilder();notice('');clearBranchPanel();}
function builderChanged(){builder.saved=false;renderBuilder();}
function renderBuilder(){
  stopBranchVideo();
  // A take that has arrived for a gap fills it.
  for(const [i,gap] of builder.gaps){
    const pair=branchPairs.get(gap.source+'>'+gap.target);
    if(pair?.preferred){builder.steps[i]=pair.preferred.id;builder.gaps.delete(i);builder.saved=false;}
  }
  markBuilder();
  const clips=builderClips(),present=clips.filter(Boolean),complete=clips.length>0&&present.length===clips.length;
  const start=branchTree.nodes.find(n=>n.id===builder.start);
  if(present.length){const poster=el('img');poster.src=present[0].thumbnail;poster.alt='';$('#branch-preview').replaceChildren(poster);}
  else if(start?.image){const img=el('img');img.src=start.image;img.alt='';$('#branch-preview').replaceChildren(img);}
  else $('#branch-preview').replaceChildren();
  const seconds=Math.round(present.reduce((sum,c)=>sum+(c.seconds||0),0)*10)/10;
  const caption=el('p','take-caption');
  const warning=el('p','builder-warning');warning.hidden=true;warning.setAttribute('role','status');
  const title=builder.chainId?builder.name.toUpperCase():'BUILDING A CHAIN';
  $('#branch-info').replaceChildren(el('span','eyebrow',`${title} · ${count(clips.length,'STEP','STEPS')} · ${seconds} S`),caption,warning);
  for(const note of builder.notes)$('#branch-info').append(el('p','muted',note));
  if(builder.gaps.size){
    const [i,gap]=[...builder.gaps][0];
    $('#branch-info').append(el('p','builder-warning',`Step ${i+1} (${nodeName(gap.source)} → ${nodeName(gap.target)}) has no take left, so the chain cannot play, export or be saved. A new take of that connection fills the gap when it finishes, while the chain is open here or the next time it is opened.`));
  }
  $('#branch-info').append(el('p','muted',clips.length?'Click a blue connection to add the next step. The same one can be added again and again.':'Click a blue connection to add the first step.'));
  const column=chainColumn(clips);
  $('#branch-info').append(column);column.scrollTop=column.scrollHeight;
  const play=actionButton('▶ Play chain',()=>playClips($('#branch-preview'),clips,{captionEl:caption,
    caption:i=>`Clip ${i+1} of ${clips.length} · ${nodeName(clips[i].source)} → ${nodeName(clips[i].target)}`,
    onShow:(clip,i)=>{highlightPlaying(clip);markStepPlaying(i);}}));
  const undo=actionButton('Undo last step',()=>{builder.steps.pop();builder.gaps.delete(builder.steps.length);builderChanged();});
  const clear=actionButton('Clear',()=>{builder.steps=[];builder.gaps.clear();builderChanged();});
  play.disabled=!complete;undo.disabled=clear.disabled=!clips.length;
  const grid=el('div','branch-grid');
  grid.append(play,complete?exportChainButton(clips):actionButton('Export chain ↓',()=>{}),undo,clear);
  if(!complete)grid.children[1].disabled=true;
  const done=actionButton('Exit Build Chain',stopBuilder);
  done.title='Leave the chain builder (Esc). Save, play or export the chain first; an unsaved chain is not kept.';
  $('#branch-actions').replaceChildren(saveChainRow(clips,complete),grid,done);
}
// Name and save the chain. A chain opened from the saved list is saved over; a new one is added to the list.
function saveChainRow(clips,complete){
  const row=el('div','chain-save');
  const name=el('input');name.maxLength=80;name.placeholder='Chain name';name.value=builder.name;name.setAttribute('aria-label','Chain name');
  const label=()=>builder.saved?'Saved ✓':builder.chainId?'Save changes':'Save chain';
  const save=actionButton(label(),async()=>{
    const body={name:builder.name,clips:clips.map(c=>c.id)};
    const chain=builder.chainId?await api('/branches/chains/'+builder.chainId,'PUT',body):await api('/branches/chains','POST',body);
    builder.chainId=chain.id;builder.name=chain.name;builder.saved=true;builder.notes=[];
    branchTree.chains=[chain,...(branchTree.chains||[]).filter(c=>c.id!==chain.id)];
    renderBuilder();
  });
  name.oninput=()=>{builder.name=name.value;builder.saved=false;save.textContent=label();};
  save.disabled=!complete;
  if(!complete)save.title='A chain with a missing step cannot be saved until the gap is filled.';
  row.append(name,save);return row;
}
// The chain being built, drawn down the side panel: every image it visits, in order, with the connection between
// each pair, its length and its take. An image visited twice appears twice, as the footage does.
function chainColumn(clips){
  const column=el('div','chain-column');
  const image=id=>{
    const n=branchTree.nodes.find(x=>x.id===id),row=el('div','column-node');
    if(n?.thumbnail){const img=el('img');img.src=n.thumbnail;img.alt='';row.append(img);}else row.append(el('div','blank'));
    row.append(el('span','',nodeName(id)));row.title=n?.prompt||'';return row;
  };
  column.append(image(builder.start));
  clips.forEach((clip,i)=>{
    if(!clip){
      const link=el('div','column-link missing');
      link.append(el('div','column-arrow'),el('span','','No take left'));
      column.append(link,image(builder.gaps.get(i).target));return;
    }
    const pair=pairByClip.get(clip.id),link=el('div','column-link');
    link.append(el('div','column-arrow'),el('span','',`${clip.seconds} s`));
    if(pair.takes.length>1){
      const pick=el('select');pick.setAttribute('aria-label',`Take for step ${i+1}`);
      for(const take of pair.takes){const o=el('option','',`Take ${take.take}`+(take===pair.preferred?' ★':''));o.value=take.id;pick.append(o);}
      pick.value=clip.id;pick.onchange=()=>{builder.steps[i]=pick.value;builderChanged();};
      link.append(pick);
    }else link.append(el('span','',`Take ${clip.take}`));
    column.append(link,image(clip.target));
  });
  return column;
}
// Highlights the step playing now and scrolls the chain (not the page) to keep it in view.
function markStepPlaying(index){
  const list=$('#branch-info .chain-column'),item=list?.querySelectorAll('.column-link')[index];
  if(!item)return;
  list.querySelectorAll('.column-link').forEach(link=>link.classList.toggle('playing',link===item));
  if(item.offsetTop<list.scrollTop||item.offsetTop+item.offsetHeight>list.scrollTop+list.clientHeight)
    list.scrollTop=item.offsetTop-list.clientHeight/2;
}
// Joins the chain into one video on this computer and saves it to the gallery.
function exportChainButton(clips){
  const button=actionButton('Export chain ↓',async()=>{
    button.disabled=true;button.textContent='Joining…';
    try{
      const joined=await api('/branches/sequence','POST',{clips:clips.map(c=>c.id)});
      const download=el('a','quiet','Download MP4');download.href=`/api/assets/${joined.id}/file`;download.download=joined.id+'.mp4';
      const saved=el('div','branch-saved');
      saved.append(el('p','muted','Saved to the gallery as one video.'),download,actionButton('View in gallery',()=>openAsset(joined)));
      $('#branch-actions').append(saved);refreshGallery();
    }finally{button.disabled=false;button.textContent='Export chain ↓';}
  });
  return button;
}
$('#export-selected').onclick=run(async()=>{
  // A selected connection stands for every take between its two images.
  const items=cy?cy.$(':selected').map(e=>e.id()).flatMap(id=>{const p=pairByEdge.get(id);return p?[...p.takes,...p.pending].map(c=>c.id):[id];}):[];
  if(!items.length){notice('Select images and connections to export first: shift-click them, or shift-drag a box around them.');return;}
  const button=$('#export-selected');button.disabled=true;button.textContent='Exporting…';
  try{
    const r=await fetch('/api/branches/export',{method:'POST',headers:{'X-PixAnvil':'local','Content-Type':'application/json'},body:JSON.stringify({items})});
    if(!r.ok){const data=await r.json().catch(()=>({}));throw new Error(typeof data.detail==='string'?data.detail:'Export failed');}
    // The server names the zip after the tree.
    // A name with spaces or other characters arrives encoded, as filename*=utf-8''...
    const disposition=r.headers.get('content-disposition')||'';
    const encoded=/filename\*=utf-8''([^;]+)/i.exec(disposition)?.[1],plain=/filename="?([^";]+)"?/i.exec(disposition)?.[1];
    const link=el('a');link.href=URL.createObjectURL(await r.blob());link.download=encoded?decodeURIComponent(encoded):plain||'branches.zip';
    link.click();setTimeout(()=>URL.revokeObjectURL(link.href),60000);notice('');
  }finally{button.disabled=false;button.textContent='Export selected';}
});
function chainHeading(clips){
  const seconds=Math.round(clips.reduce((sum,c)=>sum+(c.seconds||0),0)*10)/10;
  return el('span','eyebrow',`CHAIN · ${clips.length} CLIPS · ${seconds} S`);
}
// ---- A connection's panel: its takes, the preferred one playing, and its label. ----
function showBranchPair(p,playing=p?.preferred){
  if(!p)return;
  stopBranchVideo();
  if(playing){playOne($('#branch-preview'),playing);highlightPlaying(playing);}
  else $('#branch-preview').replaceChildren(el('p','muted',p.rendering?'The first take is rendering.':'The first take is queued.'));
  const onTheWay=[p.rendering?`${p.rendering} rendering`:'',p.queued?`${p.queued} queued`:''].filter(Boolean).join(' · ');
  $('#branch-info').replaceChildren(el('span','eyebrow',[count(p.takes.length,'take','takes'),pairName(p),onTheWay].filter(Boolean).join(' · ').toUpperCase()));
  if(playing)$('#branch-info').append(el('p','',playing.prompt));
  const actions=[];
  if(p.takes.length){
    $('#branch-info').append(pairLabelField(p),takeList(p,playing));
    const grid=el('div','branch-grid');
    grid.append(actionButton('▶ Play all takes',()=>playAllTakes(p)),actionButton('Open take gallery',()=>openTakeGallery(p)),
      actionButton('New take ↗',()=>newTake(p,playing||p.preferred)),actionButton('Build chain…',()=>startBuilder(p.source,[playing||p.preferred])));
    if(playing)grid.append(actionButton('View in gallery',async()=>openAsset(await api('/assets/'+playing.id))));
    actions.push(grid);
  }
  $('#branch-actions').replaceChildren(...actions);
}
// Stats label (automatic) or custom label (exactly what was typed). Switching back to stats keeps the typed text.
function pairLabelField(p){
  const mode=el('select');
  for(const [value,text] of [['stats','Stats label'],['custom','Custom label']]){const o=el('option','',text);o.value=value;mode.append(o);}
  mode.value=p.labelMode;
  const input=el('input');input.maxLength=60;input.placeholder='e.g. Leap';input.value=p.label;input.hidden=p.labelMode!=='custom';
  const save=run(async()=>{
    await api('/branches/pairs','PUT',{clip_id:p.takes[0].id,label_mode:mode.value,label:input.value});
    p.labelMode=mode.value;p.label=input.value.trim();cy?.getElementById(p.id).data('label',pairCaption(p));
  });
  mode.onchange=()=>{input.hidden=mode.value!=='custom';if(mode.value==='custom')input.focus();save();};
  input.onchange=save;
  const field=el('div','pair-label');field.append(el('span','','Label'),mode,input);return field;
}
function takeList(p,playing){
  const list=el('div','take-list');
  for(const take of p.takes){
    const row=el('div','take-row'+(take===playing?' playing':''));
    const play=el('button','take-play',`Take ${take.take} · ${take.seconds} s · ${take.prompt}`);play.type='button';play.title=take.prompt;
    play.onclick=()=>showBranchPair(p,take);
    const star=el('button','take-icon',take.preferred?'★':'☆');star.type='button';star.disabled=take.preferred;
    star.title=take.preferred?'The preferred take':'Make this the preferred take';star.setAttribute('aria-label',star.title);
    star.onclick=run(async()=>{await api('/branches/pairs','PUT',{clip_id:take.id,preferred:true});await reloadPair(p.key,take.id);});
    const remove=el('button','take-icon','🗑');remove.type='button';remove.title='Delete this take';remove.setAttribute('aria-label',`Delete take ${take.take}`);
    remove.onclick=run(async()=>{if(await deleteTakes([take]))await reloadPair(p.key,playing?.id);});
    row.append(play,star,remove);list.append(row);
  }
  return list;
}
function playAllTakes(p){
  stopBranchVideo();
  showBranchPair(p,null);
  const caption=el('p','take-caption');$('#branch-info').querySelector('.eyebrow').after(caption);
  const rows=[...$('#branch-info').querySelectorAll('.take-row')];
  playClips($('#branch-preview'),p.takes,{captionEl:caption,caption:(i,pass)=>`Take ${p.takes[i].take} (${i+1} of ${p.takes.length})`+(pass>1?' · looped':''),
    onShow:take=>{highlightPlaying(take);rows.forEach((row,i)=>row.classList.toggle('playing',p.takes[i]===take));}});
}
// Opens Generate with a take's settings, both of the pair's images and a new random seed, so what it makes lands
// on the same connection as another take.
function newTake(p,take){return generateBetween(p.source,p.target,take);}
// Opens Generate for a clip from one image to another (the same image for a loop), with a new random seed. Settings
// come from `base`, a clip, when given; otherwise from the start image, as Extend forward does. A frame not yet
// saved is saved first, as Extend does.
async function generateBetween(startId,endId,base){
  const ends=[branchNode(startId),branchNode(endId)];
  if(!ends.every(canConnect))throw new Error('A clip between two images needs both, and one of them was deleted or has no saved frame.');
  const start=await realNode(ends[0]),end=startId===endId?start:await realNode(ends[1]);
  const both=w=>w.start_image&&w.end_image;
  const fallback=workflows.find(w=>both(w)&&w.available)||workflows.find(both);
  let recipe;
  if(base){
    recipe=(await api('/assets/'+base.id)).recipe;
    recipe={...recipe,workflow:workflows.find(w=>w.id===recipe.workflow&&both(w))?recipe.workflow:fallback?.id||recipe.workflow};
  }else recipe={...start.recipe,workflow:fallback?.id||start.recipe.workflow,model:'',steps:null,cfg:null,width:null,height:null,frames:null,
    refinement:null,negative:'',emphasis:[],prompt:start.recipe.operation==='import'?'':start.recipe.prompt};
  endEditing(false);
  await restoreRecipe({...recipe,operation:'img2vid',source_asset_id:start.id,end_asset_id:end.id,seed:'-1',count:1,variation_seed:'-1'});
  $('#viewer').close();tab('generate');
  notice(toolCapabilities.img2vid?.available?'':toolCapabilities.img2vid?.issues?.join('; ')||'Connect to SPARK to animate images.');
}
// ---- Dragging a new connection off an image's side. ----
// The outer SIDE of an image's width at each side starts a connection instead of moving the image: drag off the
// right side to choose what comes after it, off the left side to choose what comes before. Dropping on an image
// (any part of it) connects the two; dropping on empty space opens Extend forward or backward. Nothing is queued:
// Generate opens filled in.
let connectDrag=null;
function branchNode(id){return branchTree?.nodes.find(n=>n.id===id);}
function canConnect(n){return !!n&&n.available&&['image','virtual'].includes(n.kind);}
function graphPosition(event){
  const box=$('#branch-graph').getBoundingClientRect(),pan=cy.pan(),zoom=cy.zoom();
  return {x:(event.clientX-box.left-pan.x)/zoom,y:(event.clientY-box.top-pan.y)/zoom};
}
// Which side of a node a graph position is on: 'right', 'left', or null for the middle or outside.
function sideAt(node,pos){
  const at=node.position(),dx=pos.x-at.x,half=NODE_W/2,inner=half-NODE_W*SIDE;
  if(Math.abs(pos.y-at.y)>NODE_H/2||Math.abs(dx)>half)return null;
  return dx>inner?'right':dx<-inner?'left':null;
}
function imageNodeAt(pos){
  return cy.nodes('.image, .virtual').filter(node=>{
    const at=node.position();return Math.abs(pos.x-at.x)<=NODE_W/2&&Math.abs(pos.y-at.y)<=NODE_H/2;
  })[0];
}
// Runs before Cytoscape sees the press, so an image pressed on its side is not picked up and moved.
function startConnectDrag(event){
  if(!cy||builder||event.button!==0||event.shiftKey)return;
  const pos=graphPosition(event),node=imageNodeAt(pos);
  const side=node&&canConnect(branchNode(node.id()))?sideAt(node,pos):null;
  if(!side)return;
  node.ungrabify();cy.userPanningEnabled(false);cy.boxSelectionEnabled(false);
  connectDrag={from:node.id(),side,start:pos,active:false,target:null};
}
function moveConnectDrag(pos){
  const drag=connectDrag;
  if(!drag.active){
    // A few pixels of movement first, so a click on a side still opens the image.
    if(Math.hypot(pos.x-drag.start.x,pos.y-drag.start.y)*cy.zoom()<6)return;
    drag.active=true;
    const fromRight=drag.side==='right';
    cy.add({group:'nodes',data:{id:'drag-end'},classes:'drag-end',position:{...pos},grabbable:false,selectable:false});
    cy.add({group:'edges',data:{id:'drag-line',source:fromRight?drag.from:'drag-end',target:fromRight?'drag-end':drag.from},
      classes:'drag-line '+(fromRight?'from-right':'from-left'),selectable:false});
    // Dragging off a right side lands on left sides, and the other way round; the same image counts, for a loop.
    const offset=(NODE_W-NODE_W*SIDE)/2*(fromRight?-1:1);
    for(const n of branchTree.nodes.filter(canConnect)){
      const at=cy.getElementById(n.id).position();
      cy.add({group:'nodes',data:{id:'drop:'+n.id,label:''},classes:'drop-zone',position:{x:at.x+offset,y:at.y},grabbable:false,selectable:false});
    }
    $('#branch-graph').style.cursor='crosshair';
  }
  cy.getElementById('drag-end').position({...pos});
  const over=imageNodeAt(pos);
  drag.target=over&&canConnect(branchNode(over.id()))?over.id():null;
  cy.nodes('.drop-zone').forEach(zone=>zone.toggleClass('hover',zone.id()==='drop:'+drag.target));
}
function endConnectDrag(){
  const drag=connectDrag;connectDrag=null;
  cy.remove('.drag-line, .drag-end, .drop-zone');
  cy.getElementById(drag.from).grabify();cy.userPanningEnabled(true);cy.boxSelectionEnabled(true);
  $('#branch-graph').style.cursor='';
  return drag;
}
async function dropConnectDrag(){
  const drag=endConnectDrag();
  if(!drag.active)return;
  const fromRight=drag.side==='right';
  if(drag.target){
    const [start,end]=fromRight?[drag.from,drag.target]:[drag.target,drag.from];
    // An existing connection gets a new take of its preferred one. A new connection starts from the settings of the
    // latest clip leaving its start image, else the clip that made that image.
    const pair=branchPairs.get(start+'>'+end);
    if(pair?.preferred)return newTake(pair,pair.preferred);
    const finished=branchTree.clips.filter(c=>!c.pending).sort((a,b)=>a.created.localeCompare(b.created));
    const base=finished.filter(c=>c.source===start).at(-1)||finished.find(c=>c.target===start);
    return generateBetween(start,end,base);
  }
  const node=await realNode(branchNode(drag.from));
  if(fromRight)startImageTool('img2vid',node);else extendBackwards(node);
}
async function deleteTakes(takes){
  const what=takes.length===1?`take ${takes[0].take}`:`${takes.length} takes`;
  if(!confirm(`Delete ${what}? This removes ${takes.length===1?'the clip and its job':'the clips and their jobs'} permanently.`))return false;
  for(const take of takes)await api('/assets/'+take.id,'DELETE');
  refreshGallery();return true;
}
// After a change to a pair: reload the tree, then show the pair again if it still has any clips.
async function reloadPair(key,playingId){
  branchSignature='';await branchTask(()=>loadTree(branchOpenId).catch(()=>loadTrees()));
  const pair=branchTree&&branchPairs.get(key);
  if(pair)showBranchPair(pair,pair.takes.find(t=>t.id===playingId)||pair.preferred);else clearBranchPanel();
}
// ---- Set viewer: every take of a pair in one dialog. Built to show a gallery batch the same way later. ----
let setView=null;
function openTakeGallery(p){
  const view=pair=>({title:['TAKES',pairName(pair)].filter(Boolean).join(' · '),items:pair.takes,preferredId:pair.preferred?.id,
    prefer:item=>api('/branches/pairs','PUT',{clip_id:item.id,preferred:true}),
    newTake:item=>newTake(pair,item),
    reload:async()=>{
      branchSignature='';await branchTask(()=>loadTree(branchOpenId).catch(()=>loadTrees()));
      const next=branchTree&&branchPairs.get(pair.key);return next?.takes.length?view(next):null;
    },
    closed:()=>{const next=branchTree&&branchPairs.get(pair.key);if(next)showBranchPair(next);else clearBranchPanel();}});
  stopBranchVideo();openSetViewer(view(p));
}
function openSetViewer(view,playingId){
  setView={...view,selected:new Set(),playing:view.items.find(i=>i.id===playingId)||view.items.find(i=>i.id===view.preferredId)||view.items[0]};
  renderSetViewer();playSetItem(setView.playing);
  if(!$('#set-viewer').open)$('#set-viewer').showModal();
}
function setNumber(item){return `Take ${item.take}`;}
function renderSetViewer(){
  const v=setView;
  $('#set-title').textContent=`${v.title} · ${count(v.items.length,'take','takes').toUpperCase()}`;
  $('#set-grid').replaceChildren(...v.items.map(item=>{
    const card=el('button','set-card');card.type='button';card.dataset.id=item.id;card.title=item.prompt;
    const img=el('img');img.src=item.thumbnail;img.alt='';img.loading='lazy';
    card.append(img,el('span','',`${setNumber(item)} · ${item.seconds} s`+(item.id===v.preferredId?' ★':'')));
    card.onclick=e=>{
      if(e.shiftKey||e.ctrlKey||e.metaKey){v.selected.has(item.id)?v.selected.delete(item.id):v.selected.add(item.id);renderSetActions();markSetCards();}
      else playSetItem(item);
    };
    return card;
  }));
  markSetCards();renderSetActions();
}
function markSetCards(){
  for(const card of $('#set-grid').children){
    card.classList.toggle('playing',card.dataset.id===setView.playing?.id);
    card.classList.toggle('selected',setView.selected.has(card.dataset.id));
  }
}
function renderSetActions(){
  const v=setView,item=v.playing,actions=[actionButton('▶ Play all takes',playAllInSet)];
  if(v.prefer&&item&&item.id!==v.preferredId)actions.push(actionButton('★ Make preferred',async()=>{await v.prefer(item);await reloadSet(item.id);}));
  if(v.newTake&&item)actions.push(actionButton('New take ↗',async()=>{closeSetViewer();await v.newTake(item);}));
  if(item)actions.push(actionButton('Delete',async()=>{if(await deleteTakes([item]))await reloadSet();}),
    actionButton('View in gallery',async()=>{closeSetViewer();openAsset(await api('/assets/'+item.id));}));
  if(v.selected.size){
    const chosen=v.items.filter(i=>v.selected.has(i.id));
    const remove=actionButton(`Delete selected (${chosen.length})`,async()=>{if(await deleteTakes(chosen))await reloadSet(item?.id);});
    remove.classList.add('danger');actions.push(remove);
  }
  $('#set-actions').replaceChildren(...actions);
}
function playSetItem(item){
  setView.playing=item;playOne($('#set-player'),item);
  $('#set-caption').textContent=`${setNumber(item)} · ${item.prompt}`;
  markSetCards();renderSetActions();
}
function playAllInSet(){
  const v=setView;
  playClips($('#set-player'),v.items,{captionEl:$('#set-caption'),caption:(i,pass)=>`${setNumber(v.items[i])} (${i+1} of ${v.items.length})`+(pass>1?' · looped':''),
    onShow:item=>{v.playing=item;markSetCards();renderSetActions();}});
}
async function reloadSet(playingId){
  const next=await setView.reload();
  if(!next){closeSetViewer();return;}
  openSetViewer(next,playingId);
}
function closeSetViewer(){if($('#set-viewer').open)$('#set-viewer').close();}
$('#close-set-viewer').onclick=closeSetViewer;
$('#set-viewer').addEventListener('close',()=>{stopVideos($('#set-player'));const v=setView;setView=null;v?.closed?.();});
function actionButton(label,handler){const b=el('button','quiet',label);b.type='button';b.onclick=run(handler);return b;}
// A virtual node is saved as a gallery image the first time something is built from it.
function realNode(n){return n.kind==='image'?api('/assets/'+n.asset_id):api(`/branches/nodes/${encodeURIComponent(n.id)}/real`,'POST');}
function showBranchNode(n){
  if(!n)return;
  stopBranchVideo();
  if(n.image){const img=el('img');img.src=n.image;img.alt=n.prompt;$('#branch-preview').replaceChildren(img);}
  else $('#branch-preview').replaceChildren(el('p','muted',{pending:'This clip is still rendering.',missing:'This image was deleted. The clips made from it are kept.'}[n.kind]||'This clip was made before last frames were saved, so its end cannot be extended from.'));
  const label=n.kind==='missing'?'DELETED IMAGE':n.kind==='pending'?'ON ITS WAY':n.kind==='virtual'?(n.frame==='last'?'LAST FRAME · NOT SAVED YET':'FIRST FRAME · NOT SAVED YET'):'IMAGE';
  $('#branch-info').replaceChildren(el('span','eyebrow',label),el('p','',n.prompt));
  if(n.label_key)$('#branch-info').append(labelField(n));
  const actions=[];
  if(n.available&&n.kind!=='pending'){
    // Two rows: build backward or forward, then look at the image or edit it into a new one.
    const gallery=actionButton('View in gallery',async()=>openAsset(await api('/assets/'+n.asset_id)));
    if(n.kind!=='image'){gallery.disabled=true;gallery.title='This frame is saved to the gallery once something is built from it.';}
    const grid=el('div','branch-grid');
    grid.append(actionButton('← Extend backward',async()=>extendBackwards(await realNode(n))),
      actionButton('Extend forward →',async()=>startImageTool('img2vid',await realNode(n))),
      gallery,actionButton('Edit into new ↗',async()=>startImageTool('edit',await realNode(n))));
    actions.push(grid);
  }
  if(n.kind!=='pending'&&[...branchPairs.values()].some(p=>p.preferred&&p.source===n.id))
    actions.push(actionButton('Build chain from here…',()=>startBuilder(n.id)));
  if(n.added&&!branchTree.clips.some(c=>c.source===n.id||c.target===n.id))
    actions.push(actionButton('Remove from Branches',async()=>{await api('/branches/images/'+n.asset_id,'DELETE');branchOpenId=null;await showBranches();}));
  $('#branch-actions').replaceChildren(...actions);
}
// A short name such as A1, shown on the node and used for exported file names.
function labelField(n){
  const input=el('input');input.maxLength=40;input.placeholder=n.default_label||'e.g. A1';input.value=n.label||'';
  input.onchange=run(async()=>{
    await api('/branches/labels/'+encodeURIComponent(n.label_key),'PUT',{label:input.value});
    n.label=input.value.trim();cy?.getElementById(n.id).data('label',nodeCaption(n));
  });
  const field=el('label','branch-label','Label');field.append(input);return field;
}
// Building backwards: the node becomes the clip's end image, and the clip has no start image.
function extendBackwards(asset){
  endEditing(false);
  const w=workflows.find(w=>w.end_image&&w.available)||workflows.find(w=>w.end_image);
  sourceAsset=null;endAsset=asset;
  loadRecipe({...asset.recipe,operation:'img2vid',workflow:w?.id||asset.recipe.workflow,model:'',source_asset_id:null,end_asset_id:asset.id,
    prompt:asset.recipe.operation==='import'?'':asset.recipe.prompt,steps:null,cfg:null,width:null,height:null,frames:null,refinement:null,
    negative:'',emphasis:[],seed:'-1',count:1,variation_seed:'-1'});
  $('#viewer').close();tab('generate');
  notice(toolCapabilities.img2vid?.available?'':toolCapabilities.img2vid?.issues?.join('; ')||'Connect to SPARK to animate images.');
}
async function updateBranchButton(a){
  const button=$('#branch-image');button.hidden=true;
  // A joined chain is not a step in a tree; it shows the clips it was made from, while they still exist.
  if(a.recipe.operation==='sequence'){
    // Older exports kept only their clip ids, so a deleted clip in one cannot be replaced by another take.
    const steps=a.recipe.chain||(a.recipe.clips||[]).map(clip=>({clip}));
    let found=null;
    for(const step of steps){
      for(const id of [step.clip,step.start_key].filter(Boolean)){found=await api('/branches/'+encodeURIComponent(id)).catch(()=>null);if(found)break;}
      if(found)break;
    }
    if(selected?.id!==a.id||!found)return;
    button.textContent='Open chain in builder';
    button.onclick=()=>{$('#viewer').close();openInBranches(found.id,[],{steps});};
    button.hidden=false;return;
  }
  const found=await api('/branches/'+a.id).catch(()=>null);
  if(selected?.id!==a.id)return;
  if(found){button.textContent='Show in branches';button.onclick=()=>{$('#viewer').close();openInBranches(found.id);};}
  else{
    button.textContent='Add to Branches';
    // A clip joins through its first frame; an image is listed as a tree of its own.
    button.onclick=run(async()=>{
      const id=isVideo(a)?(await api(`/branches/nodes/${encodeURIComponent(a.id+':first')}/real`,'POST')).id:(await api('/branches/images','POST',{asset_id:a.id})).tree;
      $('#viewer').close();await refreshGallery();openInBranches(id);
    });
  }
  button.hidden=false;
}
$('#back-to-trees').onclick=()=>{branchOpenId=null;run(showBranches)();};
$('#fit-tree').onclick=()=>cy?.fit(undefined,40);
$('#tidy-tree').onclick=run(async()=>{
  if(!branchTree||!confirm('Lay out this whole tree again? Positions you set by hand are replaced.'))return;
  await api('/branches/layout','PUT',{positions:Object.fromEntries(branchTree.nodes.map(n=>[n.id,null]))});
  branchTree.positions={};await branchTask(()=>drawTree(branchTree,true));
});
$('#tree-name').onchange=run(async()=>{if(!branchTree)return;await api(`/branches/${encodeURIComponent(branchTree.id)}/name`,'PUT',{name:$('#tree-name').value});branchSignature='';await showBranches();});
document.addEventListener('keydown',e=>{
  if(e.key==='Escape'&&builder&&!document.querySelector('dialog[open]')&&!$('#branches').hidden){stopBuilder();return;}
  if((e.key==='f'||e.key==='F')&&!e.ctrlKey&&!e.metaKey&&!e.altKey&&cy&&!$('#tree-view').hidden&&!$('#branches').hidden
     &&!e.target.closest('input,textarea,select')&&!document.querySelector('dialog[open]'))cy.fit(undefined,40);
});
async function connection(){const c=await api('/connection');$('#connection-dot').classList.toggle('ready',c.state==='ready');$('#connection-label').textContent=c.state==='ready'?'Connected · ready':c.state==='starting'?'Starting ComfyUI…':'Unavailable';$('#start-comfy').hidden=c.state==='ready';$('#start-comfy').disabled=c.state==='starting';$('#connection-detail').textContent=c.state==='ready'?`Connected to ${c.url}`:(c.startup?.message||c.error||'');if(c.state==='ready'&&!workflows.length)await loadWorkflows();}
async function loadWorkflows(){try{workflows=(await api('/workflows')).items;toolCapabilities=await api('/image-tools');$('#upscale-model').replaceChildren(...(toolCapabilities.upscale?.models||[]).filter(m=>m==='RealESRGAN_x4plus.pth').map(m=>{const o=el('option','',m);o.value=m;return o;}));setMode(mode(),$('#workflow').value||'flux-dev');setModelOptions($('#workflow').value);refinementModels();const draft=localStorage.getItem('pixanvil-draft');// A fresh visit always starts with a random seed; a fixed seed lasts only until the page is reloaded.
  if(draft)await restoreRecipe({...JSON.parse(draft),seed:'-1'});}catch(e){$('#workflow-description').textContent=e.message;$('#generate-button').disabled=true;}}
$('#start-comfy').onclick=run(async()=>{await api('/connection/start','POST');notice('Starting ComfyUI on SPARK. Status will update here.');await connection();});
$('#settings-form').onsubmit=run(async e=>{e.preventDefault();await api('/settings','PUT',{library:$('#library').value,comfy_url:$('#comfy-url').value,ssh_host:$('#ssh-host').value});workflows=[];notice('Settings saved. Existing images remain linked to their original files.');await connection();});$('#discover').onclick=run(async()=>{const d=await api('/discover');$('#discovery-result').textContent=d.addresses.length?`${d.hostname} → ${d.addresses.join(', ')}`:d.error;if(d.addresses.length){$('#comfy-url').value=`http://${d.hostname}:8188`;$('#ssh-host').value=`exordium@${d.hostname}`;}});
async function readMetadata(file){const form=new FormData();form.append('file',file);metadata=await api('/inspect','POST',form);$('#metadata-content').textContent=JSON.stringify(metadata.recipe||metadata.raw,null,2);$('#metadata-notice').textContent=metadata.notice||`Recovered ${metadata.format} settings. Review before generating.`;$('#load-metadata').hidden=!metadata.recipe;$('#metadata-dialog').showModal();}
$('#metadata-file').onchange=run(e=>{const file=pickedFile(e);if(file)return readMetadata(file);});dropZone($('#metadata-drop'),readMetadata);$('#close-metadata').onclick=()=>$('#metadata-dialog').close();$('#load-metadata').onclick=run(async()=>{endEditing(false);await restoreRecipe(metadata.recipe);$('#metadata-dialog').close();});
async function init(){tab(location.hash.slice(1)||'gallery');const settings=await api('/settings');$('#library').value=settings.library;$('#comfy-url').value=settings.comfy_url;$('#ssh-host').value=settings.ssh_host;await Promise.all([refreshGallery(),refreshPresets(),refreshJobs(),connection()]);setInterval(()=>refreshJobs().catch(()=>{}),2000);setInterval(()=>connection().catch(()=>{}),10000);}
run(init)();
