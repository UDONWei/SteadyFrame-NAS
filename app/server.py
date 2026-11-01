import json
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator
from . import engine

MEDIA=Path(os.environ.get('MEDIA_ROOT','/media')).resolve()
DATA=Path(os.environ.get('DATA_ROOT','/data')).resolve()
DATA.mkdir(parents=True,exist_ok=True)
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

class Options(BaseModel):
    mode: Literal['translation','smooth','lock','subject']='smooth'
    smoothing: int=Field(30,ge=1,le=150)
    zoom: float=Field(1.1,ge=1,le=2)
    resolution: Literal[0,480,720,1080,1440,2160]=0
    format: Literal['mp4','mov','mkv','webm']='mp4'
    codec: Literal['h264','hevc','vp9']='h264'
    roi: tuple[float,float,float,float] | None=None
    @model_validator(mode='after')
    def validate_options(self):
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
        original=source(j['path']); meta=engine.inspect(original)
        engine.extract_first(original,folder/'first.jpg',meta)
        if events[id].is_set(): raise InterruptedError('任务已取消')
        update(id,state='previewing',frame_ready=True,meta=meta,message='首帧已就绪，可选主体或直接导出；后台生成720p预览',progress=0)
        def progress(value):
            with lock:
                if jobs[id]['state']=='previewing': update(id,progress=round(value,1))
        try:
            engine.normalize(original,folder/'preview.mp4',meta,
                lambda: events[id].is_set() or preview_stops[id].is_set(),progress)
            with lock:
                update(id,preview_ready=True)
                if jobs[id]['state']=='previewing': update(id,state='ready',message='720p预览已就绪；导出读取原片',progress=0)
        except Exception as e:
            with lock:
                if events[id].is_set(): update(id,state='cancelled',message='任务已取消')
                elif jobs[id]['state']=='previewing':
                    update(id,state='ready',preview_error=str(e),message='播放预览未完成，仍可用首帧选主体并从原片导出',progress=0)
    except Exception as e: update(id,state='cancelled' if events[id].is_set() else 'failed',message=str(e))

def render_worker(id,options):
    try:
        if events[id].is_set(): raise InterruptedError('任务已取消')
        update(id,state='running',message='分析运动')
        j=getjob(id); output=DATA/id/('result.'+options['format'])
        meta=engine.stabilize(source(j['path']),DATA/id/'preview.mp4',output,options,
            lambda message,progress:update(id,message=message,progress=round(progress,1)),events[id].is_set)
        update(id,state='done',message='导出完成',progress=100,result=output.name,output=meta)
    except Exception as e: update(id,state='cancelled' if events[id].is_set() else 'failed',message=str(e))

@app.get('/api/health')
def health(): return {'status':'ok','version':'0.2.0'}

@app.get('/api/files')
def files(path: str=''):
    folder=(MEDIA/path).resolve()
    if not folder.is_relative_to(MEDIA) or not folder.is_dir(): raise HTTPException(400,'无效目录')
    result=[]
    for p in sorted(folder.iterdir(),key=lambda p:(not p.is_dir(),p.name.lower())):
        if not p.resolve().is_relative_to(MEDIA): continue
        if p.is_dir() or p.suffix.lower() in EXT:
            result.append(dict(name=p.name,path=p.relative_to(MEDIA).as_posix(),directory=p.is_dir()))
        if len(result)>=2000: break
    return result

@app.get('/api/jobs')
def list_jobs():
    with lock: return list(reversed([dict(j) for j in jobs.values()]))

@app.post('/api/prepare')
def prepare(body: Prepare):
    source(body.path)
    with lock:
        if sum(j['state'] in ('preparing','previewing','queued','running') for j in jobs.values())>=4: raise HTTPException(429,'任务队列已满')
        id=uuid.uuid4().hex; jobs[id]=dict(id=id,path=body.path,state='preparing',message='提取首帧',progress=0)
        events[id]=threading.Event(); preview_stops[id]=threading.Event(); save(jobs[id]); pool.submit(prepare_worker,id)
        return dict(jobs[id])

@app.post('/api/jobs/{id}/render')
def render(id: str,options: Options):
    with lock:
        if getjob(id)['state'] not in ('ready','previewing'): raise HTTPException(409,'请等待首帧准备完成或新建任务')
        update(id,state='queued',message='等待原片导出',options=options.model_dump())
        preview_stops[id].set(); pool.submit(render_worker,id,options.model_dump())
    return getjob(id)

@app.post('/api/jobs/{id}/cancel')
def cancel(id: str):
    with lock:
        j=getjob(id); events[id].set()
        if j['state']=='ready': update(id,state='cancelled',message='任务已取消')
    return {'message':'取消已请求'}

@app.get('/api/jobs/{id}/{asset}')
def asset(id: str,asset: str):
    j=getjob(id)
    if asset not in ('first.jpg','preview.mp4',j.get('result')): raise HTTPException(404)
    p=DATA/id/asset
    if not p.is_file(): raise HTTPException(404)
    return FileResponse(p,filename=p.name if asset.startswith('result.') else None)

app.mount('/',StaticFiles(directory=Path(__file__).parent/'web',html=True),name='web')
