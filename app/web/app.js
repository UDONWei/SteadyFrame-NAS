const $=id=>document.getElementById(id);let folder='',selected=null,roi=null,loaded=null,previewLoaded=null,photoId=null,photo=new Image(),drag=null;let referenceTime=0,frameRequest=0,selectionRequest=0,selectionState='none',selectionMessage='',jobReady=false;
async function api(url,body){const r=await fetch(url,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{});if(!r.ok)throw Error(JSON.stringify((await r.json()).detail));return r.json()}
function error(e){$('error').textContent=e.message}
const downloads=new Map();
async function removeJob(j){if(!confirm('删除此任务及全部预览缓存和导出文件（包括NAS输出目录中的结果）？原始视频不会删除。'))return;try{await api(`/api/jobs/${j.id}/delete`,{confirmed:true});if(selected===j.id){selected=null;photoId=null;loaded=null;roi=null;$('preview').pause();$('preview').removeAttribute('src');$('preview').load();$('preview').hidden=true;$('canvas').getContext('2d').clearRect(0,0,$('canvas').width,$('canvas').height);$('submit').disabled=true;$('hint').textContent='任务已删除，请选择视频';cropDemo();}await poll();}catch(e){error(e)}}
async function browse(path=''){try{const list=await api('/api/files?path='+encodeURIComponent(path));folder=path;$('folder').textContent='/'+path;$('files').replaceChildren();for(const f of list){const b=document.createElement('button');b.textContent=(f.directory?'📁 ':'▶ ')+f.name+(f.directory?'':` · ${f.active?({preparing:'准备中',previewing:'预览生成中',queued:'排队中',running:'处理中'}[f.active]):''} ${f.preview?'预览已生成':f.frame?'首帧已生成':'未生成预览'}${f.outputs?' · 已处理 '+f.outputs+' 个结果':''}`);b.onclick=()=>f.directory?browse(f.path):prepare(f.path);$('files').append(b)}}catch(e){error(e)}}
async function prepare(path){try{const j=await api('/api/prepare',{path});selected=j.id;roi=(j.subject_selection||j.options)?.roi||null;referenceTime=(j.subject_selection||j.options)?.reference_time||0;frameRequest++;$('clipStart').value=j.options?.start||0;$('clipEnd').value=j.options?.end??'';$('autoCrop').value=String(j.options?.auto_crop??true);$('maxAutoZoom').value=j.options?.max_auto_zoom??2;for(const key of ['mode','smoothing','zoom','resolution','format','codec']){if(j.options?.[key]!==undefined)$(key).value=j.options[key];}if(j.options?.export_directory)$('exportDirectory').value=j.options.export_directory;$('seek').value=0;$('seekTime').value=0;loaded=null;previewLoaded=null;$('preview').pause();$('preview').removeAttribute('src');$('preview').load();$('preview').hidden=true;$('canvas').getContext('2d').clearRect(0,0,$('canvas').width,$('canvas').height);$('selection').textContent='未选择主体';photoId=null;cropDemo();$('submit').disabled=true;$('hint').textContent=j.cache_hit?'已复用预览缓存':'正在提取首帧；无需等待整段视频转码';poll()}catch(e){error(e)}}
function subjectOverlay(ctx,rect,label='已框选主体'){
 const [x,y,w,h]=rect;ctx.save();ctx.strokeStyle='#ffb454';ctx.lineWidth=Math.max(3,ctx.canvas.width/240);ctx.strokeRect(x,y,w,h);
 const fs=Math.max(14,ctx.canvas.width/38);ctx.font=`bold ${fs}px sans-serif`;const textWidth=ctx.measureText(label).width;
 const labelX=Math.max(0,Math.min(ctx.canvas.width-textWidth-12,x)),labelY=Math.max(fs+8,y);
 ctx.fillStyle='#15191ee6';ctx.fillRect(labelX,labelY-fs-6,textWidth+12,fs+8);ctx.fillStyle='#ffb454';ctx.fillText(label,labelX+6,labelY-2);ctx.restore();
}
function refreshSelection(){
 const ready=!!selected&&photoId===selected&&!!photo.naturalWidth;
 const uses=$('mode').value==='subject';
 $('submit').disabled=!jobReady||!ready||(uses&&(!roi||selectionState!=='valid'));
 $('checkSubject').disabled=!ready||!roi||selectionState==='checking';
 $('selection').className=selectionState;
 $('selection').textContent=!ready?'参考帧尚未就绪，暂不能选主体':!roi?'未框选主体：请在上方静态画面点选或拖动框选':`已框选主体 · 参考帧 ${referenceTime.toFixed(3)} 秒 · ${selectionMessage||'等待检查'}`+(uses?'':' · 当前防抖模式不使用主体框');
 const c=$('subjectThumb'),ctx=c.getContext('2d');ctx.clearRect(0,0,c.width,c.height);
 if(ready&&roi){const [x,y,w,h]=roi;c.width=320;c.height=Math.max(40,Math.round(320*(h*photo.height)/(w*photo.width)));ctx.drawImage(photo,x*photo.width,y*photo.height,w*photo.width,h*photo.height,0,0,c.width,c.height);}
}
async function verifySubject(){
 if(!selected||photoId!==selected||!roi)return;
 const id=selected,request=++selectionRequest,box=[...roi],time=referenceTime;
 selectionState='checking';selectionMessage='正在检查选框与跟踪纹理';refreshSelection();
 try{const r=await api(`/api/jobs/${id}/subject-check`,{roi:box,reference_time:time});
  if(request!==selectionRequest||selected!==id||referenceTime!==time||JSON.stringify(roi)!==JSON.stringify(box))return;
  selectionState='valid';selectionMessage=`检查通过（${r.features}个特征点）；请确认放大图中是目标主体`;
 }catch(e){if(request!==selectionRequest||selected!==id)return;selectionState='invalid';selectionMessage=e.message;}
 refreshSelection();
}
function cropDemo(){
 const a=$('cropSource'),b=$('cropOutput');
 const ac=a.getContext('2d'),bc=b.getContext('2d');ac.clearRect(0,0,a.width,a.height);bc.clearRect(0,0,b.width,b.height);
 if(!selected||photoId!==selected||!photo.naturalWidth){$('cropInfo').textContent='选择视频后显示静态裁切示意。';return;}
 const z=Math.max(1,Math.min(4,+$('zoom').value||1));
 a.width=b.width=photo.naturalWidth;a.height=b.height=photo.naturalHeight;
 const w=a.width/z,h=a.height/z,x=(a.width-w)/2,y=(a.height-h)/2;
 ac.drawImage(photo,0,0);ac.fillStyle='rgba(0,0,0,.55)';ac.fillRect(0,0,a.width,y);ac.fillRect(0,y+h,a.width,y);ac.fillRect(0,y,x,h);ac.fillRect(x+w,y,x,h);ac.strokeStyle='#7ce3b5';ac.lineWidth=3;ac.strokeRect(x,y,w,h);
 bc.drawImage(photo,x,y,w,h,0,0,b.width,b.height);
 if(roi){subjectOverlay(ac,[roi[0]*a.width,roi[1]*a.height,roi[2]*a.width,roi[3]*a.height]);subjectOverlay(bc,[(roi[0]*a.width-x)*z,(roi[1]*a.height-y)*z,roi[2]*a.width*z,roi[3]*a.height*z]);}
 $('cropInfo').textContent=`示意倍率 ${z.toFixed(2)}× · 保留宽/高 ${(100/z).toFixed(1)}% · 保留画面面积 ${(100/z/z).toFixed(1)}%`+($('autoCrop').value==='true'?'；自动模式可能进一步裁切，达到上限时减弱防抖。':'；手动模式不保证无黑边。');
}
async function deleteOutput(j,file){
 const r=j.results.find(r=>r.file===file);if(!r)return;
 if(!confirm('删除这个输出视频？\n'+(r.saved_path||r.file)+'\n其他结果、预览缓存和原视频会保留。'))return;
 try{await api(`/api/jobs/${j.id}/delete-output`,{file,confirmed:true});downloads.delete(j.id);await poll();}catch(e){error(e)}
}
function draw(){const c=$('canvas'),ctx=c.getContext('2d');ctx.clearRect(0,0,c.width,c.height);if(photoId!==selected)return;ctx.drawImage(photo,0,0,c.width,c.height);if(roi)subjectOverlay(ctx,[roi[0]*c.width,roi[1]*c.height,roi[2]*c.width,roi[3]*c.height]);cropDemo();refreshSelection()}
function point(e){const r=$('canvas').getBoundingClientRect();return [Math.max(0,Math.min(1,(e.clientX-r.left)/r.width)),Math.max(0,Math.min(1,(e.clientY-r.top)/r.height))]}
$('canvas').onpointerdown=e=>{if(!selected||photoId!==selected)return;drag=point(e);$('canvas').setPointerCapture(e.pointerId)};
$('canvas').onpointerup=e=>{if(!drag)return;const p=point(e);let x=Math.min(drag[0],p[0]),y=Math.min(drag[1],p[1]),w=Math.abs(p[0]-drag[0]),h=Math.abs(p[1]-drag[1]);if(w<.01||h<.01){w=.16;h=.16;x=Math.min(.84,Math.max(0,p[0]-.08));y=Math.min(.84,Math.max(0,p[1]-.08))}roi=[x,y,w,h];drag=null;selectionRequest++;selectionState='none';$('mode').value='subject';draw();verifySubject()};
$('canvas').onpointermove=e=>{if(!drag)return;draw();const p=point(e),c=$('canvas');subjectOverlay(c.getContext('2d'),[Math.min(drag[0],p[0])*c.width,Math.min(drag[1],p[1])*c.height,Math.abs(p[0]-drag[0])*c.width,Math.abs(p[1]-drag[1])*c.height],'正在框选');};
$('canvas').onpointercancel=()=>{drag=null;draw()};
$('checkSubject').onclick=verifySubject;$('mode').onchange=refreshSelection;
$('up').onclick=()=>browse(folder.split('/').slice(0,-1).join('/'));
$('format').onchange=()=>{$('codec').value=$('format').value==='webm'?'vp9':'h264'};
$('render').onsubmit=async e=>{e.preventDefault();if($('mode').value==='subject'&&(!roi||selectionState!=='valid'||photoId!==selected)){error(Error('请先框选主体并等待检查通过'));return;}try{await api(`/api/jobs/${selected}/render`,{mode:$('mode').value,smoothing:+$('smoothing').value,zoom:+$('zoom').value,resolution:+$('resolution').value,format:$('format').value,codec:$('codec').value,roi,start:+$('clipStart').value,end:$('clipEnd').value===''?null:+$('clipEnd').value,reference_time:referenceTime,export_directory:$('exportDirectory').value,auto_crop:$('autoCrop').value==='true',max_auto_zoom:+$('maxAutoZoom').value});$('submit').disabled=true;poll()}catch(e){error(e)}};
let timer=null,polling=false,pollAgain=false;async function poll(){if(document.hidden)return;if(polling){pollAgain=true;return;}polling=true;clearTimeout(timer);timer=null;try{const list=await api('/api/jobs');$('jobs').replaceChildren();for(const j of list){const row=document.createElement('div');row.className='job';const text=document.createElement('div');text.textContent=`${j.path} · ${j.state} · ${j.message}`;row.append(text);const bar=document.createElement('progress');bar.max=100;bar.value=j.progress;row.append(bar);if(j.results?.length){const select=document.createElement('select');select.className='versions';select.setAttribute('aria-label','选择下载结果');j.results.forEach((r,i)=>{const o=document.createElement('option');o.value=r.file;const p=r.options||{};o.textContent=`结果 ${i+1} · ${p.mode||''} · 平滑 ${p.smoothing||'-'} · 放大 ${p.zoom||'-'} · ${p.resolution||'原片'} · ${p.codec||''} · ${p.start||0}–${p.end??'片尾'}秒`;select.append(o)});select.value=downloads.get(j.id)||j.results.at(-1).file;if(!select.value)select.value=j.results.at(-1).file;const a=document.createElement('a');a.textContent='下载所选结果';const saved=document.createElement('div');saved.className='saved-path';const sync=()=>{const chosen=j.results.find(r=>r.file===select.value);saved.textContent=(chosen?.saved_path?'已保存：'+chosen.saved_path:'旧结果保存在应用数据目录')+(chosen?.output?.effective_zoom?` · 实际裁切 ${chosen.output.effective_zoom.toFixed(3)}× · 保留画面 ${(100*chosen.output.retained_area).toFixed(1)}% · 防抖修正 ${(100*chosen.output.stabilization_strength).toFixed(0)}%`:'');downloads.set(j.id,select.value);a.href=`/api/jobs/${j.id}/${select.value}`};select.onchange=sync;sync();const remove=document.createElement('button');remove.textContent='删除所选输出';remove.disabled=['preparing','previewing','queued','running'].includes(j.state);remove.onclick=()=>deleteOutput(j,select.value);row.append(select,a,remove,saved)}if(j.frame_ready || j.state==='ready'){const b=document.createElement('button');b.textContent='调参 / 再次生成';b.onclick=()=>{selected=j.id;loaded=null;previewLoaded=null;frameRequest++;photoId=null;selectionRequest++;selectionState='none';refreshSelection();referenceTime=(j.subject_selection||j.options)?.reference_time||0;$('clipStart').value=j.options?.start||0;$('clipEnd').value=j.options?.end??'';roi=(j.subject_selection||j.options)?.roi||null;if(j.options?.export_directory)$('exportDirectory').value=j.options.export_directory;$('autoCrop').value=String(j.options?.auto_crop??true);$('maxAutoZoom').value=j.options?.max_auto_zoom??2;for(const key of ['mode','smoothing','zoom','resolution','format','codec']){if(j.options?.[key]!==undefined)$(key).value=j.options[key];}$('preview').hidden=true;$('preview').pause();poll()};row.append(b)}if(['preparing','previewing','queued','running'].includes(j.state)){const b=document.createElement('button');b.textContent='取消';b.onclick=async()=>{try{await api(`/api/jobs/${j.id}/cancel`,{})}catch(e){error(e)}};row.append(b)}if(!['preparing','previewing','queued','running'].includes(j.state)){const d=document.createElement('button');d.textContent='删除任务';d.onclick=()=>removeJob(j);row.append(d)}$('jobs').append(row);if(j.id===selected){
 jobReady=['ready','previewing','done','failed','cancelled','interrupted'].includes(j.state)&&j.frame_ready;refreshSelection();
 $('chooseFrame').disabled=!j.meta;
 if(j.meta){$('seek').max=Math.max(0,(j.meta.frames-1)/j.meta.fps);$('seek').step=1/j.meta.fps;}
 const hasFrame=j.frame_ready || j.state==='ready';
 if(hasFrame&&loaded!==j.id){
  loaded=j.id;roi=(j.subject_selection||j.options)?.roi||null;const id=j.id,request=++frameRequest;photoId=null;selectionRequest++;selectionState='none';refreshSelection();const nextPhoto=new Image();
  nextPhoto.onload=()=>{if(selected!==id||request!==frameRequest)return;photo=nextPhoto;photoId=id;$('canvas').width=photo.width;$('canvas').height=photo.height;draw();if(roi)verifySubject()};
  nextPhoto.onerror=()=>{if(selected===id&&request===frameRequest){loaded=null;selectionMessage='参考帧加载失败，请重新选择视频';refreshSelection();}};
  nextPhoto.src=referenceTime?`/api/jobs/${j.id}/frame?time=${referenceTime}`:`/api/jobs/${j.id}/first.jpg`;
  $('referenceLabel').textContent=`主体参考帧：${referenceTime.toFixed(3)} 秒`;
  $('seek').value=referenceTime;$('seekTime').value=referenceTime;
 }
 if(j.preview_ready && previewLoaded!==j.id){
  previewLoaded=j.id;$('preview').src=`/api/jobs/${j.id}/preview.mp4`;$('preview').hidden=false;
 }
 const size=j.meta?`原片 ${j.meta.width} × ${j.meta.height}；`:'';
 $('hint').textContent=size+(j.state==='previewing'?'首帧已就绪，可点选主体或直接导出。720p播放预览生成中 '+Math.round(j.progress)+'%。':j.preview_ready?'720p播放预览已就绪。导出默认保留原片分辨率。':j.message);
}}await browse(folder);if(!document.hidden&&list.some(j=>['preparing','previewing','queued','running'].includes(j.state)))timer=setTimeout(poll,2000)}catch(e){error(e)}finally{polling=false;if(pollAgain&&!document.hidden){pollAgain=false;queueMicrotask(poll);}}}
$('seek').oninput=()=>{$('seekTime').value=$('seek').value;};
$('seekTime').onchange=()=>{$('seek').value=$('seekTime').value;};
$('preview').ontimeupdate=()=>{if(!$('preview').paused){$('seek').value=$('preview').currentTime;$('seekTime').value=$('preview').currentTime.toFixed(3)}};
$('zoom').oninput=cropDemo;$('autoCrop').onchange=cropDemo;$('maxAutoZoom').oninput=cropDemo;
$('chooseFrame').onclick=async()=>{
 const id=selected,request=++frameRequest,time=+$('seekTime').value;
 if(!id)return;
 photoId=null;roi=null;drag=null;selectionRequest++;selectionState='none';refreshSelection();cropDemo();
 $('chooseFrame').disabled=true;
 try{
  const r=await fetch(`/api/jobs/${id}/frame?time=${time}`);
  if(!r.ok)throw Error((await r.json()).detail);
  const url=URL.createObjectURL(await r.blob()),nextPhoto=new Image();
  nextPhoto.onload=()=>{URL.revokeObjectURL(url);if(selected!==id||frameRequest!==request)return;photo=nextPhoto;photoId=id;referenceTime=time;roi=null;$('canvas').width=photo.width;$('canvas').height=photo.height;draw();refreshSelection();$('referenceLabel').textContent=`主体参考帧：${time.toFixed(3)} 秒`};
  nextPhoto.onerror=()=>URL.revokeObjectURL(url);nextPhoto.src=url;
 }catch(e){error(e)}finally{if(selected===id)$('chooseFrame').disabled=false;}
};
api('/api/export-settings').then(s=>{$('exportDirectory').value=s.default;$('exportHelp').textContent=s.description;}).catch(error);
document.addEventListener('visibilitychange',()=>{clearTimeout(timer);timer=null;if(document.hidden){pollAgain=false;$('preview').pause();}else{poll();}});
poll();
