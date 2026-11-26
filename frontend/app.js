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
  $('#duration').replaceChildren(...videoLengths(w).map(frames=>{const o=el('option','',`${(frames/w.defaults.fps).toFixed(1)} s · ${frames} frames`);o.value=frames;return o;}));
  if(w?.defaults?.frames)$('#duration').value=w.defaults.frames;
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
function loadRecipe(r){let wf=r.workflow&&workflows.some(w=>w.id===r.workflow)?r.workflow:null;if(!wf&&r.model&&r.operation!=='upscale'){const match=workflows.find(w=>w.models.includes(r.model));if(match)wf=match.id;else notice('The imported model is unavailable. Select a compatible installed model before generating.');}const source=sourceAsset,end=endAsset;setMode(modeOf({...r,workflow:wf}),wf);sourceAsset=r.operation&&r.operation!=='txt2img'?source:null;endAsset=r.operation==='img2vid'?end:null;setModelOptions($('#workflow').value,r.model);$('#prompt').value=r.prompt||'';const w=workflows.find(w=>w.id===$('#workflow').value);$('#negative').value=w?.negative?(r.negative||''):'';$('#seed').value=r.seed??'-1';if(r.width&&r.height&&r.operation!=='edit')setDimensions(r.width,r.height);if(r.frames){if(![...$('#duration').options].some(o=>o.value===String(r.frames))){const o=el('option','',`${(r.frames/(r.fps||24)).toFixed(1)} s · ${r.frames} frames`);o.value=r.frames;$('#duration').append(o);}$('#duration').value=r.frames;}$('#quality').value=Math.max(0,profiles.indexOf(r.quality||'quality'));$('#steps').value=r.operation==='upscale'?'':r.steps??'';$('#cfg').value=r.operation==='upscale'?'':r.cfg??'';$('#count').value=r.count||1;$('#emphasis-text').value=w?.emphasis?(r.emphasis?.[0]?.text||''):'';$('#emphasis-weight').value=r.emphasis?.[0]?.weight??1;
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
let cy=null,branchTree=null,branchOpenId=null,branchSignature='',positionQueue={},positionTimer=null;
// The clip a playing chain is on, kept so a redraw can highlight it again; redrawingTree hides the redraw's own reselection.
let chainPlaying=null,redrawingTree=false,chainTimer=null,branchFocus=[];
// Loading and drawing run one at a time, so a poll arriving mid-draw cannot build the graph twice.
let branchWork=Promise.resolve();
function branchTask(fn){branchWork=branchWork.catch(()=>{}).then(fn);return branchWork;}
function showBranches(){return branchTask(()=>branchOpenId?loadTree(branchOpenId).catch(()=>loadTrees()):loadTrees());}
function openInBranches(id,focus=[]){branchOpenId=id;branchFocus=focus;tab('branches');}
async function loadTrees(){
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
  $('#tree-summary').textContent=`${count(tree.clips.filter(c=>!c.pending).length,'clip','clips')} · ${tree.seconds} s of footage`+(tree.pending?` · ${tree.pending} rendering`:'');
  const signature=JSON.stringify([tree.nodes,tree.clips]);
  if(fresh||signature!==branchSignature){branchSignature=signature;await drawTree(tree,fresh);}
  else cy?.resize();
  // Opened to show particular items, such as the clips a joined chain was made from.
  if(branchFocus.length&&cy){
    const focus=branchFocus;branchFocus=[];
    cy.elements().unselect();for(const item of focus)cy.getElementById(item).select();
  }
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
function branchStyle(){
  const accent=getComputedStyle(document.documentElement).getPropertyValue('--accent').trim();
  return [
    {selector:'node',style:{shape:'round-rectangle',width:NODE_W,height:NODE_H,'background-color':'#252922','border-width':2,'border-color':'#3b4235',
      label:'data(label)',color:'#cbd0c3','font-size':10,'text-valign':'bottom','text-margin-y':6,'text-wrap':'ellipsis','text-max-width':NODE_W}},
    {selector:'node[thumb]',style:{'background-image':'data(thumb)','background-fit':'cover'}},
    {selector:'node.virtual',style:{opacity:.55,'border-style':'dashed'}},
    {selector:'node.pending',style:{'border-style':'dashed','text-valign':'center','text-margin-y':0}},
    {selector:'node.missing',style:{'border-style':'dashed','border-color':'#a5523f','text-valign':'center','text-margin-y':0}},
    {selector:'node:selected',style:{'border-color':accent,'border-width':3,opacity:1}},
    {selector:'edge',style:{'curve-style':'bezier','control-point-step-size':40,width:3,'line-color':'#5b6450','target-arrow-shape':'triangle','target-arrow-color':'#5b6450',
      label:'data(label)',color:'#cbd0c3','font-size':10,'text-background-color':'#101210','text-background-opacity':1,'text-background-padding':3}},
    {selector:'edge.pending',style:{'line-style':'dashed'}},
    {selector:'edge:selected',style:{'line-color':accent,'target-arrow-color':accent,width:4}},
    {selector:'edge.playing',style:{'line-color':'#f4f1e8','target-arrow-color':'#f4f1e8',width:6}},
  ];
}
// The text under a node: its label, and its state when it has no picture.
function nodeCaption(n){
  const state={pending:'Rendering…',missing:'Deleted image'}[n.kind]||(n.available?'':'No saved frame');
  return [n.label,state].filter(Boolean).join(' · ');
}
async function drawTree(tree,fit){
  const positions=await placeNodes(tree);
  const elements=[
    ...tree.nodes.map(n=>({group:'nodes',classes:n.kind,position:{...positions[n.id]},
      data:{id:n.id,label:nodeCaption(n),...(n.thumbnail?{thumb:n.thumbnail}:{})}})),
    ...tree.clips.map(c=>({group:'edges',classes:c.pending?'pending':'',data:{id:c.id,source:c.source,target:c.target,label:c.pending?c.state:`${c.seconds} s`}})),
  ];
  if(!cy){
    cy=cytoscape({container:$('#branch-graph'),elements,layout:{name:'preset'},style:branchStyle(),boxSelectionEnabled:true,maxZoom:3,minZoom:.1});
    cy.on('tap','node',e=>showBranchNode(branchTree.nodes.find(n=>n.id===e.target.id())));
    cy.on('tap','edge',e=>showBranchClip(branchTree.clips.find(c=>c.id===e.target.id())));
    cy.on('tap',e=>{if(e.target===cy)clearBranchPanel();});
    // Selecting several items offers them as a chain. Runs after the tap handlers, so it wins over a single item's panel.
    cy.on('select unselect',()=>{if(redrawingTree)return;clearTimeout(chainTimer);chainTimer=setTimeout(showChainSelection,0);});
    // Dropped nodes keep their place; a dragged selection saves in one request.
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
    if(!fit)cy.viewport(viewport);
  }
  if(fit){clearBranchPanel();cy.fit(undefined,40);}
}
const BRANCH_HINT='Click an image or a clip. Shift-click clips, or two images, to play a chain. Drag to move, scroll to zoom, F to fit.';
function clearBranchPanel(){
  stopBranchVideo();
  $('#branch-preview').replaceChildren(el('p','muted',BRANCH_HINT));
  $('#branch-info').replaceChildren();$('#branch-actions').replaceChildren();
}
function stopBranchVideo(){
  document.querySelectorAll('#branch-preview video').forEach(v=>{v.onended=null;v.pause();});
  cy?.edges().removeClass('playing');chainPlaying=null;
}
// ---- Chains: clips that each start where the previous one ended, played back to back. ----
// From the selection: the selected clips in order, or else the one route between two selected images.
function chainFromSelection(tree,ids){
  const clips=tree.clips.filter(c=>ids.includes(c.id)),nodes=tree.nodes.filter(n=>ids.includes(n.id));
  if(clips.length){
    if(clips.some(c=>c.pending))return {error:'A selected clip is still rendering.'};
    return orderClips(clips);
  }
  if(nodes.length===2){
    const [left,right]=[...nodes].sort((a,b)=>cy.getElementById(a.id).position('x')-cy.getElementById(b.id).position('x'));
    return routeBetween(tree,left.id,right.id)||routeBetween(tree,right.id,left.id)||{error:'No finished clips lead from one of these images to the other.'};
  }
  return {error:'To play a chain, select clips that follow on from each other, or two images with a route between them.'};
}
function orderClips(clips){
  const broken={error:'The selected clips do not connect end to start. Select clips where each one starts on the image the previous one ends on.'};
  const from=new Map();
  for(const c of clips){
    if(from.has(c.source))return {error:'The selected clips split into more than one path from one image. Select a single route.'};
    from.set(c.source,c);
  }
  const targets=new Set(clips.map(c=>c.target)),starts=clips.filter(c=>!targets.has(c.source));
  if(starts.length>1)return broken;
  // A selection that loops all the way round starts from its oldest clip.
  let clip=starts[0]||[...clips].sort((a,b)=>a.created.localeCompare(b.created))[0];
  const order=[];
  while(clip&&!order.includes(clip)){order.push(clip);clip=from.get(clip.target);}
  return order.length===clips.length?{clips:order}:broken;
}
// The shortest route of finished clips from one node to another, or null. Two equally short routes are ambiguous.
function routeBetween(tree,start,end){
  if(start===end)return null;
  const finished=tree.clips.filter(c=>!c.pending);
  const depth=new Map([[start,0]]),routes=new Map([[start,1]]),via=new Map(),queue=[start];
  while(queue.length){
    const node=queue.shift();
    for(const c of finished.filter(c=>c.source===node)){
      if(!depth.has(c.target)){depth.set(c.target,depth.get(node)+1);routes.set(c.target,0);via.set(c.target,c);queue.push(c.target);}
      if(depth.get(c.target)===depth.get(node)+1)routes.set(c.target,routes.get(c.target)+routes.get(node));
    }
  }
  if(!depth.has(end))return null;
  if(routes.get(end)>1)return {error:'More than one route joins these images. Shift-click the clips you want instead.'};
  const order=[];
  for(let node=end;node!==start;node=via.get(node).source)order.unshift(via.get(node));
  return {clips:order};
}
function showChainSelection(){
  if(!cy||!branchTree)return;
  const ids=cy.$(':selected').map(e=>e.id());
  if(ids.length<2)return;
  stopBranchVideo();
  const chain=chainFromSelection(branchTree,ids);
  $('#branch-actions').replaceChildren();
  if(chain.error){
    $('#branch-preview').replaceChildren(el('p','muted',chain.error));
    $('#branch-info').replaceChildren(el('span','eyebrow',`${ids.length} SELECTED`));
    return;
  }
  const poster=el('img');poster.src=chain.clips[0].thumbnail;poster.alt=chain.clips[0].prompt;
  $('#branch-preview').replaceChildren(poster);
  $('#branch-info').replaceChildren(chainHeading(chain.clips),el('p','muted','Plays the clips back to back. Each join may hitch slightly in the browser.'));
  const grid=el('div','branch-grid');
  grid.append(actionButton('▶ Play chain',()=>playChain(chain.clips)),exportChainButton(chain.clips));
  $('#branch-actions').replaceChildren(grid);
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
  const items=cy?cy.$(':selected').map(e=>e.id()):[];
  if(!items.length){notice('Select images and clips to export first: shift-click them, or shift-drag a box around them.');return;}
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
// Two players take turns: while one plays, the other loads the next clip, so each join waits as little as possible.
function playChain(clips){
  stopBranchVideo();
  const players=[0,1].map(()=>{const v=el('video');v.controls=true;v.playsInline=true;v.preload='auto';v.hidden=true;return v;});
  const caption=el('p','');
  $('#branch-preview').replaceChildren(...players);
  $('#branch-info').replaceChildren(chainHeading(clips),caption);
  $('#branch-actions').replaceChildren(actionButton('↺ Restart chain',()=>playChain(clips)));
  let active=0,index=0;
  const load=(player,i)=>{player.poster=clips[i].thumbnail;player.src=clips[i].file;};
  const show=()=>{
    const current=players[active],other=players[1-active];
    other.pause();other.hidden=true;current.hidden=false;
    current.play().catch(()=>{});
    load(other,(index+1)%clips.length);
    caption.textContent=`Clip ${index+1} of ${clips.length} · ${clips[index].prompt}`;
    cy.edges().removeClass('playing');cy.getElementById(clips[index].id).addClass('playing');chainPlaying=clips[index].id;
  };
  // The chain loops, like a single clip does.
  for(const player of players)player.onended=()=>{if(player!==players[active])return;active=1-active;index=(index+1)%clips.length;show();};
  load(players[0],0);show();
}
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
  if(n.added&&!branchTree.clips.some(c=>c.source===n.id||c.target===n.id))
    actions.push(actionButton('Remove from Branches',async()=>{await api('/branches/images/'+n.asset_id,'DELETE');branchOpenId=null;await showBranches();}));
  $('#branch-actions').replaceChildren(...actions);
}
// A short name such as A1, shown on the node and used for exported file names.
function labelField(n){
  const input=el('input');input.maxLength=40;input.placeholder='e.g. A1';input.value=n.label||'';
  input.onchange=run(async()=>{
    await api('/branches/labels/'+encodeURIComponent(n.label_key),'PUT',{label:input.value});
    n.label=input.value.trim();cy?.getElementById(n.id).data('label',nodeCaption(n));
  });
  const field=el('label','branch-label','Label');field.append(input);return field;
}
function showBranchClip(c){
  if(!c)return;
  stopBranchVideo();
  if(c.pending){$('#branch-preview').replaceChildren(el('p','muted','This clip is '+c.state+'.'));}
  else{
    const video=el('video');video.controls=true;video.loop=true;video.playsInline=true;video.poster=c.thumbnail;video.src=c.file;
    $('#branch-preview').replaceChildren(video);video.play().catch(()=>{});
  }
  $('#branch-info').replaceChildren(el('span','eyebrow',`CLIP · ${c.seconds} S · ${c.workflow}`),el('p','',c.prompt));
  $('#branch-actions').replaceChildren(...(c.pending?[]:[actionButton('View in gallery',async()=>openAsset(await api('/assets/'+c.id)))]));
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
    const clips=a.recipe.clips||[];
    const found=clips.length?await api('/branches/'+clips[0]).catch(()=>null):null;
    if(selected?.id!==a.id||!found)return;
    button.textContent='Show chain in branches';
    button.onclick=()=>{$('#viewer').close();openInBranches(found.id,clips);};
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
