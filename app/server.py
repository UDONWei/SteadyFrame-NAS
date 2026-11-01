import json
import logging
import os
import threading
import uuid
import shutil
import tempfile
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator
from . import engine

class QuietHealth(logging.Filter):
    quiet_health_filter=True
    def filter(self, record):
        args=record.args
        return not (isinstance(args,tuple) and len(args)>=5 and args[1]=='GET'
                    and str(args[2]).split('?',1)[0]=='/api/health' and args[4]==200)

access_logger=logging.getLogger('uvicorn.access')
if not any(getattr(f,'quiet_health_filter',False) for f in access_logger.filters):
    access_logger.addFilter(QuietHealth())

MEDIA=Path(os.environ.get('MEDIA_ROOT','/media')).resolve()
DATA=Path(os.environ.get('DATA_ROOT','/data')).resolve()
DATA.mkdir(parents=True,exist_ok=True)
OUTPUT=Path(os.environ.get('OUTPUT_ROOT',str(DATA/'exports'))).resolve()
OUTPUT_HOST=os.environ.get('OUTPUT_HOST_ROOT',str(OUTPUT)).replace('\\','/').rstrip('/')
ACTIVE=('preparing','previewing','queued','running')
app=FastAPI(title='SteadyFrame NAS')
pool=ThreadPoolExecutor(max_workers=1)
lock=threading.RLock()
jobs={}
events={}
preview_stops={}
EXT={'.mp4','.mov','.mkv','.avi','.mts','.m2ts','.webm','.m4v','.mpg','.mpeg','.ts','.wmv','.flv','.3gp'}

def source(path):
    p=(MEDIA/path).resolve()
    if not p.is_relative_to(MEDIA) or not p.is_file(): raise HTTPException(400,'无效媒体路径')
    return p

def fingerprint(path):
    p=source(path); st=p.stat()
    return dict(path=str(p),size=st.st_size,mtime=st.st_mtime_ns,version=1)

def unchanged(j):
    return j.get('fingerprint')==fingerprint(j['path'])

def export_directory(value):
    value=value.strip().replace('\\','/')
    if value==OUTPUT_HOST: value=''
    elif value.startswith(OUTPUT_HOST+'/'): value=value[len(OUTPUT_HOST)+1:]
    p=(OUTPUT/value).resolve()
    if not p.is_relative_to(OUTPUT) or p.is_relative_to(MEDIA) or MEDIA.is_relative_to(p):
        raise HTTPException(400,'导出目录必须位于已挂载的输出目录内，不能写入原视频目录')
    return p

def export_file(r):
    if r.get('export_root')!=OUTPUT_HOST: raise HTTPException(409,'此结果位于原输出根目录，请恢复对应的输出目录挂载后访问')
    p=export_directory(r['export_relative'])
    if p==OUTPUT: raise HTTPException(400,'无效导出文件')
    return p

class Options(BaseModel):
    export_directory: str=Field('',max_length=2048)
    mode: Literal['translation','smooth','lock','subject']='smooth'
    smoothing: int=Field(30,ge=1,le=150)
    zoom: float=Field(1.1,ge=1,le=4)
    auto_crop: bool=True
    max_auto_zoom: float=Field(2,ge=1.01,le=4)
    resolution: Literal[0,480,720,1080,1440,2160]=0
    format: Literal['mp4','mov','mkv','webm']='mp4'
    codec: Literal['h264','hevc','vp9']='h264'
    roi: tuple[float,float,float,float] | None=None
    start: float=Field(0,ge=0,allow_inf_nan=False)
    end: float | None=Field(None,gt=0,allow_inf_nan=False)
    reference_time: float=Field(0,ge=0,allow_inf_nan=False)
    @model_validator(mode='after')
    def validate_options(self):
        if self.end is not None and self.end<=self.start: raise ValueError('剪辑结束时间必须晚于开始时间')
        if self.mode=='subject' and (self.reference_time<self.start or (self.end is not None and self.reference_time>=self.end)): raise ValueError('主体选择帧必须位于剪辑范围内')
        if (self.format=='webm') != (self.codec=='vp9'): raise ValueError('WebM 需配合 VP9；其他容器请选择 H.264 或 HEVC')
        if self.mode=='subject':
            if self.roi is None: raise ValueError('请点选或框选主体')
            x,y,w,h=self.roi
            if not (0<=x<1 and 0<=y<1 and 0<w<=1-x and 0<h<=1-y): raise ValueError('无效主体框')
        return self

