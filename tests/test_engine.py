import hashlib
import os
import cv2
import numpy as np
import pytest
from app import engine

@pytest.fixture
def clip(tmp_path):
    source=tmp_path/'source.avi'
    rng=np.random.default_rng(17)
    base=np.zeros((180,320,3),np.uint8)
    for x,y in rng.integers([20,20],[300,160],(120,2)):
        cv2.circle(base,(int(x),int(y)),3,(220,180,120),-1)
    writer=cv2.VideoWriter(str(source),cv2.VideoWriter_fourcc(*'MJPG'),24,(320,180))
    for i in range(36):
        dx=0 if i==0 else (5 if i%2 else -5)
        writer.write(cv2.warpAffine(base,np.float32([[1,0,dx],[0,1,0]]),(320,180)))
    writer.release()
    normalized=tmp_path/'preview.mp4';engine.normalize(source,normalized)
    return source,normalized

@pytest.mark.parametrize('mode,fmt,codec',[('translation','mp4','h264'),('smooth','mov','h264'),('lock','mkv','hevc'),('subject','webm','vp9')])
def test_export_and_readonly(clip,tmp_path,mode,fmt,codec):
    source,normalized=clip; before=hashlib.sha256(source.read_bytes()).hexdigest()
    output=tmp_path/('result.'+fmt)
    options=dict(mode=mode,format=fmt,codec=codec,resolution=480,smoothing=8,zoom=1.1,roi=(.3,.25,.3,.4))
    engine.stabilize(source,normalized,output,options,lambda *args:None,lambda:False)
    meta=engine.inspect(output)
    assert meta['height']==480 and meta['frames']==36
    assert hashlib.sha256(source.read_bytes()).hexdigest()==before

def test_translation_reduces_jitter(clip,tmp_path):
    source,normalized=clip;out=tmp_path/'result.mp4'
    engine.stabilize(source,normalized,out,dict(mode='translation',format='mp4',codec='h264',resolution=0,smoothing=8,zoom=1),lambda *a:None,lambda:False)
    def motion(path):
        cap=cv2.VideoCapture(str(path));prev=None;values=[]
        while True:
            ok,f=cap.read()
            if not ok:break
            gray=cv2.cvtColor(f,cv2.COLOR_BGR2GRAY).astype(np.float32)[25:-25,30:-30]
            if prev is not None:values.append(abs(cv2.phaseCorrelate(prev,gray)[0][0]))
            prev=gray
        cap.release();return np.median(values)
    assert motion(out)<motion(normalized)*.4

def test_cancel(clip,tmp_path):
    with pytest.raises(InterruptedError):
        engine.stabilize(*clip,tmp_path/'out.mp4',dict(mode='smooth'),lambda *a:None,lambda:True)
