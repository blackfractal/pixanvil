const $=s=>document.querySelector(s);
const el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;};
let workflows=[],presets=[],assets=[],selected=null,metadata=null,favorites=false,offset=0,total=0,lastSignature='',busy=false;
let sourceAsset=null,endAsset=null,toolCapabilities={};
let activeJobs=[],latestId='';
const profiles=['fast','balanced','quality','maximum'];
const titles={gallery:'Your image collection',live:'Watch it take shape',generate:'Give an idea its shape',history:'Every step, remembered',settings:'Make yourself at home'};
async function api(path,method='GET',body){const options={method,headers:{'X-PixAnvil':'local'}};if(body instanceof FormData)options.body=body;else if(body!==undefined){options.headers['Content-Type']='application/json';options.body=JSON.stringify(body);}const r=await fetch('/api'+path,options);const data=await r.json();if(!r.ok)throw new Error(typeof data.detail==='string'?data.detail:JSON.stringify(data.detail));return data;}
function notice(message){$('#notice').hidden=!message;$('#notice').textContent=message;}
function run(fn){return async(...args)=>{try{await fn(...args);}catch(e){notice(e.message);}};}
function tab(name){if(!titles[name])name='gallery';document.querySelectorAll('.page').forEach(p=>p.hidden=p.id!==name);document.querySelectorAll('.nav').forEach(b=>b.classList.toggle('active',b.dataset.tab===name));$('#section-label').textContent=name.toUpperCase();$('#page-title').replaceChildren(document.createTextNode(titles[name]),el('span','orange','.'));$('#new-image').hidden=name==='generate';location.hash=name;}
document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>tab(b.dataset.tab));
$('#new-image').onclick=$('#first-image').onclick=()=>tab('generate');
window.addEventListener('hashchange',()=>tab(location.hash.slice(1)));
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
  $('#duration').replaceChildren(...(w?.durations||[]).map(d=>{const o=el('option','',`${d.seconds} seconds`);o.value=d.frames;return o;}));
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
function saveDraft(){localStorage.setItem('pixanvil-draft',JSON.stringify(data()));}
function setModelOptions(workflow,model){const w=workflows.find(w=>w.id===workflow);$('#model').replaceChildren(...(w?.models||[]).map(m=>{const o=el('option','',m);o.value=m;return o;}));if(model&&w?.models.includes(model))$('#model').value=model;$('#workflow-description').textContent=w?(w.available?w.description:w.issues.join('; ')):'Connect to SPARK to discover workflows.';$('#negative-label').hidden=!w?.negative;$('#emphasis-section').hidden=!w?.emphasis;$('#negative').disabled=!w?.negative;$('#emphasis-text').disabled=!w?.emphasis;if(!w?.negative)$('#negative').value='';if(!w?.emphasis)$('#emphasis-text').value='';setWorkflowOptions(w);updateOperation();}
function loadRecipe(r){let wf=r.workflow&&workflows.some(w=>w.id===r.workflow)?r.workflow:null;if(!wf&&r.model&&r.operation!=='upscale'){const match=workflows.find(w=>w.models.includes(r.model));if(match)wf=match.id;else notice('The imported model is unavailable. Select a compatible installed model before generating.');}const source=sourceAsset,end=endAsset;setMode(modeOf({...r,workflow:wf}),wf);sourceAsset=r.operation&&r.operation!=='txt2img'?source:null;endAsset=r.operation==='img2vid'?end:null;setModelOptions($('#workflow').value,r.model);$('#prompt').value=r.prompt||'';const w=workflows.find(w=>w.id===$('#workflow').value);$('#negative').value=w?.negative?(r.negative||''):'';$('#seed').value=r.seed??'-1';if(r.width&&r.height&&r.operation!=='edit')setDimensions(r.width,r.height);if(r.frames){if(![...$('#duration').options].some(o=>o.value===String(r.frames))){const o=el('option','',`${r.frames} frames`);o.value=r.frames;$('#duration').append(o);}$('#duration').value=r.frames;}$('#quality').value=Math.max(0,profiles.indexOf(r.quality||'quality'));$('#steps').value=r.operation==='upscale'?'':r.steps??'';$('#cfg').value=r.operation==='upscale'?'':r.cfg??'';$('#count').value=r.count||1;$('#emphasis-text').value=w?.emphasis?(r.emphasis?.[0]?.text||''):'';$('#emphasis-weight').value=r.emphasis?.[0]?.weight??1;
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
  showSlot('source',sourceAsset,video?!!w?.start_image:m!=='image',video?'START IMAGE · OPTIONAL':op==='edit'?'IMAGE TO CHANGE':'SOURCE IMAGE',video?'The clip opens on this image. Choose a file, or open an image in Gallery and choose Animate.':'Choose a file, or open an image in Gallery and use its buttons.');
  showSlot('end',endAsset,video&&!!w?.end_image,'END IMAGE · OPTIONAL','The clip finishes on this image. Use it alone or together with a start image.');
  $('#variation-controls').hidden=!variation;$('#denoise-controls').hidden=op!=='refine';$('#upscale-controls').hidden=!upscale;
  $('#direction-controls').hidden=upscale;$('#sampler-controls').hidden=upscale;$('#prompt').disabled=upscale;
  for(const id of ['seed','workflow','model','dimensions'])$('#'+id).disabled=variation;
  $('#dimensions').disabled=variation||op==='edit';
  $('#refinement-controls').hidden=!$('#refinement-enabled').checked;
  const labels={txt2img:'Generate image ↗',variation:'Generate variation ↗',refine:'Refine image ↗',upscale:'Upscale image ↗',img2vid:'Animate image ↗',edit:'Edit image ↗'};
  $('#generate-button').textContent=video?'Generate video ↗':labels[op];$('#operation-caption').textContent=op==='txt2img'?'Create something new':video?'Source images preserved · clip saved separately':'Original preserved · result saved separately';
  const feature=toolCapabilities[op],ref=workflows.find(w=>w.id===$('#refinement-workflow').value);
  $('#generate-button').disabled=busy||(op!=='txt2img'&&(!(sourceAsset||endAsset)||!feature?.available))||(!upscale&&!w?.available)||(op==='refine'&&(w?.id==='flux-schnell'||w?.media==='video'))||(op==='img2vid'&&!w?.start_image)||(op==='edit'&&!w?.source_image)||(['txt2img','refine'].includes(op)&&!!w?.source_image)||(!upscale&&!video&&$('#refinement-enabled').checked&&!ref?.available);
  $('#upscale-description').textContent=sourceAsset?`Output: ${sourceAsset.width*Number($('#upscale-factor').value)} × ${sourceAsset.height*Number($('#upscale-factor').value)}. RealESRGAN renders at 4×, then resizes for a 2× result.`:'';
  for(const [id,label] of [['variation-strength','variation-label'],['denoise','denoise-label'],['refinement-denoise','refinement-label']])$('#'+label).textContent=Number($('#'+id).value).toFixed(2);
}
// A source that has since left the library is dropped; the rest of the settings still load.
const findAsset=id=>id?api('/assets/'+id).catch(()=>null):null;
async function restoreRecipe(r){sourceAsset=await findAsset(r.source_asset_id);endAsset=await findAsset(r.end_asset_id);loadRecipe(r);}
async function startImageTool(op,asset=selected){
  sourceAsset=asset;endAsset=null;const r={...asset.recipe,operation:op,source_asset_id:asset.id,end_asset_id:null,variation_seed:'-1',count:1};
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
async function attachFile(e,slot){
  const file=e.target.files[0];if(!file)return;e.target.value='';
  const form=new FormData();form.append('file',file);const asset=await api('/assets/import','POST',form);
  if(slot==='end')endAsset=asset;
  else{sourceAsset=asset;if(mode()==='refine')fitRefineCanvas(asset);}
  updateOperation();saveDraft();await refreshGallery();
}
$('#source-file').onchange=run(e=>attachFile(e,'source'));$('#end-file').onchange=run(e=>attachFile(e,'end'));
$('#clear-end').onclick=()=>{endAsset=null;updateOperation();saveDraft();};
$('#clear-source').onclick=()=>{sourceAsset=null;if(mode()==='variation'){setMode('image');setModelOptions($('#workflow').value);}updateOperation();saveDraft();};
for(const id of ['variation-strength','denoise','refinement-denoise','upscale-factor','refinement-enabled'])$('#'+id).oninput=()=>{updateOperation();saveDraft();};
$('#refinement-workflow').onchange=()=>{refinementModels();updateOperation();saveDraft();};
function updateLabels(){$('#quality-label').textContent=profiles[+$('#quality').value].replace(/^./,c=>c.toUpperCase());$('#weight-label').textContent=Number($('#emphasis-weight').value).toFixed(2)+'×';}
$('#quality').oninput=()=>{updateLabels();$('#steps').value='';$('#cfg').value='';saveDraft();};$('#emphasis-weight').oninput=()=>{updateLabels();saveDraft();};$('#workflow').onchange=()=>{if(videoMode())$('#refinement-enabled').checked=false;setModelOptions($('#workflow').value);$('#steps').value='';$('#cfg').value='';saveDraft();};
$('#generation-form').addEventListener('input',()=>{if(workflows.length)saveDraft();});
$('#example-prompt').onclick=()=>{$('#prompt').value='Create a high quality image of a young, handsome teenage water knight casting a powerful water spell. Sleeveless armor, cinematic lighting';saveDraft();};
$('#generation-form').onsubmit=run(async e=>{e.preventDefault();busy=true;$('#generate-button').disabled=true;try{const response=await api('/jobs','POST',data());notice(`Queued ${response.jobs.length} ${response.jobs[0].recipe.media==='video'?'video':'image'}(s). You can keep browsing; results save automatically.`);await refreshJobs();}finally{busy=false;setModelOptions($('#workflow').value,$('#model').value);}});
async function refreshGallery(append=false){if(!append)offset=0;const response=await api(`/assets?limit=48&offset=${offset}&favorites=${favorites}&q=${encodeURIComponent($('#search').value)}`);total=response.total;assets=append?[...assets,...response.items]:response.items;offset=assets.length;$('#image-count').textContent=`${total} image${total===1?'':'s'}`;$('#empty-gallery').hidden=total>0;$('#load-more').hidden=offset>=total;const grid=$('#gallery-grid');grid.replaceChildren();for(const a of assets){const card=el('button','card');card.setAttribute('aria-label',a.recipe.prompt);const imgWrap=el('div','card-image');const img=el('img');img.src=`/api/assets/${a.id}/thumbnail`;img.alt=a.recipe.prompt;img.loading='lazy';imgWrap.append(img);if(a.favorite)imgWrap.append(el('span','heart','♥'));if(isVideo(a))imgWrap.append(el('span','play',`▶ ${clipSeconds(a.recipe)}s`));const text=el('div','card-text');text.append(el('strong','',a.recipe.prompt),el('span','',`${a.width} × ${a.height}`));card.append(imgWrap,text,el('small','',`${a.recipe.workflow} / ${new Date(a.created).toLocaleDateString()}`));card.onclick=()=>openAsset(a);grid.append(card);}renderPending();if(!favorites&&!$('#search').value){if(assets.length)showLatest(assets[0]);else if(latestId===''){$('#latest-preview').replaceChildren(...latestPlaceholder.map(n=>n.cloneNode(true)));$('#latest-caption').textContent='';}}}
// The Live tab's empty state, restored when its last result is deleted.
const latestPlaceholder=[...$('#latest-preview').childNodes].map(n=>n.cloneNode(true));
// The Live activity tab shows the newest result: a clip plays in place, an image opens in the viewer.
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
    img.onerror=()=>figure.remove();figure.onclick=run(async e=>{e.stopPropagation();openAsset(await api('/assets/'+id));});
    figure.append(img,el('figcaption','',label));box.append(figure);
  }
  return box;
}
// Jobs that have not produced a result yet appear at the front of the gallery as placeholders.
function renderPending(){
  const grid=$('#gallery-grid');grid.querySelectorAll('.card.pending').forEach(n=>n.remove());
  if(favorites||$('#search').value)return;
  const cards=activeJobs.map(j=>{
    const card=el('button','card pending'),frame=el('div','card-image'),source=j.recipe.source_asset_id||j.recipe.end_asset_id;
    card.setAttribute('aria-label','In progress: '+j.recipe.prompt);
    if(source){const img=el('img');img.src=`/api/assets/${source}/thumbnail`;img.alt='';img.onerror=()=>img.remove();frame.append(img);}
    frame.append(el('span','state',j.state==='queued'?'Queued':j.state==='unknown'?'Needs attention':'Rendering'),progressView(j));
    const text=el('div','card-text');text.append(el('strong','',j.recipe.prompt),el('span','',j.recipe.media==='video'?'video':'image'));
    card.append(frame,text,el('small','',`${j.recipe.workflow} / on its way`));card.onclick=()=>tab('live');return card;
  });
  grid.prepend(...cards);$('#empty-gallery').hidden=total>0||cards.length>0;
}
let searchTimer;$('#search').oninput=()=>{clearTimeout(searchTimer);searchTimer=setTimeout(run(()=>refreshGallery()),250);};$('#favorites').onclick=run(async()=>{favorites=true;$('#favorites').classList.add('selected');$('#all-images').classList.remove('selected');await refreshGallery();});$('#all-images').onclick=run(async()=>{favorites=false;$('#all-images').classList.add('selected');$('#favorites').classList.remove('selected');await refreshGallery();});$('#load-more').onclick=run(()=>refreshGallery(true));
function openAsset(a){selected=a;const video=isVideo(a),file=`/api/assets/${a.id}/file`;$('#full-image').hidden=video;$('#full-video').hidden=!video;
  if(video){if(!$('#full-video').src.endsWith(file)){$('#full-video').poster=`/api/assets/${a.id}/thumbnail`;$('#full-video').src=file;}}else{$('#full-video').removeAttribute('src');$('#full-image').src=file;$('#full-image').alt=a.recipe.prompt;}
  $('#image-prompt').textContent=a.recipe.prompt;$('#image-details').replaceChildren();const details={Operation:a.recipe.operation||'txt2img',Model:a.recipe.model,Seed:a.recipe.seed,Size:`${a.width} × ${a.height}`,Steps:a.recipe.steps,Guidance:a.recipe.cfg,Workflow:a.recipe.workflow,File:a.path};if(video)details.Length=`${clipSeconds(a.recipe)} s · ${a.recipe.frames} frames at ${a.recipe.fps} fps`;if(a.recipe.operation==='variation'){details['Variation seed']=a.recipe.variation_seed;details.Strength=a.recipe.variation_strength;}if(a.recipe.operation==='refine')details.Denoise=a.recipe.denoise;if(a.recipe.refinement)details['Second model']=a.recipe.refinement.model;for(const [k,v]of Object.entries(details)){ $('#image-details').append(el('dt','',k),el('dd','',String(v)));}$('#image-recipe').textContent=JSON.stringify(a.recipe,null,2);$('#download-image').href=file;$('#download-image').download=a.id+(video?'.mp4':'.png');$('#download-image').textContent=video?'Download MP4':'Download PNG';$('#refine-image').disabled=$('#upscale-image').disabled=$('#animate-image').disabled=$('#edit-image').disabled=video;$('#frame-image').hidden=!video;$('#favorite-image').textContent=a.favorite?'♥ Favorited':'♡ Favorite';$('#parent-image').hidden=!a.recipe.parent_asset_id;$('#vary-image').disabled=video||!['txt2img','variation'].includes(a.recipe.operation||'txt2img');if(!$('#viewer').open)$('#viewer').showModal();}