class Prepare(BaseModel):
    path: str

def save(job):
    directory=DATA/job['id']; directory.mkdir(exist_ok=True)
    tmp=directory/'state.tmp'; tmp.write_text(json.dumps(job,ensure_ascii=False),encoding='utf-8'); tmp.replace(directory/'state.json')

for state in DATA.glob('*/state.json'):
    try:
        j=json.loads(state.read_text(encoding='utf-8'))
        if j['state'] in ('preparing','previewing','queued','running'): j.update(state='interrupted',message='服务重启，任务中断；请重新准备')
        jobs[j['id']]=j; events[j['id']]=threading.Event(); preview_stops[j['id']]=threading.Event()
    except (ValueError,KeyError): pass

def update(id,**values):
    with lock: jobs[id].update(values); save(jobs[id])

def getjob(id):
    with lock:
        if id not in jobs: raise HTTPException(404,'任务不存在')
        return dict(jobs[id])

def prepare_worker(id):
    try:
        j=getjob(id); folder=DATA/id
        if events[id].is_set(): raise InterruptedError('任务已取消')
        original=source(j['path'])
        if not unchanged(j): raise ValueError('原视频已改变，请重新选择视频')
        meta=j.get('meta') or engine.inspect(original)
        if not (folder/'first.jpg').is_file(): engine.extract_first(original,folder/'first.jpg',meta)
        if events[id].is_set(): raise InterruptedError('任务已取消')
        if not unchanged(j): raise ValueError('原视频已改变，请重新选择视频')
        update(id,state='previewing',frame_ready=True,meta=meta,message='首帧已就绪，可选主体或直接导出；后台生成720p预览',progress=0)
        def progress(value):
            with lock:
                if jobs[id]['state']=='previewing': update(id,progress=round(value,1))
        try:
            engine.normalize(original,folder/'preview.mp4',meta,
                lambda: events[id].is_set() or preview_stops[id].is_set(),progress)
            with lock:
                if not unchanged(j): raise ValueError('原视频已改变，请重新选择视频')
                update(id,preview_ready=True)
                if jobs[id]['state']=='previewing': update(id,state='ready',message='720p预览已就绪；导出读取原片',progress=0)
        except Exception as e:
            with lock:
                if events[id].is_set(): update(id,state='cancelled',message='任务已取消')
                elif jobs[id]['state']=='previewing':
                    update(id,state='ready',preview_error=str(e),message='播放预览未完成，仍可用首帧选主体并从原片导出',progress=0)
    except Exception as e: update(id,state='cancelled' if events[id].is_set() else 'failed',message=str(e))

def results(j):
    return j.get('results', [dict(file=j['result'], options=j.get('options',{}))] if j.get('result') else [])

def render_worker(id,options):
    try:
        if events[id].is_set(): raise InterruptedError('任务已取消')
        update(id,state='running',message='分析运动')
        j=getjob(id)
        if not unchanged(j): raise ValueError('原视频已改变，请重新选择视频')
        output=DATA/id/('result-'+uuid.uuid4().hex+'.'+options['format'])
        meta=engine.stabilize(source(j['path']),DATA/id/'preview.mp4',output,options,
            lambda message,progress:update(id,message=message,progress=round(progress,1)),events[id].is_set)
        if not unchanged(j): raise ValueError('原视频已改变，未发布导出结果')
        destination=export_directory(options.get('export_directory',''))
        destination.mkdir(parents=True,exist_ok=True)
        name=source(j['path']).stem.encode('utf-8')[:120].decode('utf-8',errors='ignore')+'-steady-'+uuid.uuid4().hex+'.'+options['format']
        final=destination/name
        temporary=destination/(name+'.partial')
        try:
            update(id,message='保存到NAS输出目录',progress=99)
            with output.open('rb') as src,temporary.open('wb') as dst:
                while chunk:=src.read(8*1024*1024):
                    if events[id].is_set(): raise InterruptedError('任务已取消')
                    dst.write(chunk)
            if events[id].is_set(): raise InterruptedError('任务已取消')
            temporary.replace(final)
        finally: temporary.unlink(missing_ok=True)
        relative=final.relative_to(OUTPUT).as_posix()
        output.unlink(missing_ok=True)
        with lock:
            versions=results(jobs[id])+[dict(export_relative=relative,export_root=OUTPUT_HOST,saved_path=OUTPUT_HOST+'/'+relative,file=output.name,options=options,created=datetime.now(timezone.utc).isoformat(),output=meta)]
            update(id,state='done',message='导出完成',progress=100,result=output.name,output=meta,results=versions)
    except Exception as e: update(id,state='cancelled' if events[id].is_set() else 'failed',message=str(e))

