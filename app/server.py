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
        if j['state'] in ('preparing','queued','running'): j.update(state='interrupted',message='服务重启，任务中断；请重新准备')
        jobs[j['id']]=j; events[j['id']]=threading.Event()
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
        engine.normalize(source(j['path']),folder/'preview.mp4')
        if events[id].is_set(): raise InterruptedError('任务已取消')
        meta=engine.inspect(folder/'preview.mp4')
        cap=engine.cv2.VideoCapture(str(folder/'preview.mp4'))
        try: ok,frame=cap.read()
        finally: cap.release()
        if not ok: raise ValueError('无法读取预览')
        engine.cv2.imwrite(str(folder/'first.jpg'),frame)
        update(id,state='ready',meta=meta,message='可选择防抖方式及主体',progress=0)
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
def health(): return {'status':'ok'}

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
        if sum(j['state'] in ('preparing','queued','running') for j in jobs.values())>=4: raise HTTPException(429,'任务队列已满')
        id=uuid.uuid4().hex; jobs[id]=dict(id=id,path=body.path,state='preparing',message='生成标准化预览',progress=0)
        events[id]=threading.Event(); save(jobs[id]); pool.submit(prepare_worker,id)
        return dict(jobs[id])

@app.post('/api/jobs/{id}/render')
def render(id: str,options: Options):
    with lock:
        if getjob(id)['state']!='ready': raise HTTPException(409,'请等待预览准备完成或新建任务')
        update(id,state='queued',options=options.model_dump()); pool.submit(render_worker,id,options.model_dump())
    return getjob(id)

@app.post('/api/jobs/{id}/cancel')
def cancel(id: str):
    getjob(id); events[id].set(); return {'message':'取消已请求；预览转码将在当前步骤结束后取消'}

@app.get('/api/jobs/{id}/{asset}')
def asset(id: str,asset: str):
    j=getjob(id)
    if asset not in ('first.jpg','preview.mp4',j.get('result')): raise HTTPException(404)
    p=DATA/id/asset
    if not p.is_file(): raise HTTPException(404)
    return FileResponse(p,filename=p.name if asset.startswith('result.') else None)

app.mount('/',StaticFiles(directory=Path(__file__).parent/'web',html=True),name='web')
