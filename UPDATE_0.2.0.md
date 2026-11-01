# 0.2.0 预览更新

仅更新 app、tests、README.md、STATUS.md 和本说明，不覆盖 NAS 的 Dockerfile、compose.yaml、.env、data。保留用户换源、源码权限修复和局域网端口配置。

将 steadyframe-preview-update-0.2.0.tar.gz 上传到已有程序目录，不需要手工解压。在 NAS 终端执行：

```sh
cd /volume1/docker/SteadyFrame-NAS &&
test -f steadyframe-preview-update-0.2.0.tar.gz &&
tar -czf "app-backup-$(date +%Y%m%d-%H%M%S).tar.gz" app &&
sudo docker compose stop steadyframe &&
tar -xzf steadyframe-preview-update-0.2.0.tar.gz &&
sudo docker compose --progress plain build &&
sudo docker compose up -d
```

暂停服务会中断现有任务。构建复用已装依赖缓存，不使用 --no-cache。等待约30秒，执行：

```sh
sudo docker compose ps
sudo docker compose exec -T steadyframe python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8786/api/health').read().decode())"
```

应显示 healthy 和版本 0.2.0。继续访问已有局域网地址，Ctrl+F5 强制刷新。重新选择视频新建任务，旧任务不自动重跑。首帧就绪即可选主体/提交，后台720p播放预览可看进度；选择“原片分辨率”保留4K尺寸，输出不使用低清预览。高质量有损编码，HDR/10-bit仍未支持。

回退：停止应用后，用实际备份文件名 `tar -xzf app-backup-时间.tar.gz` 恢复 app，再 build/up。原视频与 data 不在更新包中。