@app.get('/api/health')
def health(): return {'status':'ok','version':'0.7.1'}

@app.get('/api/export-settings')
def export_settings():
    return {'root':OUTPUT_HOST,'default':OUTPUT_HOST,'description':'可填写此输出根目录下的完整路径或子文件夹名；其他磁盘目录需先修改 OUTPUT_DIR 挂载。'}

@app.get('/api/files')
def files(path: str=''):
    folder=(MEDIA/path).resolve()
    if not folder.is_relative_to(MEDIA) or not folder.is_dir(): raise HTTPException(400,'无效目录')
    result=[]
    for p in sorted(folder.iterdir(),key=lambda p:(not p.is_dir(),p.name.lower())):
        if not p.resolve().is_relative_to(MEDIA): continue
        if p.is_dir() or p.suffix.lower() in EXT:
            relative=p.relative_to(MEDIA).as_posix()
            with lock:
                related=[j for j in jobs.values() if j['path']==relative]
                signature=fingerprint(relative) if p.is_file() else None
                valid=[j for j in related if j.get('fingerprint')==signature]
                status=dict(preview=any(j.get('preview_ready') and (DATA/j['id']/'preview.mp4').is_file() for j in valid),frame=any(j.get('frame_ready') and (DATA/j['id']/'first.jpg').is_file() for j in valid),outputs=sum(len(results(j)) for j in related),active=next((j['state'] for j in related if j['state'] in ('preparing','previewing','queued','running')),None))
            result.append(dict(name=p.name,path=relative,directory=p.is_dir(),**status))
        if len(result)>=2000: break
    return result

@app.get('/api/jobs')
def list_jobs():
    with lock: return list(reversed([dict(j,results=results(j)) for j in jobs.values()]))

@app.post('/api/prepare')
def prepare(body: Prepare):
    signature=fingerprint(body.path)
    with lock:
        for j in reversed(list(jobs.values())):
            if j['path']!=body.path or j.get('fingerprint')!=signature: continue
            folder=DATA/j['id']
            if j['state'] in ACTIVE: return dict(j,cache_hit=True)
            if not (folder/'first.jpg').is_file(): continue
            if (folder/'preview.mp4').is_file():
                update(j['id'],frame_ready=True,preview_ready=True)
                return dict(j,cache_hit=True)
            if sum(x['state'] in ACTIVE for x in jobs.values())>=4: return dict(j,cache_hit=True)
            events[j['id']].clear();preview_stops[j['id']].clear()
            update(j['id'],state='preparing',preview_ready=False,message='复用首帧，补充720p播放预览',progress=0)
            pool.submit(prepare_worker,j['id'])
            return dict(j,cache_hit=True)
        if sum(j['state'] in ('preparing','previewing','queued','running') for j in jobs.values())>=4: raise HTTPException(429,'任务队列已满')
        id=uuid.uuid4().hex; jobs[id]=dict(id=id,path=body.path,fingerprint=signature,state='preparing',message='提取首帧',progress=0)
        events[id]=threading.Event(); preview_stops[id]=threading.Event(); save(jobs[id]); pool.submit(prepare_worker,id)
        return dict(jobs[id])

