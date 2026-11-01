const $=id=>document.getElementById(id);let folder='',selected=null,roi=null,loaded=null,previewLoaded=null,photoId=null,photo=new Image(),drag=null;
async function api(url,body){const r=await fetch(url,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{});if(!r.ok)throw Error(JSON.stringify((await r.json()).detail));return r.json()}
function error(e){$('error').textContent=e.message}
async function browse(path=''){try{const list=await api('/api/files?path='+encodeURIComponent(path));folder=path;$('folder').textContent='/'+path;$('files').replaceChildren();for(const f of list){const b=document.createElement('button');b.textContent=(f.directory?'📁 ':'▶ ')+f.name;b.onclick=()=>f.directory?browse(f.path):prepare(f.path);$('files').append(b)}}catch(e){error(e)}}
async function prepare(path){try{const j=await api('/api/prepare',{path});selected=j.id;roi=null;loaded=null;previewLoaded=null;$('preview').pause();$('preview').removeAttribute('src');$('preview').load();$('preview').hidden=true;$('canvas').getContext('2d').clearRect(0,0,$('canvas').width,$('canvas').height);$('selection').textContent='未选择主体';$('submit').disabled=true;$('hint').textContent='正在提取首帧；无需等待整段视频转码';poll()}catch(e){error(e)}}
function draw(){const c=$('canvas'),ctx=c.getContext('2d');ctx.drawImage(photo,0,0,c.width,c.height);if(roi){ctx.strokeStyle='#7ce3b5';ctx.lineWidth=3;ctx.strokeRect(roi[0]*c.width,roi[1]*c.height,roi[2]*c.width,roi[3]*c.height)}}
function point(e){const r=$('canvas').getBoundingClientRect();return [Math.max(0,Math.min(1,(e.clientX-r.left)/r.width)),Math.max(0,Math.min(1,(e.clientY-r.top)/r.height))]}
$('canvas').onpointerdown=e=>{if(photoId!==selected)return;drag=point(e);$('canvas').setPointerCapture(e.pointerId)};
$('canvas').onpointerup=e=>{if(!drag)return;const p=point(e);let x=Math.min(drag[0],p[0]),y=Math.min(drag[1],p[1]),w=Math.abs(p[0]-drag[0]),h=Math.abs(p[1]-drag[1]);if(w<.01||h<.01){w=.16;h=.16;x=Math.min(.84,Math.max(0,p[0]-.08));y=Math.min(.84,Math.max(0,p[1]-.08))}roi=[x,y,w,h];drag=null;draw();$('selection').textContent='主体框已选择，可重新点选或拖动调整';$('mode').value='subject'};
$('up').onclick=()=>browse(folder.split('/').slice(0,-1).join('/'));
$('format').onchange=()=>{$('codec').value=$('format').value==='webm'?'vp9':'h264'};
$('render').onsubmit=async e=>{e.preventDefault();try{await api(`/api/jobs/${selected}/render`,{mode:$('mode').value,smoothing:+$('smoothing').value,zoom:+$('zoom').value,resolution:+$('resolution').value,format:$('format').value,codec:$('codec').value,roi});$('submit').disabled=true;poll()}catch(e){error(e)}};
let timer=null;async function poll(){clearTimeout(timer);try{const list=await api('/api/jobs');$('jobs').replaceChildren();for(const j of list){const row=document.createElement('div');row.className='job';const text=document.createElement('div');text.textContent=`${j.path} · ${j.state} · ${j.message}`;row.append(text);const bar=document.createElement('progress');bar.max=100;bar.value=j.progress;row.append(bar);if(j.state==='done'){const a=document.createElement('a');a.textContent='下载结果';a.href=`/api/jobs/${j.id}/${j.result}`;row.append(a)}if(j.frame_ready || j.state==='ready'){const b=document.createElement('button');b.textContent='打开预览';b.onclick=()=>{selected=j.id;loaded=null;previewLoaded=null;$('preview').hidden=true;$('preview').pause();poll()};row.append(b)}if(['preparing','previewing','queued','running'].includes(j.state)){const b=document.createElement('button');b.textContent='取消';b.onclick=async()=>{try{await api(`/api/jobs/${j.id}/cancel`,{})}catch(e){error(e)}};row.append(b)}$('jobs').append(row);if(j.id===selected){
 $('submit').disabled=!['ready','previewing'].includes(j.state);
 const hasFrame=j.frame_ready || j.state==='ready';
 if(hasFrame&&loaded!==j.id){
  loaded=j.id;roi=null;const id=j.id;const nextPhoto=new Image();
  nextPhoto.onload=()=>{if(selected!==id)return;photo=nextPhoto;photoId=id;$('canvas').width=photo.width;$('canvas').height=photo.height;draw()};
  nextPhoto.src=`/api/jobs/${j.id}/first.jpg`;
 }
 if(j.preview_ready && previewLoaded!==j.id){
  previewLoaded=j.id;$('preview').src=`/api/jobs/${j.id}/preview.mp4`;$('preview').hidden=false;
 }
 const size=j.meta?`原片 ${j.meta.width} × ${j.meta.height}；`:'';
 $('hint').textContent=size+(j.state==='previewing'?'首帧已就绪，可点选主体或直接导出。720p播放预览生成中 '+Math.round(j.progress)+'%。':j.preview_ready?'720p播放预览已就绪。导出默认保留原片分辨率。':j.message);
}}if(list.some(j=>['preparing','previewing','queued','running'].includes(j.state)))timer=setTimeout(poll,2000)}catch(e){error(e)}}
browse();poll();
