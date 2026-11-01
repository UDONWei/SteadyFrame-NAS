"""Bounded-frame-memory, two-pass CPU stabilization. No source writes."""
import os
import subprocess
import tempfile
import threading
from pathlib import Path
import cv2
import numpy as np

cv2.setNumThreads(2)
FFMPEG = os.environ.get('FFMPEG', 'ffmpeg')

def run(args):
    p = subprocess.run([FFMPEG, '-hide_banner', '-loglevel', 'error', '-nostdin', *args], capture_output=True)
    if p.returncode:
        raise ValueError(p.stderr.decode(errors='replace')[-2000:])

def preview_size(meta):
    w,h=meta['width'],meta['height']
    scale=min(1.,1280/max(w,h),720/min(w,h))
    return max(2,int(w*scale)//2*2),max(2,int(h*scale)//2*2)

def video_filter(meta, size):
    return f"fps={meta['fps']:.12g},scale={size[0]}:{size[1]},setsar=1"

def extract_first(source, target, meta):
    w,h=preview_size(meta)
    run(['-y','-threads','2','-i',str(source),'-map','0:v:0','-an',
         '-vf',f'scale={w}:{h},setsar=1','-frames:v','1','-q:v','3','-threads','2',str(target)])

def normalize(source, target, meta=None, cancelled=lambda:False, progress=lambda value:None):
    """Playback-only 720p proxy. Never used as export input."""
    meta=meta or inspect(source)
    temp=target.with_name('preview-building.mp4')
    args=[FFMPEG,'-hide_banner','-loglevel','error','-nostdin','-y','-threads','2',
          '-i',str(source),'-map','0:v:0','-map','0:a:0?',
          '-vf',video_filter(meta,preview_size(meta)),'-c:v','libx264','-crf','25',
          '-preset','ultrafast','-pix_fmt','yuv420p','-threads','2','-c:a','aac',
          '-movflags','+faststart','-progress','pipe:1','-nostats',str(temp)]
    with tempfile.TemporaryFile() as log:
        p=subprocess.Popen(args,stdout=subprocess.PIPE,stderr=log,text=True)
        latest=[0]
        def read_progress():
            for line in p.stdout:
                if line.startswith('frame='):
                    try: latest[0]=int(line.split('=',1)[1])
                    except ValueError: pass
        reader=threading.Thread(target=read_progress,daemon=True); reader.start()
        try:
            while True:
                if cancelled(): raise InterruptedError('预览已停止')
                try: p.wait(timeout=.25); break
                except subprocess.TimeoutExpired:
                    progress(min(99,100*latest[0]/max(1,meta['frames'])))
            if p.returncode:
                log.seek(0); raise ValueError(log.read().decode(errors='replace')[-2000:])
            if cancelled(): raise InterruptedError('预览已停止')
            temp.replace(target); progress(100)
        finally:
            if p.poll() is None: p.kill(); p.wait()
            reader.join(); p.stdout.close()
            temp.unlink(missing_ok=True)

class SourceFrames:
    """Decode original through FFmpeg, same orientation and CFR timeline in both passes."""
    def __init__(self, source, meta, size, cancelled):
        self.size=size; self.cancelled=cancelled
        self.log=tempfile.TemporaryFile()
        self.p=subprocess.Popen([FFMPEG,'-hide_banner','-loglevel','error','-nostdin',
            '-threads','2','-i',str(source),'-map','0:v:0','-an',
            '-vf',video_filter(meta,size),'-pix_fmt','bgr24','-threads','2',
            '-f','rawvideo','pipe:1'],stdout=subprocess.PIPE,stderr=self.log)
        self.done=threading.Event()
        def watch():
            while not self.done.wait(.1):
                if cancelled():
                    if self.p.poll() is None: self.p.kill()
                    return
        self.watcher=threading.Thread(target=watch,daemon=True);self.watcher.start()
    def read(self):
        if self.cancelled(): raise InterruptedError('任务已取消')
        w,h=self.size; expected=w*h*3; data=bytearray()
        while len(data)<expected:
            part=self.p.stdout.read(expected-len(data))
            if not part: break
            data.extend(part)
        if self.cancelled(): raise InterruptedError('任务已取消')
        if not data:
            if self.p.wait()!=0:
                self.log.seek(0);raise ValueError(self.log.read().decode(errors='replace')[-2000:])
            return False,None
        if len(data)!=expected: raise ValueError('原始视频解码不完整')
        return True,np.frombuffer(data,np.uint8).reshape(h,w,3)
    def release(self):
        self.done.set()
        if self.p.poll() is None: self.p.kill()
        self.p.wait();self.watcher.join();self.p.stdout.close();self.log.close()

def inspect(path):
    cap = cv2.VideoCapture(str(path))
    try:
        cap.set(cv2.CAP_PROP_ORIENTATION_AUTO,1)
        ok, frame = cap.read()
        if not ok: raise ValueError('无法解码视频首帧')
        fps = cap.get(cv2.CAP_PROP_FPS)
        if not np.isfinite(fps) or fps <= 0: raise ValueError('无效帧率')
        w,h=frame.shape[1],frame.shape[0]
        sar_den=cap.get(cv2.CAP_PROP_SAR_DEN)
        sar=cap.get(cv2.CAP_PROP_SAR_NUM)/sar_den if sar_den else 1.
        if not np.isfinite(sar) or sar<=0: sar=1.
        if int(cap.get(cv2.CAP_PROP_ORIENTATION_META))%180:
            h=round(h*sar)
        else: w=round(w*sar)
        return dict(width=max(2,w//2*2), height=max(2,h//2*2), fps=fps,
                    frames=int(cap.get(cv2.CAP_PROP_FRAME_COUNT)))
    finally: cap.release()

def smooth(a, radius):
    return np.stack([np.convolve(np.pad(a[:, i], (radius, radius), mode='edge'),
                     np.ones(2*radius+1)/(2*radius+1), mode='valid') for i in range(a.shape[1])], axis=1)

def matrix(p):
    x, y, angle = p
    c, s = np.cos(angle), np.sin(angle)
    return np.array([[c, -s, x], [s, c, y], [0, 0, 1]], dtype=np.float64)

def stabilize(source, normalized, output, options, report, cancelled):
    # normalized retained for API compatibility; it is deliberately never opened.
    def check():
        if cancelled(): raise InterruptedError('任务已取消')
    check()
    meta = inspect(source)
    w, h, fps = meta['width'], meta['height'], meta['fps']
    scale = min(1., 960 / max(w, h))
    size = (max(2, round(w*scale)), max(2, round(h*scale)))
    mode = options['mode']
    cap = SourceFrames(source,meta,size,cancelled)
    poses, cumulative, tracker, start = [], np.eye(3), None, None
    previous = None
    try:
        while True:
            check()
            ok, frame = cap.read()
            if not ok: break
            small = cv2.resize(frame, size)
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            if mode == 'subject':
                if tracker is None:
                    x,y,rw,rh = options['roi']
                    box = tuple(int(v) for v in (x*size[0],y*size[1],rw*size[0],rh*size[1]))
                    if min(box[2:]) < 8: raise ValueError('主体框过小，请增大选框')
                    tracker = cv2.TrackerCSRT_create(); tracker.init(small, box)
                    start = np.array([box[0]+box[2]/2,box[1]+box[3]/2])
                else:
                    found, box = tracker.update(small)
                    if not found: raise ValueError(f'第 {len(poses)} 帧主体跟踪丢失，请改选主体或缩短片段')
                center = np.array([box[0]+box[2]/2,box[1]+box[3]/2])
                poses.append([*(center-start),0.])
            else:
                if previous is not None:
                    pts = cv2.goodFeaturesToTrack(previous, 300, .01, 15)
                    if pts is None or len(pts)<8: raise ValueError('画面纹理不足，无法可靠估计运动')
                    nxt, status, _ = cv2.calcOpticalFlowPyrLK(previous, gray, pts, None)
                    if nxt is None: raise ValueError('光流估计失败')
                    a,b = pts[status.ravel()==1],nxt[status.ravel()==1]
                    if len(a)<8: raise ValueError('有效跟踪点不足')
                    if mode == 'translation':
                        delta=np.eye(3); delta[:2,2]=np.median((b-a).reshape(-1,2),axis=0)
                    else:
                        affine, inliers=cv2.estimateAffinePartial2D(a,b,method=cv2.RANSAC)
                        if affine is None or inliers.sum()<6: raise ValueError('场景切换或运动估计失败')
                        angle=np.arctan2(affine[1,0],affine[0,0])
                        delta=matrix([affine[0,2],affine[1,2],angle])
                    cumulative=delta@cumulative
                poses.append([cumulative[0,2],cumulative[1,2],np.arctan2(cumulative[1,0],cumulative[0,0])])
            previous=gray
            if len(poses)%20==0: report('分析运动',len(poses)/max(1,meta['frames'])*45)
    finally: cap.release()
    if not poses: raise ValueError('空视频')
    poses=np.asarray(poses); poses[:,2]=np.unwrap(poses[:,2])
    desired=np.zeros_like(poses) if mode in ('lock','subject') else smooth(poses,options['smoothing'])
    height=options['resolution']
    ow,oh=(w//2*2,h//2*2) if height==0 else (max(2,round(w/h*height/2)*2),height)
    codec=options['codec']; fmt=options['format']
    enc={'h264':['-c:v','libx264','-crf','18','-preset','fast'],
         'hevc':['-c:v','libx265','-crf','20','-preset','fast','-x265-params','pools=2'],
         'vp9':['-c:v','libvpx-vp9','-crf','32','-b:v','0']}[codec]
    temp=output.with_name('render.'+fmt)
    cmd=[FFMPEG,'-hide_banner','-loglevel','error','-y','-f','rawvideo','-pix_fmt','bgr24',
         '-s',f'{ow}x{oh}','-r',str(fps),'-i','pipe:0','-i',str(source),
         '-map','0:v:0','-map','1:a:0?',*enc,'-threads','2','-pix_fmt','yuv420p',
         '-c:a','libopus' if fmt=='webm' else 'aac','-af','apad','-t',f'{len(poses)/fps:.12g}']
    if fmt in ('mp4','mov'): cmd+=['-movflags','+faststart']
    cap=SourceFrames(source,meta,(w,h),cancelled)
    coordinates=np.diag([w/size[0],h/size[1],1.])
    try:
        with open(output.parent/'encode.log','wb') as log:
            p=subprocess.Popen([*cmd,str(temp)],stdin=subprocess.PIPE,stderr=log)
            try:
                for i,(pose,target) in enumerate(zip(poses,desired)):
                    check(); ok,frame=cap.read()
                    if not ok: raise ValueError('第二遍解码提前结束')
                    warp=coordinates@matrix(target)@np.linalg.inv(matrix(pose))@np.linalg.inv(coordinates)
                    zoom=options['zoom']; z=np.array([[zoom,0,(1-zoom)*w/2],[0,zoom,(1-zoom)*h/2],[0,0,1]])
                    stabilized=cv2.warpAffine(frame,(z@warp)[:2],(w,h),borderMode=cv2.BORDER_CONSTANT)
                    p.stdin.write(cv2.resize(stabilized,(ow,oh)).tobytes())
                    if i%20==0: report('编码导出',45+54*i/len(poses))
                p.stdin.close()
                if p.wait()!=0: raise ValueError((output.parent/'encode.log').read_text(errors='replace')[-1500:])
            finally:
                if p.poll() is None: p.kill(); p.wait()
    finally: cap.release()
    check(); temp.replace(output)
    return dict(**meta,output_width=ow,output_height=oh)
