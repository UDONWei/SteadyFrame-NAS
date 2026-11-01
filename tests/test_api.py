import importlib
import pytest
import time
import threading
from fastapi.testclient import TestClient

@pytest.fixture
def client(tmp_path,monkeypatch):
    media=tmp_path/'media';media.mkdir();(media/'a.mp4').write_bytes(b'bad')
    monkeypatch.setenv('MEDIA_ROOT',str(media));monkeypatch.setenv('DATA_ROOT',str(tmp_path/'data'))
    from app import server
    importlib.reload(server)
    return TestClient(server.app)

def test_paths_and_options(client):
    assert client.get('/api/health').status_code==200
    assert client.get('/api/files').json()[0]['name']=='a.mp4'
    assert client.get('/api/files',params={'path':'..'}).status_code==400
    assert client.post('/api/prepare',json={'path':'../../secret'}).status_code==400
    assert client.post('/api/jobs/missing/render',json={'mode':'subject'}).status_code==422
    assert client.post('/api/jobs/missing/render',json={'format':'webm','codec':'h264'}).status_code==422
    assert client.post('/api/jobs/missing/render',json={'mode':'subject','roi':[.9,.9,.5,.5]}).status_code==422

def test_job_lifecycle(client):
    import cv2
    import numpy as np
    from app import server
    path=server.MEDIA/'demo.avi'
    rng=np.random.default_rng(9)
    frame=rng.integers(0,255,(120,160,3),dtype=np.uint8)
    writer=cv2.VideoWriter(str(path),cv2.VideoWriter_fourcc(*'MJPG'),12,(160,120))
    for i in range(12): writer.write(frame)
    writer.release()
    job=client.post('/api/prepare',json={'path':'demo.avi'}).json()
    def wait_for(state):
        for _ in range(300):
            current=next(j for j in client.get('/api/jobs').json() if j['id']==job['id'])
            if current['state']==state:return current
            assert current['state']!='failed',current
            time.sleep(.05)
        pytest.fail('job timed out')
    wait_for('ready')
    assert client.get(f"/api/jobs/{job['id']}/first.jpg").status_code==200
    assert client.post(f"/api/jobs/{job['id']}/render",json={'mode':'translation'}).status_code==200
    assert client.post(f"/api/jobs/{job['id']}/render",json={}).status_code==409
    done=wait_for('done')
    result=client.get(f"/api/jobs/{job['id']}/{done['result']}")
    assert result.status_code==200 and len(result.content)>1000

def test_first_frame_available_before_proxy_and_export_preempts(client,monkeypatch):
    from app import server,engine
    from test_preview import wait_job
    source=server.MEDIA/'short.mp4'
    engine.run(['-y','-f','lavfi','-i','testsrc2=size=320x180:rate=12:duration=1',
                '-c:v','libx264','-threads','2',str(source)])
    started=threading.Event();stopped=threading.Event()
    def slow_preview(source,target,meta,cancelled,progress):
        started.set()
        for _ in range(200):
            if cancelled():stopped.set();raise InterruptedError('preempted')
            time.sleep(.02)
        raise RuntimeError('preview should have been interrupted')
    monkeypatch.setattr(engine,'normalize',slow_preview)
    job=client.post('/api/prepare',json={'path':'short.mp4'}).json()
    try:
        assert started.wait(5)
        j=wait_job(client,job['id'],'previewing')
        assert j['frame_ready'] and not j.get('preview_ready')
        assert client.get(f"/api/jobs/{j['id']}/first.jpg").status_code==200
        assert client.get(f"/api/jobs/{j['id']}/preview.mp4").status_code==404
        assert client.post(f"/api/jobs/{j['id']}/render",json={'mode':'translation'}).status_code==200
        assert stopped.wait(2)
        assert wait_job(client,j['id'],'done')['output']['output_width']==320
    finally:
        server.events[job['id']].set()