$('#close-viewer').onclick=()=>$('#viewer').close();$('#viewer').addEventListener('close',()=>$('#full-video').pause());$('#reuse-image').onclick=run(async()=>{await restoreRecipe(selected.recipe);$('#viewer').close();tab('generate');});$('#favorite-image').onclick=run(async()=>{selected=await api('/assets/'+selected.id,'PUT',{favorite:!selected.favorite,rating:selected.rating});openAsset(selected);await refreshGallery();});
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
let galleryJobSignature='';
async function refreshJobs(){
  const jobs=(await api('/jobs')).items;
  const sig=JSON.stringify(jobs.map(j=>[j.id,j.state,j.outputs,j.error,j.progress]));
  if(sig===lastSignature)return;
  lastSignature=sig;
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
    reuse.onclick=run(async()=>{await restoreRecipe(j.recipe);tab('generate');});actions.append(reuse);
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
    const item=el('div','activity-job');
    item.append(el('p','',`${j.state.toUpperCase()} · ${j.recipe.prompt.slice(0,85)}${j.error?' — '+j.error:''}`),inputThumbs(j.recipe),progressView(j));
    $('#activity').append(item);
  }
  $('#footer-status').textContent=active.length?`${active.length} active job(s) · progress updates every 2 seconds`:'Your library stays on this machine.';
  // Newest first, matching the gallery's order.
  activeJobs=active;renderPending();
  if(changed)await refreshGallery();
}
async function connection(){const c=await api('/connection');$('#connection-dot').classList.toggle('ready',c.state==='ready');$('#connection-label').textContent=c.state==='ready'?'Connected · ready':c.state==='starting'?'Starting ComfyUI…':'Unavailable';$('#start-comfy').hidden=c.state==='ready';$('#start-comfy').disabled=c.state==='starting';$('#connection-detail').textContent=c.state==='ready'?`Connected to ${c.url}`:(c.startup?.message||c.error||'');if(c.state==='ready'&&!workflows.length)await loadWorkflows();}
async function loadWorkflows(){try{workflows=(await api('/workflows')).items;toolCapabilities=await api('/image-tools');$('#upscale-model').replaceChildren(...(toolCapabilities.upscale?.models||[]).filter(m=>m==='RealESRGAN_x4plus.pth').map(m=>{const o=el('option','',m);o.value=m;return o;}));setMode(mode(),$('#workflow').value||'flux-dev');setModelOptions($('#workflow').value);refinementModels();const draft=localStorage.getItem('pixanvil-draft');if(draft)await restoreRecipe(JSON.parse(draft));}catch(e){$('#workflow-description').textContent=e.message;$('#generate-button').disabled=true;}}
$('#start-comfy').onclick=run(async()=>{await api('/connection/start','POST');notice('Starting ComfyUI on SPARK. Status will update here.');await connection();});
$('#settings-form').onsubmit=run(async e=>{e.preventDefault();await api('/settings','PUT',{library:$('#library').value,comfy_url:$('#comfy-url').value,ssh_host:$('#ssh-host').value});workflows=[];notice('Settings saved. Existing images remain linked to their original files.');await connection();});$('#discover').onclick=run(async()=>{const d=await api('/discover');$('#discovery-result').textContent=d.addresses.length?`${d.hostname} → ${d.addresses.join(', ')}`:d.error;if(d.addresses.length){$('#comfy-url').value=`http://${d.hostname}:8188`;$('#ssh-host').value=`exordium@${d.hostname}`;}});
$('#metadata-file').onchange=run(async e=>{const file=e.target.files[0];if(!file)return;const form=new FormData();form.append('file',file);metadata=await api('/inspect','POST',form);$('#metadata-content').textContent=JSON.stringify(metadata.recipe||metadata.raw,null,2);$('#metadata-notice').textContent=metadata.notice||`Recovered ${metadata.format} settings. Review before generating.`;$('#load-metadata').hidden=!metadata.recipe;$('#metadata-dialog').showModal();e.target.value='';});$('#close-metadata').onclick=()=>$('#metadata-dialog').close();$('#load-metadata').onclick=run(async()=>{await restoreRecipe(metadata.recipe);$('#metadata-dialog').close();});
async function init(){tab(location.hash.slice(1)||'gallery');const settings=await api('/settings');$('#library').value=settings.library;$('#comfy-url').value=settings.comfy_url;$('#ssh-host').value=settings.ssh_host;await Promise.all([refreshGallery(),refreshPresets(),refreshJobs(),connection()]);setInterval(()=>refreshJobs().catch(()=>{}),2000);setInterval(()=>connection().catch(()=>{}),10000);}
run(init)();