frame_lock=threading.Lock()
@app.get('/api/jobs/{id}/frame')
def selection_frame(id: str, time: float=Query(0,ge=0,allow_inf_nan=False)):
    j=getjob(id)
    if not j.get('meta'): raise HTTPException(409,'请等待视频信息读取完成')
    if not unchanged(j): raise HTTPException(409,'原视频已改变，请重新选择视频')
    meta=j['meta']
    frame_index=round(time*meta['fps'])
    cached=DATA/id/'frames'/f'{frame_index}.jpg'
    if time>=meta['frames']/meta['fps'] or frame_index>=meta['frames']: raise HTTPException(400,'时间超出视频范围')
    if not frame_lock.acquire(blocking=False): raise HTTPException(429,'正在提取选帧，请稍后重试')
    try:
        if cached.is_file():
            cached.touch()
            return Response(cached.read_bytes(),media_type='image/jpeg',headers={'X-Preview-Cache':'HIT'})
        if frame_index==0 and (DATA/id/'first.jpg').is_file():
            return Response((DATA/id/'first.jpg').read_bytes(),media_type='image/jpeg',headers={'X-Preview-Cache':'HIT'})
        w,h=engine.preview_size(meta)
        import subprocess
        p=subprocess.run([engine.FFMPEG,'-hide_banner','-loglevel','error','-nostdin',
            '-threads','2','-ss',str(round(time*meta['fps'])/meta['fps']),'-i',str(source(j['path'])),
            '-vf',f'scale={w}:{h},setsar=1','-frames:v','1','-threads','2','-f','image2pipe','-vcodec','mjpeg','pipe:1'],capture_output=True,timeout=30)
        if p.returncode or not p.stdout: raise HTTPException(400,'无法提取此帧')
        with lock:
            getjob(id)
            if not unchanged(j): raise HTTPException(409,'原视频已改变，请重新选择视频')
            cached.parent.mkdir(exist_ok=True)
            temp=cached.with_suffix('.tmp');temp.write_bytes(p.stdout);temp.replace(cached)
            entries=sorted(cached.parent.glob('*.jpg'),key=lambda x:x.stat().st_mtime_ns)
            for old in entries[:-128]: old.unlink(missing_ok=True)
        return Response(p.stdout,media_type='image/jpeg',headers={'X-Preview-Cache':'MISS'})
    finally:frame_lock.release()

class SubjectSelection(BaseModel):
    roi: tuple[float,float,float,float]
    reference_time: float=Field(0,ge=0,allow_inf_nan=False)
    @model_validator(mode='after')
    def check_box(self):
        x,y,w,h=self.roi
        if not (0<=x<1 and 0<=y<1 and 0<w<=1-x and 0<h<=1-y): raise ValueError('无效主体框')
        return self

@app.post('/api/jobs/{id}/subject-check')
def check_subject(id: str, body: SubjectSelection):
    j=getjob(id)
    if not j.get('meta') or not unchanged(j): raise HTTPException(409,'请重新选择视频，等待参考帧就绪')
    meta=j['meta'];index=round(body.reference_time*meta['fps'])
    if index>=meta['frames']: raise HTTPException(400,'主体参考帧超出视频范围')
    if not frame_lock.acquire(blocking=False): raise HTTPException(429,'正在检查预览，请稍后重试')
    reader=None
    try:
        scale=min(1.,960/max(meta['width'],meta['height']))
        size=tuple(max(2,round(meta[k]*scale)) for k in ('width','height'))
        reader=engine.SourceFrames(source(j['path']),meta,size,lambda:False,index/meta['fps'],1)
        ok,frame=reader.read()
        if not ok: raise ValueError('无法解码主体参考帧')
        box,count=engine.validate_subject(frame,body.roi)
        tracker=engine.cv2.TrackerCSRT_create()
        if tracker.init(frame,box) is False: raise ValueError('主体跟踪初始化失败')
        with lock:
            getjob(id)
            update(id,subject_selection=body.model_dump())
        return {'valid':True,'features':count,'reference_time':index/meta['fps'],
                'message':'已接收选框，参考帧纹理检查通过；请确认框内是目标主体'}
    except ValueError as e: raise HTTPException(400,str(e))
    finally:
        if reader:reader.release()
        frame_lock.release()

