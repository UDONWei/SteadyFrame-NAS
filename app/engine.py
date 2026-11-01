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
    def __init__(self, source, meta, size, cancelled, start=0, count=None):
        self.size=size; self.cancelled=cancelled
        self.log=tempfile.TemporaryFile()
        self.p=subprocess.Popen([FFMPEG,'-hide_banner','-loglevel','error','-nostdin',
            '-threads','2','-ss',str(start),'-i',str(source),'-map','0:v:0','-an',
            *(['-frames:v',str(count)] if count is not None else []),'-vf',video_filter(meta,size),'-pix_fmt','bgr24','-threads','2',
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

def crop_zoom(warps, width, height):
    """Minimum fixed centered zoom whose inverse output corners stay in source pixels."""
    center=np.array([(width-1)/2,(height-1)/2])
    inverse=np.linalg.inv(warps)
    anchor=np.einsum('nij,j->ni',inverse[:,:2,:2],center)+inverse[:,:2,2]
    low=np.array([1.,1.]);high=np.array([width-2.,height-2.])
    if np.any(anchor<=low) or np.any(anchor>=high): return float('inf')
    corners=np.array([[0,0],[width-1,0],[width-1,height-1],[0,height-1]])
    offsets=np.einsum('nij,kj->nki',inverse[:,:2,:2],corners-center)
    room=np.where(offsets>=0,high-anchor[:,None,:],anchor[:,None,:]-low)
    return max(1.,float(np.max(np.abs(offsets)/room)))*(1+1e-7)


def plan_crop(poses, desired, size, dimensions, options, cancelled=lambda:False):
    width,height=dimensions
    coordinates=np.diag([width/size[0],height/size[1],1.])
    inv_coordinates=np.linalg.inv(coordinates)
    relative=np.array([matrix(t)@np.linalg.inv(matrix(p)) for p,t in zip(poses,desired)])
    angles=np.arctan2(relative[:,1,0],relative[:,0,0])
    def transforms(strength):
        result=np.repeat(np.eye(3)[None],len(relative),axis=0)
        c=np.cos(angles*strength);s=np.sin(angles*strength)
        result[:,0,0]=c;result[:,1,1]=c;result[:,0,1]=-s;result[:,1,0]=s
        result[:,:2,2]=relative[:,:2,2]*strength
        return coordinates@result@inv_coordinates
    warps=transforms(1.)
    requested=options['zoom'];strength=1.
    if not options.get('auto_crop',True): return warps,requested,strength
    limit=max(requested,options.get('max_auto_zoom',2.))
    needed=crop_zoom(warps,width,height)
    if needed>limit:
        low,high=0.,1.
        if crop_zoom(transforms(0.),width,height)>limit:
            raise ValueError('自动裁切上限太低，请略微提高上限以留出插值安全边距')
        for _ in range(28):
            if cancelled(): raise InterruptedError('任务已取消')
            mid=(low+high)/2
            if crop_zoom(transforms(mid),width,height)<=limit: low=mid
            else: high=mid
        strength=low;warps=transforms(strength);needed=crop_zoom(warps,width,height)
    return warps,max(requested,needed),strength


def validate_subject(frame, roi):
    h,w=frame.shape[:2]
    x,y,rw,rh=roi
    box=tuple(int(v) for v in (x*w,y*h,rw*w,rh*h))
    if min(box[2:])<8: raise ValueError('主体框过小，请扩大选框并覆盖主体纹理')
    gray=cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY)
    mask=np.zeros_like(gray);bx,by,bw,bh=box;mask[by:by+bh,bx:bx+bw]=255
    features=cv2.goodFeaturesToTrack(gray,150,.01,5,mask=mask)
    count=0 if features is None else len(features)
    if count<6: raise ValueError('所选区域纹理不足，无法可靠跟踪；请框住主体细节或换一帧重新选择')
    return box,count


def subject_step(previous, current, box):
    """Subpixel displacement, rejecting background and inconsistent optical flow."""
    x,y,w,h=map(int,box); mask=np.zeros_like(previous)
    x=max(0,x);y=max(0,y);mask[y:y+h,x:x+w]=255
    points=cv2.goodFeaturesToTrack(previous,150,.01,5,mask=mask)
    if points is None or len(points)<6: return None
    nxt,status,_=cv2.calcOpticalFlowPyrLK(previous,current,points,None)
    if nxt is None: return None
    back,reverse,_=cv2.calcOpticalFlowPyrLK(current,previous,nxt,None)
    if back is None: return None
    valid=(status.ravel()==1)&(reverse.ravel()==1)&(np.linalg.norm((back-points).reshape(-1,2),axis=1)<.8)
    delta=(nxt-points).reshape(-1,2)[valid]
    if len(delta)<6:return None
    median=np.median(delta,axis=0)
    good=delta[np.linalg.norm(delta-median,axis=1)<1.5]
    return np.median(good,axis=0) if len(good)>=6 else None

def analysis_frames(source, meta, size, cancelled, start, count, reference, subject):
    def forward(first,n):
        reader=SourceFrames(source,meta,size,cancelled,start+first/meta['fps'],n)
        try:
            for i in range(first,first+n):
                ok,frame=reader.read()
                if not ok: break
                yield i,frame
        finally:reader.release()
    if not subject:
        for i,frame in forward(0,count):yield i,frame,False
        return
    for i,frame in forward(reference,count-reference):yield i,frame,i==reference
    if reference:
        # Decode short chunks so reverse tracking never buffers a whole 4K clip.
        end=reference+1;first_chunk=True
        while end>0:
            begin=max(0,end-48)
            chunk=list(forward(begin,end-begin))
            for i,frame in reversed(chunk):
                yield i,frame,first_chunk and i==reference
            first_chunk=False;end=begin

def render_frame(frame, transform, output_size):
    """Cubic subpixel warp + area reduction to limit moving high-frequency aliasing."""
    h,w=frame.shape[:2]
    stabilized=cv2.warpAffine(frame,transform[:2],(w,h),flags=cv2.INTER_CUBIC,
                              borderMode=cv2.BORDER_CONSTANT)
    if output_size==(w,h):return stabilized
    interpolation=cv2.INTER_AREA if output_size[0]<=w and output_size[1]<=h else cv2.INTER_CUBIC
    return cv2.resize(stabilized,output_size,interpolation=interpolation)


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
    clip_start=round(options.get('start',0)*fps)/fps
    clip_end=options.get('end')
    duration=meta['frames']/fps
    clip_end=min(duration,clip_end) if clip_end is not None else duration
    count=int(round((clip_end-clip_start)*fps))
    if count<1: raise ValueError('剪辑范围至少需要一帧')
    reference=round((options.get('reference_time',clip_start)-clip_start)*fps)
    if mode=='subject' and not 0<=reference<count: raise ValueError('主体选择帧必须位于剪辑范围内')
    cap=analysis_frames(source,meta,size,cancelled,clip_start,count,reference,mode=='subject')
    poses, cumulative, tracker, start = [None]*count, np.eye(3), None, None
    processed=0
    weak_tracking=0;weak_total=0
    previous = None
    try:
        for frame_index,frame,reset in cap:
            check()
            if reset: tracker=None;previous=None;weak_tracking=0
            small = cv2.resize(frame, size)
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            if mode == 'subject':
                if tracker is None:
                    box,_=validate_subject(small,options['roi'])
                    tracker = cv2.TrackerCSRT_create()
                    if tracker.init(small,box) is False: raise ValueError('主体跟踪初始化失败，请重新框选')
                    start = np.array([box[0]+box[2]/2,box[1]+box[3]/2])
                    refined=start.copy()
                else:
                    previous_box=box
                    found, box = tracker.update(small)
                    if not found: raise ValueError(f'第 {frame_index+1} 帧主体跟踪丢失，请改选主体或缩短片段')
                center = np.array([box[0]+box[2]/2,box[1]+box[3]/2])
                if previous is not None:
                    delta=subject_step(previous,gray,previous_box)
                    weak_tracking=weak_tracking+1 if delta is None else 0
                    weak_total+=int(delta is None)
                    if weak_tracking>=5: raise ValueError(f'第 {frame_index+1} 帧主体运动特征连续不可靠，已停止导出；请重新框选清晰主体或缩短片段')
                    predicted=refined+delta if delta is not None else refined
                    # Retain measured shake cancellation; slowly correct tracker drift.
                    alpha=2/(options['smoothing']+2) if delta is not None else .35
                    refined=predicted+alpha*(center-predicted)
                poses[frame_index]=[*(refined-start),0.]
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
                poses[frame_index]=[cumulative[0,2],cumulative[1,2],np.arctan2(cumulative[1,0],cumulative[0,0])]
            previous=gray
            processed+=1
            if processed%20==0: report('分析运动',min(45,processed/max(1,count)*45))
    finally: cap.close()
    if any(p is None for p in poses): raise ValueError('剪辑范围超出可解码的视频帧')
    poses=np.asarray(poses); poses[:,2]=np.unwrap(poses[:,2])
    desired=np.zeros_like(poses) if mode in ('lock','subject') else smooth(poses,options['smoothing'])
    check()
    warps,zoom,strength=plan_crop(poses,desired,size,(w,h),options,cancelled)
    report(f'裁切 {zoom:.3f}×，保留防抖修正 {strength:.0%}',45)
    height=options['resolution']
    ow,oh=(w//2*2,h//2*2) if height==0 else (max(2,round(w/h*height/2)*2),height)
    codec=options['codec']; fmt=options['format']
    enc={'h264':['-c:v','libx264','-crf','18','-preset','fast'],
         'hevc':['-c:v','libx265','-crf','20','-preset','fast','-x265-params','pools=2'],
         'vp9':['-c:v','libvpx-vp9','-crf','32','-b:v','0']}[codec]
    temp=output.with_name('render.'+fmt)
    cmd=[FFMPEG,'-hide_banner','-loglevel','error','-y','-f','rawvideo','-pix_fmt','bgr24',
         '-s',f'{ow}x{oh}','-r',str(fps),'-i','pipe:0','-ss',str(clip_start),'-i',str(source),
         '-map','0:v:0','-map','1:a:0?',*enc,'-threads','2','-pix_fmt','yuv420p',
         '-c:a','libopus' if fmt=='webm' else 'aac','-af','apad','-t',f'{len(poses)/fps:.12g}']
    if fmt in ('mp4','mov'): cmd+=['-movflags','+faststart']
    cap=SourceFrames(source,meta,(w,h),cancelled,clip_start,count)
    center_x,center_y=(w-1)/2,(h-1)/2
    z=np.array([[zoom,0,(1-zoom)*center_x],[0,zoom,(1-zoom)*center_y],[0,0,1]])
    try:
        with open(output.parent/'encode.log','wb') as log:
            p=subprocess.Popen([*cmd,str(temp)],stdin=subprocess.PIPE,stderr=log)
            try:
                for i,warp in enumerate(warps):
                    check(); ok,frame=cap.read()
                    if not ok: raise ValueError('第二遍解码提前结束')
                    p.stdin.write(render_frame(frame,z@warp,(ow,oh)).tobytes())
                    if i%20==0: report('编码导出',45+54*i/len(poses))
                p.stdin.close()
                if p.wait()!=0: raise ValueError((output.parent/'encode.log').read_text(errors='replace')[-1500:])
            finally:
                if p.poll() is None: p.kill(); p.wait()
    finally: cap.release()
    check(); temp.replace(output)
    return dict(**meta,output_width=ow,output_height=oh,effective_zoom=zoom,stabilization_strength=strength,auto_crop=options.get('auto_crop',True),retained_area=1/zoom**2,weak_tracking_frames=weak_total,resampling='cubic-area')
