const $=id=>document.getElementById(id);let folder='',selected=null,roi=null,loaded=null,previewLoaded=null,photoId=null,photo=new Image(),drag=null;let referenceTime=0,frameRequest=0;
async function api(url,body){const r=await fetch(url,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{});if(!r.ok)throw Error(JSON.stringify((await r.json()).detail));return r.json()}
function error(e){$('error').textContent=e.message}
const downloads=new Map();
async function removeJob(j){if(!confirm('删除此任务及全部预览缓存和导出文件（包括NAS输出目录中的结果）？原始视频不会删除。'))return;try{await api(`/api/jobs/${j.id}/delete`,{confirmed:true});if(selected===j.id){selected=null;photoId=null;loaded=null;roi=null;$('preview').pause();$('preview').removeAttribute('src');$('preview').load();$('preview').hidden=true;$('canvas').getContext('2d').clearRect(0,0,$('canvas').width,$('canvas').height);$('submit').disabled=true;$('hint').textContent='任务已删除，请选择视频';}await poll();}catch(e){error(e)}}
async function browse(path=''){try{const list=await api('/api/files?path='+encodeURIComponent(path));folder=path;$('folder').textContent='/'+path;$('files').replaceChildren();for(const f of list){const b=document.createElement('button');b.textContent=(f.directory?'📁 ':'▶ ')+f.name+(f.directory?'':` · ${f.active?({preparing:'准备中',previewing:'预览生成中',queued:'排队中',running:'处理中'}[f.active]):''} ${f.preview?'预览已生成':f.frame?'首帧已生成':'未生成预览'}${f.outputs?' · 已处理 '+f.outputs+' 个结果':''}`);b.onclick=()=>f.directory?browse(f.path):prepare(f.path);$('files').append(b)}}catch(e){error(e)}}
async function prepare(path){try{const j=await api('/api/prepare',{path});selected=j.id;roi=j.options?.roi||null;referenceTime=j.options?.reference_time||0;frameRequest++;$('clipStart').value=j.options?.start||0;$('clipEnd').value=j.options?.end??'';for(const key of ['mode','smoothing','zoom','resolution','format','codec']){if(j.options?.[key]!==undefined)$(key).value=j.options[key];}if(j.options?.export_directory)$('exportDirectory').value=j.options.export_directory;$('seek').value=0;$('seekTime').value=0;loaded=null;previewLoaded=null;$('preview').pause();$('preview').removeAttribute('src');$('preview').load();$('preview').hidden=true;$('canvas').getContext('2d').clearRect(0,0,$('canvas').width,$('canvas').height);$('selection').textContent='未选择主体';$('submit').disabled=true;$('hint').textContent=j.cache_hit?'已复用预览缓存':'正在提取首帧；无需等待整段视频转码';poll()}catch(e){error(e)}}
function draw(){const c=$('canvas'),ctx=c.getContext('2d');ctx.drawImage(photo,0,0,c.width,c.height);if(roi){ctx.strokeStyle='#7ce3b5';ctx.lineWidth=3;ctx.strokeRect(roi[0]*c.width,roi[1]*c.height,roi[2]*c.width,roi[3]*c.height)}}
function point(e){const r=$('canvas').getBoundingClientRect();return [Math.max(0,Math.min(1,(e.clientX-r.left)/r.width)),Math.max(0,Math.min(1,(e.clientY-r.top)/r.height))]}
$('canvas').onpointerdown=e=>{if(photoId!==selected)return;drag=point(e);$('canvas').setPointerCapture(e.pointerId)};
$('canvas').onpointerup=e=>{if(!drag)return;const p=point(e);let x=Math.min(drag[0],p[0]),y=Math.min(drag[1],p[1]),w=Math.abs(p[0]-drag[0]),h=Math.abs(p[1]-drag[1]);if(w<.01||h<.01){w=.16;h=.16;x=Math.min(.84,Math.max(0,p[0]-.08));y=Math.min(.84,Math.max(0,p[1]-.08))}roi=[x,y,w,h];drag=null;draw();$('selection').textContent='主体框已选择，可重新点选或拖动调整';$('mode').value='subject'};
$('up').onclick=()=>browse(folder.split('/').slice(0,-1).join('/'));
$('format').onchange=()=>{$('codec').value=$('format').value==='webm'?'vp9':'h264'};
$('render').onsubmit=async e=>{e.preventDefault();try{await api(`/api/jobs/${selected}/render`,{mode:$('mode').value,smoothing:+$('smoothing').value,zoom:+$('zoom').value,resolution:+$('resolution').value,format:$('format').value,codec:$('codec').value,roi,start:+$('clipStart').value,end:$('clipEnd').value===''?null:+$('clipEnd').value,reference_time:referenceTime,export_directory:$('exportDirectory').value});$('submit').disabled=true;poll()}catch(e){error(e)}};
let timer=null;async function poll(){clearTimeout(timer);try{const list=await api('/api/jobs');$('jobs').replaceChildren();for(const j of list){const row=document.createElement('div');row.className='job';const text=document.createElement('div');text.textContent=`${j.path} · ${j.state} · ${j.message}`;row.append(text);const bar=document.createElement('progress');bar.max=100;bar.value=j.progress;row.append(bar);if(j.results?.length){const select=document.createElement('select');select.className='versions';select.setAttribute('aria-label','选择下载结果');j.results.forEach((r,i)=>{const o=document.createElement('option');o.value=r.file;const p=r.options||{};o.textContent=`结果 ${i+1} · ${p.mode||''} · 平滑 ${p.smoothing||'-'} · 放大 ${p.zoom||'-'} · ${p.resolution||'原片'} · ${p.codec||''} · ${p.start||0}–${p.end??'片尾'}秒`;select.append(o)});select.value=downloads.get(j.id)||j.results.at(-1).file;if(!select.value)select.value=j.results.at(-1).file;const a=document.createElement('a');a.textContent='下载所选结果';const saved=document.createElement('div');saved.className='saved-path';const sync=()=>{const chosen=j.results.find(r=>r.file===select.value);saved.textContent=chosen?.saved_path?'已保存：'+chosen.saved_path:'旧结果保存在应用数据目录';downloads.set(j.id,select.value);a.href=`/api/jobs/${j.id}/${select.value}`};select.onchange=sync;sync();row.append(select,a,saved)}if(j.frame_ready || j.state==='ready'){const b=document.createElement('button');b.textContent='调参 / 再次生成';b.onclick=()=>{selected=j.id;loaded=null;previewLoaded=null;frameRequest++;referenceTime=j.options?.reference_time||0;$('clipStart').value=j.options?.start||0;$('clipEnd').value=j.options?.end??'';roi=j.options?.roi||null;if(j.options?.export_directory)$('exportDirectory').value=j.options.export_directory;for(const key of ['mode','smoothing','zoom','resolution','format','codec']){if(j.options?.[key]!==undefined)$(key).value=j.options[key];}$('preview').hidden=true;$('preview').pause();poll()};row.append(b)}if(['preparing','previewing','queued','running'].includes(j.state)){const b=document.createElement('button');b.textContent='取消';b.onclick=async()=>{try{await api(`/api/jobs/${j.id}/cancel`,{})}catch(e){error(e)}};row.append(b)}if(!['preparing','previewing','queued','running'].includes(j.state)){const d=document.createElement('button');d.textContent='删除任务';d.onclick=()=>removeJob(j);row.append(d)}$('jobs').append(row);if(j.id===selected){
 $('submit').disabled=!(['ready','previewing','done','failed','cancelled','interrupted'].includes(j.state)&&j.frame_ready);
 $('chooseFrame').disabled=!j.meta;
 if(j.meta){$('seek').max=Math.max(0,(j.meta.frames-1)/j.meta.fps);$('seek').step=1/j.meta.fps;}
 const hasFrame=j.frame_ready || j.state==='ready';
 if(hasFrame&&loaded!==j.id){
  loaded=j.id;roi=j.options?.roi||null;const id=j.id;const nextPhoto=new Image();
  nextPhoto.onload=()=>{if(selected!==id)return;photo=nextPhoto;photoId=id;$('canvas').width=photo.width;$('canvas').height=photo.height;draw()};
  nextPhoto.src=referenceTime?`/api/jobs/${j.id}/frame?time=${referenceTime}`:`/api/jobs/${j.id}/first.jpg`;
  $('referenceLabel').textContent=`主体参考帧：${referenceTime.toFixed(3)} 秒`;
  $('seek').value=referenceTime;$('seekTime').value=referenceTime;
 }
 if(j.preview_ready && previewLoaded!==j.id){
  previewLoaded=j.id;$('preview').src=`/api/jobs/${j.id}/preview.mp4`;$('preview').hidden=false;
 }
 const size=j.meta?`原片 ${j.meta.width} × ${j.meta.height}；`:'';
 $('hint').textContent=size+(j.state==='previewing'?'首帧已就绪，可点选主体或直接导出。720p播放预览生成中 '+Math.round(j.progress)+'%。':j.preview_ready?'720p播放预览已就绪。导出默认保留原片分辨率。':j.message);
}}await browse(folder);if(list.some(j=>['preparing','previewing','queued','running'].includes(j.state)))timer=setTimeout(poll,2000)}catch(e){error(e)}}
$('seek').oninput=()=>{$('seekTime').value=$('seek').value;};
$('seekTime').onchange=()=>{$('seek').value=$('seekTime').value;};
$('preview').ontimeupdate=()=>{if(!$('preview').paused){$('seek').value=$('preview').currentTime;$('seekTime').value=$('preview').currentTime.toFixed(3)}};
$('chooseFrame').onclick=async()=>{
 const id=selected,request=++frameRequest,time=+$('seekTime').value;
 if(!id)return;
 $('chooseFrame').disabled=true;
 try{
  const r=await fetch(`/api/jobs/${id}/frame?time=${time}`);
  if(!r.ok)throw Error((await r.json()).detail);
  const url=URL.createObjectURL(await r.blob()),nextPhoto=new Image();
  nextPhoto.onload=()=>{URL.revokeObjectURL(url);if(selected!==id||frameRequest!==request)return;photo=nextPhoto;photoId=id;referenceTime=time;roi=null;$('canvas').width=photo.width;$('canvas').height=photo.height;draw();$('selection').textContent='请在此帧上重新选择主体';$('referenceLabel').textContent=`主体参考帧：${time.toFixed(3)} 秒`};
  nextPhoto.onerror=()=>URL.revokeObjectURL(url);nextPhoto.src=url;
 }catch(e){error(e)}finally{if(selected===id)$('chooseFrame').disabled=false;}
};
api('/api/export-settings').then(s=>{$('exportDirectory').value=s.default;$('exportHelp').textContent=s.description;}).catch(error);
browse();poll();
