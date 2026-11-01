import hashlib
import threading
import time
import subprocess
import cv2
import numpy as np
import pytest
from app import engine

def wait_job(client,id,state):
    for _ in range(400):
        j=next(j for j in client.get('/api/jobs').json() if j['id']==id)
        if j['state']==state:return j
        assert j['state'] not in ('failed','cancelled'),j
        time.sleep(.03)
    pytest.fail(f'timed out waiting for {state}')

def test_4k_preview_and_original_export(tmp_path):
    source=tmp_path/'4k.mp4'
    engine.run(['-y','-f','lavfi','-i','testsrc2=size=3840x2160:rate=6:duration=1',
                '-f','lavfi','-i','sine=frequency=440:duration=0.5',
                '-c:v','libx264','-crf','12','-preset','ultrafast','-threads','2','-c:a','aac',str(source)])
    digest=hashlib.sha256(source.read_bytes()).hexdigest()
    meta=engine.inspect(source);first=tmp_path/'first.jpg';proxy=tmp_path/'preview.mp4'
    engine.extract_first(source,first,meta)
    assert cv2.imread(str(first)).shape[:2]==(720,1280)
    engine.normalize(source,proxy,meta)
    assert engine.inspect(proxy)['width']==1280
    assert engine.inspect(proxy)['height']==720
    # Remove proxy: original export must be completely independent of it.
    proxy.unlink()
    output=tmp_path/'result.mp4'
    engine.stabilize(source,proxy,output,dict(mode='translation',format='mp4',codec='h264',resolution=0,
        smoothing=1,zoom=1),lambda *a:None,lambda:False)
    result=engine.inspect(output)
    assert (result['width'],result['height'],result['frames'])==(3840,2160,6)
    # Audio shorter than video must not truncate the picture; audio is retained.
    engine.run(['-i',str(output),'-map','0:a:0','-f','null','-'])
    assert hashlib.sha256(source.read_bytes()).hexdigest()==digest

def test_preview_cancel_has_no_playable_partial(tmp_path):
    source=tmp_path/'short.mp4'
    engine.run(['-y','-f','lavfi','-i','testsrc2=size=320x180:rate=12:duration=1',
                '-c:v','libx264','-threads','2',str(source)])
    with pytest.raises(InterruptedError):
        engine.normalize(source,tmp_path/'preview.mp4',cancelled=lambda:True)
    assert not (tmp_path/'preview.mp4').exists()
    assert not (tmp_path/'preview-building.mp4').exists()

def test_rotated_portrait_coordinates(tmp_path):
    base=tmp_path/'base.mp4';source=tmp_path/'portrait.mp4'
    engine.run(['-y','-f','lavfi','-i','testsrc2=size=1920x1080:rate=6:duration=1',
                '-c:v','libx264','-threads','2',str(base)])
    help_text=subprocess.run([engine.FFMPEG,'-h','full'],capture_output=True,text=True).stdout
    if '-display_rotation' in help_text:
        engine.run(['-y','-display_rotation','90','-i',str(base),'-c','copy',str(source)])
    else:
        engine.run(['-y','-i',str(base),'-c','copy','-metadata:s:v:0','rotate=90',str(source)])
    meta=engine.inspect(source)
    assert (meta['width'],meta['height'])==(1080,1920)
    assert engine.preview_size(meta)==(720,1280)
    engine.extract_first(source,tmp_path/'first.jpg',meta)
    frames=engine.SourceFrames(source,meta,(720,1280),lambda:False)
    try: ok,frame=frames.read()
    finally: frames.release()
    assert ok and frame.shape[:2]==(1280,720)
    assert np.mean(np.abs(frame.astype(float)-cv2.imread(str(tmp_path/'first.jpg')).astype(float)))<8
