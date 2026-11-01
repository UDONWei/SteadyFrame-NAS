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

@pytest.mark.parametrize('mode',['translation','subject'])
def test_translation_reduces_jitter(clip,tmp_path,mode):
    source,normalized=clip;out=tmp_path/'result.mp4'
    engine.stabilize(source,normalized,out,dict(mode=mode,format='mp4',codec='h264',resolution=0,smoothing=30,zoom=1,roi=(.2,.2,.6,.6)),lambda *a:None,lambda:False)
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


def test_subject_middle_frame_and_trim(clip,tmp_path):
    source,proxy=clip;out=tmp_path/'trim.mp4'
    engine.stabilize(source,proxy,out,dict(mode='subject',format='mp4',codec='h264',
        resolution=0,smoothing=30,zoom=1,roi=(.2,.2,.6,.6),start=.25,end=1.25,reference_time=.75),lambda *a:None,lambda:False)
    meta=engine.inspect(out)
    assert meta['frames']==24 and meta['width']==320
    cap=cv2.VideoCapture(str(out));previous=None;motions=[]
    while True:
        ok,f=cap.read()
        if not ok:break
        gray=cv2.cvtColor(f,cv2.COLOR_BGR2GRAY).astype(np.float32)[30:-30,40:-40]
        if previous is not None: motions.append(abs(cv2.phaseCorrelate(previous,gray)[0][0]))
        previous=gray
    cap.release()
    assert np.median(motions)<1.0


def test_backward_chunks_cover_each_frame(monkeypatch):
    class Reader:
        def __init__(self,source,meta,size,cancelled,start,count): self.i=round(start*10);self.end=self.i+count
        def read(self):
            if self.i>=self.end:return False,None
            i=self.i;self.i+=1;return True,i
        def release(self):pass
    monkeypatch.setattr(engine,'SourceFrames',Reader)
    frames=list(engine.analysis_frames(None,{'fps':10},None,lambda:False,0,120,100,True))
    assert [i for i,f,r in frames]==list(range(100,120))+list(range(100,-1,-1))
    assert all(i==f for i,f,r in frames)
    assert sum(r for i,f,r in frames)==2


@pytest.mark.parametrize('offset,angle,limit',[(18,.08,2),(240,.35,1.3),(0,0,2),(-170,-.5,1.5)])
def test_auto_crop_prevents_new_border_pixels(offset,angle,limit):
    poses=np.array([[0,0,0],[offset,20,angle],[-offset,-10,-angle]])
    desired=np.zeros_like(poses)
    transforms,zoom,strength=engine.plan_crop(poses,desired,(320,180),(320,180),
        dict(zoom=1,auto_crop=True,max_auto_zoom=limit))
    assert 1<=zoom<=limit+1e-8 and 0<=strength<=1
    if abs(offset)>150:assert strength<1
    center=np.array([159.5,89.5])
    z=np.array([[zoom,0,(1-zoom)*center[0]],[0,zoom,(1-zoom)*center[1]],[0,0,1]])
    source=np.full((180,320,3),255,np.uint8)
    for transform in transforms:
        image=engine.render_frame(source,z@transform,(320,180))
        assert image.min()==255


def test_manual_zoom_and_auto_metadata(clip,tmp_path):
    poses=np.array([[0.,0,0],[25,0,0]])
    transforms,zoom,strength=engine.plan_crop(poses,np.zeros_like(poses),(320,180),(320,180),
        dict(zoom=1.25,auto_crop=False))
    assert zoom==1.25 and strength==1
    source,proxy=clip
    meta=engine.stabilize(source,proxy,tmp_path/'auto.mp4',dict(mode='translation',format='mp4',codec='h264',
        resolution=0,smoothing=8,zoom=1,auto_crop=True,max_auto_zoom=2),lambda *a:None,lambda:False)
    assert 1<meta['effective_zoom']<=2 and meta['auto_crop']
    assert meta['retained_area']==pytest.approx(1/meta['effective_zoom']**2)


def test_subject_selection_rejects_blank_and_tiny_regions():
    blank=np.full((180,320,3),120,np.uint8)
    with pytest.raises(ValueError,match='纹理不足'):
        engine.validate_subject(blank,(.2,.2,.5,.5))
    with pytest.raises(ValueError,match='主体框过小'):
        engine.validate_subject(blank,(.2,.2,.01,.01))
    textured=np.random.default_rng(81).integers(0,255,blank.shape,dtype=np.uint8)
    box,count=engine.validate_subject(textured,(.2,.2,.5,.5))
    assert box==(64,36,160,90) and count>=6


def test_subject_stops_after_persistent_unreliable_motion(clip,tmp_path,monkeypatch):
    monkeypatch.setattr(engine,'subject_step',lambda *a:None)
    source,proxy=clip
    with pytest.raises(ValueError,match='主体运动特征连续不可靠'):
        engine.stabilize(source,proxy,tmp_path/'bad.mp4',dict(mode='subject',roi=(.2,.2,.6,.6),
            smoothing=30,format='mp4',codec='h264',resolution=0,zoom=1.1),lambda *a:None,lambda:False)
    assert not (tmp_path/'bad.mp4').exists()


def test_high_quality_downscale_reduces_texture_crawling():
    width,height=960,540;y,x=np.mgrid[:height,:width]
    old,new=[],[]
    for shift in np.linspace(0,1,12):
        plane=127+50*np.sin(2*np.pi*(x-shift)*.32)+45*np.cos(2*np.pi*(y-shift*.3)*.27)
        frame=np.clip(plane,0,255).astype(np.uint8)
        transform=np.array([[1.,0,-shift],[0,1.,-shift*.3],[0,0,1.]])
        legacy=cv2.warpAffine(frame,transform[:2],(width,height))
        legacy=cv2.resize(legacy,(320,180))
        improved=engine.render_frame(frame,transform,(320,180))
        old.append(legacy[10:-10,10:-10].astype(float))
        new.append(improved[10:-10,10:-10].astype(float))
    assert np.mean(np.std(new,axis=0))<np.mean(np.std(old,axis=0))*.25
