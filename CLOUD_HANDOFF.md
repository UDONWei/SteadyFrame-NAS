# 云端接手

仓库：https://github.com/UDONWei/SteadyFrame-NAS ，默认分支 main。
先阅读 AGENTS.md、README.md、STATUS.md，再核对提交与工作树。只在本仓库工作，不修改 Codex 或 PlumiVault 仓库。

安装：Python 3.12；系统 apt 安装 ffmpeg、libglib2.0-0；pip install -r requirements.txt；pip install pytest==8.3.5 httpx==0.28.1。
验证：python -m pytest -q。使用合成视频，不请求上传私人视频。

下一步优先在 Linux 验证 Docker build、Compose 配置和容器只读媒体边界；核验音画同步、旋转/VFR、竖屏、跟踪丢失、任务取消及磁盘空间不足。补充验证证据后更新 STATUS。任何未运行检查须明确标注。云端负责代码开发，NAS 仍是实际部署与处理位置。