@app.post('/api/jobs/{id}/render')
def render(id: str,options: Options):
    with lock:
        j=getjob(id)
        if j['state'] not in ('ready','previewing','done','failed','cancelled','interrupted') or not (DATA/id/'first.jpg').is_file(): raise HTTPException(409,'请等待首帧准备完成及当前导出结束')
        if sum(x['state'] in ('preparing','previewing','queued','running') for x in jobs.values())>=4 and j['state']!='previewing': raise HTTPException(429,'任务队列已满')
        if j.get('meta') and options.start>=j['meta']['frames']/j['meta']['fps']: raise HTTPException(400,'剪辑起点超出视频范围')
        if not unchanged(j): raise HTTPException(409,'原视频已改变，请重新选择视频以更新缓存')
        destination=export_directory(options.export_directory)
        try:
            destination.mkdir(parents=True,exist_ok=True)
            with tempfile.TemporaryFile(dir=destination): pass
        except OSError: raise HTTPException(400,'输出目录无法写入，请检查NAS目录权限和挂载')
        events[id].clear()
        update(id,state='queued',progress=0,message='等待原片导出',options=options.model_dump())
        preview_stops[id].set(); pool.submit(render_worker,id,options.model_dump())
    return getjob(id)

@app.post('/api/jobs/{id}/cancel')
def cancel(id: str):
    with lock:
        j=getjob(id); events[id].set()
        if j['state']=='ready': update(id,state='cancelled',message='任务已取消')
    return {'message':'取消已请求'}

class DeleteRequest(BaseModel):
    confirmed: bool=False

class DeleteOutput(BaseModel):
    file: str
    confirmed: bool=False

@app.post('/api/jobs/{id}/delete-output')
def delete_output(id: str, body: DeleteOutput):
    if not body.confirmed: raise HTTPException(400,'请确认删除所选输出视频')
    with lock:
        j=getjob(id)
        if j['state'] in ACTIVE: raise HTTPException(409,'请等待当前任务结束后删除输出')
        versions=results(j)
        selected=next((r for r in versions if r['file']==body.file),None)
        if selected is None: raise HTTPException(404,'输出不存在')
        target=export_file(selected) if selected.get('export_relative') else DATA/id/selected['file']
        if not selected.get('export_relative') and target.resolve().parent!=(DATA/id).resolve(): raise HTTPException(400,'无效输出路径')
        target.unlink(missing_ok=True)
        remaining=[r for r in versions if r['file']!=body.file]
        update(id,results=remaining,result=remaining[-1]['file'] if remaining else None,
               output=remaining[-1].get('output') if remaining else None,
               state='done' if remaining else 'ready',progress=100 if remaining else 0,
               message='已删除所选输出，预览缓存保留')
    return {'deleted':True}

@app.post('/api/jobs/{id}/delete')
def delete_job(id: str, body: DeleteRequest):
    if not body.confirmed: raise HTTPException(400,'请确认删除任务及其全部生成文件')
    with lock:
        j=getjob(id)
        if j['state'] in ('preparing','previewing','queued','running'): raise HTTPException(409,'请等待任务停止后删除')
        directory=(DATA/id).resolve()
        if directory.parent!=DATA: raise HTTPException(400,'无效任务路径')
        if frame_lock.locked(): raise HTTPException(409,'正在提取预览帧，请稍后删除')
        for r in results(j):
            if r.get('export_relative'): export_file(r).unlink(missing_ok=True)
        shutil.rmtree(directory)
        del jobs[id]; events.pop(id,None); preview_stops.pop(id,None)
    return {'deleted':True}

@app.get('/api/jobs/{id}/{asset}')
def asset(id: str,asset: str):
    j=getjob(id)
    if asset not in ['first.jpg','preview.mp4']+[r['file'] for r in results(j)]: raise HTTPException(404)
    r=next((r for r in results(j) if r['file']==asset),None)
    p=export_file(r) if r and r.get('export_relative') else DATA/id/asset
    if asset in ('first.jpg','preview.mp4') and not unchanged(j): raise HTTPException(409,'原视频已改变，请重新选择视频')
    if not p.is_file(): raise HTTPException(404)
    return FileResponse(p,filename=p.name if asset.startswith('result') else None)

app.mount('/',StaticFiles(directory=Path(__file__).parent/'web',html=True),name='web')
