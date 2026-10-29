# RK3568 采集卡项目 — AI 协作笔记

> 这个文件是给 AI 助手（Kimi Code / Copilot 等）和人类接棒者看的项目档案。
> AI 会话开始时请先读完本文件，再看 `50_内核定制修改存档.md` 和 `61_文件地图.md`。

## 项目是干什么的

RK3568 采集卡：TC358743 把 HDMI/VGA 输入转成 CSI，RK3568 采集后硬编 MJPEG，
通过网页（板上 8080 端口）实时预览，带 HID 键鼠回控（USB Gadget）。
核心程序是 `ccs_webcam.py`（部署到板上 `/usr/sbin/ccs_webcam.py`，
systemd 服务 `ccs_webcam.service`）。

## 工作环境与习惯（重要）

- 交流用中文，回复要通俗、能直接转给客户看的部分单独整理
- 开发机目录：`~/rk3568/`（repo 管理的 SDK），本文档仓库 = `~/rk3568/ccs_dev/`
- 测试板：`192.168.1.222`，SSH `root/root`，用 `sshpass -p root ssh -o StrictHostKeyChecking=no`
- 工作副本改完先部署到板上验证，**实测通过后才打包**；打包前把 staging
  (`~/rk3568/ubuntu/rootfs/usr/sbin/`) 和 rootfs 镜像一起更新
- 编译内核：`cd ~/rk3568 && ./build.sh kernel` → 产物 `kernel/boot.img`
- 打包固件：`./build.sh updateimg` → 产物 `rockdev/update.img`（~1.5GB）
- 打包后检查 `rockdev/boot.img`、`rockdev/rootfs.img` 软链指向没被重置
- 改动内核/服务前在板上留备份：`/root/xxx.bak_YYYYMMDD_说明`
- 每完成一项工作，追加记录到 `50_内核定制修改存档.md`，写清原因和验证结果
- 板上 `/tmp` 下的临时测试脚本重启会丢，重要的放 ccs_dev

## 已完成的核心工作（截至 2026-10-28）

1. **开机画面垂直卷动根治**（详见存档 §18 及追加）：
   - 根因：HDMI 源一直在跑时采集卡开机，TC358743 在画面随机位置建帧，
     产生固定垂直偏移；偏移只在锁定时产生，之后恒定，丢帧不影响
   - 方案：测不出相位时自动"干净重锁"（`_hpd_clean_relock`：HPD 拉低→
     源停输出→3s 静默→拉高→等锁 8s），相位恒为 0（实测 9/9），
     等效快速拔插一次；内容识别（taskbar/band/seam）只做辅助，重锁后
     偏离 0 超 10 行判假阳性
   - 持久化：`/var/lib/ccs_roll`（w h k）+ `/var/lib/ccs_ref.raw` 参考帧；
     常态开机驱动 HPD 门控放行后 1~2s 采信锚点（via=gated，见 §23）
   - 网页 `/realign` 按钮走同一机制，手动触发
2. **网页 1s 可开**：首次对齐挪进采集线程，不再阻塞 HTTP 绑定
3. **网页状态遮罩**：断线提示"无视频输入信号"，对齐中显示"对齐中…"
   （JS 2s 轮询 /stat，aligning 优先于无信号；sig_ok 看门狗每拍更新）
4. **内核 tc35874x 驱动**（10 个 commit，见 kernel-patches/）：
   HUB 供电识别、相位固定、延迟解静音、i2c 挂死自救、
   TMDS 在而 SYNC 超 60s 自救复位、热拔插提速、漏边恢复
   （芯片先于驱动锁定时轮询比对补走 format_change，否则时序
   卡死 640x480，见存档 §21）、HPD 门控（probe 起按住源端，
   应用层 set_fmt/s_stream 才放行，冷启动相位恒 0，对齐 ~5s，
   见存档 §23）、relock_dirty 脏重锁标记（中途重锁相位摇骰，
   失锁期 TMDS 在=相位随机，用户态据此禁用钉值快进，见 §24）
5. **性能**：采集 60fps 满帧（实测），编码输出 ~45fps（RGA 缩放+硬编
   ~22ms/帧，信箱丢中间帧，不是采集问题）

## 坑（不要再踩）

- **PHY_RST 寄存器 0x8535 永远不要用**：实验发现会导致永久失锁，
  HPD 重锁都救不回，只能断电重启
- nudge（手动微调）是软件消卷，重启服务即失效，不能用来模拟硬件卷动；
  要造随机相位只能重启板子
- 服务重启会在驱动 1Hz tick 边界解静音，60.000Hz 源下相位不变
  （tick=整数帧）；只有冷启动相位才随机
- 板上 rkcif 驱动不支持 QUERY_DV_TIMINGS ioctl（返回 Inappropriate ioctl），
  测帧率用 `v4l2-ctl --stream-mmap --stream-count=N --stream-to=/dev/null`
- 内容检测在客户无状态栏/均匀画面上原理性失效（接缝行均亮度跳变
  仅 12.2 < 闸门 15；seam/taskbar 有假阳性实测 k=442/338）
- 服务独占 /dev/video0，测裸采集前 `systemctl stop ccs_webcam`，测完拉起
- **build.sh kernel 会静默吞掉 make 编译错误**（报 Error 后仍用旧
  Image 重打包并打印 succeeded）——部署内核前必须核对
  `arch/arm64/boot/Image` 时间戳或板上 `uname -v`
- 服务 stop 后 GST 管线拆除要几十秒，期间 video0 REQBUFS 返回
  EBUSY，属正常，等一会再测
- 客户现场不接受任何人工对齐步骤，不接受依赖画面内容的方案

## 换机器后怎么开始工作

1. `git clone git@github.com:huangzhao98/rk3568.git`（本仓库）
2. `git clone git@github.com:huangzhao98/rk3568-kernel.git`（内核完整源码）
3. 完整 SDK（几十个 GB，repo 管理）不在 GitHub 上，需要的话从原开发机
   拷贝或用 rockchip 官方渠道重新同步，然后把 kernel-patches/ 里 10 个
   补丁 `git am` 到 SDK 的 kernel 目录
4. 读 `README_开发流程.md`、`61_文件地图.md` 建立全局认识
