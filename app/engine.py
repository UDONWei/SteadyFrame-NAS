"""Bounded-frame-memory, two-pass CPU stabilization. No source writes."""
import os
import subprocess
from pathlib import Path
import cv2
import numpy as np

cv2.setNumThreads(2)
FFMPEG = os.environ.get('FFMPEG', 'ffmpeg')

def run(args):
    p = subprocess.run([FFMPEG, '-hide_banner', '-loglevel', 'error', '-nostdin', *args], capture_output=True)
    if p.returncode:
        raise ValueError(p.stderr.decode(errors='replace')[-2000:])

def normalize(source, target):
    # CFR intermediate also normalizes rotation and pixel aspect before ROI selection.
    run(['-y', '-i', str(source), '-map', '0:v:0', '-an', '-vf',
         'scale=trunc(iw*sar/2)*2:trunc(ih/2)*2,setsar=1', '-fps_mode', 'cfr',
         '-c:v', 'libx264', '-crf', '18', '-preset', 'fast', '-threads', '2', str(target)])

def inspect(path):
    cap = cv2.VideoCapture(str(path))
    try:
        ok, frame = cap.read()
        if not ok: raise ValueError('无法解码视频首帧')
        fps = cap.get(cv2.CAP_PROP_FPS)
        if not np.isfinite(fps) or fps <= 0: raise ValueError('无效帧率')
        return dict(width=frame.shape[1], height=frame.shape[0], fps=fps,
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
    def check():
        if cancelled(): raise InterruptedError('任务已取消')
    meta = inspect(normalized)
    w, h, fps = meta['width'], meta['height'], meta['fps']
    scale = min(1., 960 / max(w, h))
    size = (max(2, round(w*scale)), max(2, round(h*scale)))
    mode = options['mode']
    cap = cv2.VideoCapture(str(normalized))
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
    poses=np.asarray(poses); poses[:,2]=np.unwrap(poses[:,2]); poses[:,:2]/=scale
    desired=np.zeros_like(poses) if mode in ('lock','subject') else smooth(poses,options['smoothing'])
    height=options['resolution']
    ow,oh=(w//2*2,h//2*2) if height==0 else (max(2,round(w/h*height/2)*2),height)
    codec=options['codec']; fmt=options['format']
    enc={'h264':['-c:v','libx264','-crf','20','-preset','fast'],
         'hevc':['-c:v','libx265','-crf','24','-preset','fast','-x265-params','pools=2'],
         'vp9':['-c:v','libvpx-vp9','-crf','32','-b:v','0']}[codec]
    temp=output.with_name('render.'+fmt)
    cmd=[FFMPEG,'-hide_banner','-loglevel','error','-y','-f','rawvideo','-pix_fmt','bgr24',
         '-s',f'{ow}x{oh}','-r',str(fps),'-i','pipe:0','-i',str(source),
         '-map','0:v:0','-map','1:a:0?',*enc,'-threads','2','-pix_fmt','yuv420p',
         '-c:a','libopus' if fmt=='webm' else 'aac','-shortest']
    if fmt in ('mp4','mov'): cmd+=['-movflags','+faststart']
    cap=cv2.VideoCapture(str(normalized))
    try:
        with open(output.parent/'encode.log','wb') as log:
            p=subprocess.Popen([*cmd,str(temp)],stdin=subprocess.PIPE,stderr=log)
            try:
                for i,(pose,target) in enumerate(zip(poses,desired)):
                    check(); ok,frame=cap.read()
                    if not ok: raise ValueError('第二遍解码提前结束')
                    warp=matrix(target)@np.linalg.inv(matrix(pose))
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
