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

## 已完成的核心工作（截至 2026-10-30）

1. **开机画面垂直卷动根治**（详见存档 §18 及追加，最终机制 §27）：
   - 根因（§27 实锤）：卷动全部来自 **rkcif DMA 侧相位**（芯片 TX 帧
     结构由延迟解静音保证对齐，偏移恒 0）。**首流/静音后/脏重锁后布防
     早于 TX 出数 → 锁到帧中间，相位随机**；**TX 已流式时重启采集管线
     → rkcif 按 FS 帧头重锁，相位恒 0**（4/4 实测 corr=1.000）
   - 方案：**零图像识别**（§25）。对齐只认确定性锚点：HPD 门控放行→
     首内容帧证明 TX 流式→**再 `_config_stream` 一次按帧头重锚钉 0**
     （§27，全程 ~2s，reboot×3 全 shift=0）；phase_suspect 脏重锁走
     clean_start 快锚 ~5s；HPD 干净重锁（`_hpd_clean_relock` 三态
     返回：1 成功 / 0 源不理会 / 2 仍 dirty 可重试≤3 次）兜底
   - 持久化：`/var/lib/ccs_roll`（w h k）+ `/var/lib/ccs_ref.raw` 参考帧
   - 网页 `/realign` 按钮走同一机制，手动触发
   - 已知未加固点：采集循环 kp==0 的 fast resume（拔插自愈）只有一次
     _config_stream，历史实测拔插均能修好；若报拔插后仍卷，加固=
     首帧后二次重启管线
2. **网页 1s 可开**：首次对齐挪进采集线程，不再阻塞 HTTP 绑定
3. **网页状态遮罩**：断线提示"无视频输入信号"，对齐中显示"对齐中…"
   （JS 2s 轮询 /stat，aligning 优先于无信号；sig_ok 看门狗每拍更新）
4. **内核 tc35874x 驱动**（11 个 commit，见 kernel-patches/）：
   HUB 供电识别、相位固定、延迟解静音、i2c 挂死自救、
   TMDS 在而 SYNC 超 60s 自救复位、热拔插提速、漏边恢复
   （芯片先于驱动锁定时轮询比对补走 format_change，否则时序
   卡死 640x480，见存档 §21）、HPD 门控（probe 起按住源端，
   应用层 set_fmt/s_stream 才放行，冷启动相位恒 0，对齐 ~5s，
   见存档 §23）、relock_dirty 脏重锁标记（中途重锁相位摇骰，
   失锁期 TMDS 在=相位随机，用户态据此禁用钉值快进，见 §24）、
   probe 时 TMDS 在跑标 dirty + work_gatewatch 门控期 TMDS 监视
   转干净 + work_unmute 失锁期"任一采样到 TMDS 消失即转干净"
   （内核 #23，§27；这些标记现只影响快进和 watchdog 判定）
5. **彩色细线保真：4:2:2 全链路**（§26）：细彩线变灰的根因是采集
   （gst NV12）和编码（vepu 输出恒 420，mpp HAL 写死）两处纵向色度
   砍半。现为 GstBE 采 NV16 + libjpeg-turbo 软编 4:2:2 shim
   （ljpeg422_shim.c，双槽流水 102fps，接口同 mpp shim，优先于 vepu）；
   PIL 回退 subsampling=1。编码器选择顺序 sw422→hw→pil
6. **性能**：采集 60fps 满帧（实测），422 软编输出 ~60fps（双槽流水），
   信箱丢中间帧，不是采集问题
7. **MS9332 EDID 自定义**（§28/§28b）：电脑看到的显示器名字/分辨率由板上
   MS9332 分配器的 EDID RAM（0x0d00 起 256B）决定，单片机 MS8005F
   上电写默认值。已破解其 I2C 协议（0x59，16 位地址[lo,hi]），RK3568
   用 i2c_sniff.ko（GPIO1_A0/A1 bit-bang，源码 ccs_dev/i2c_sniff.c）
   与单片机共享总线写 EDID；窗口开关 0x000c bit7（开=I2C 可写但 DDC
   停供，关=DDC 正常，写完必须关窗）。部署：
   `/usr/sbin/ccs_ms9332_edid.py`（写入+`--watch` 看门狗+NACK重试+
   复核防误判）、`ccs_edid.service`、`/etc/ccs_edid.bin`（出厂=
   720p60首选+1080p60可选双DTD，名字CCS-HDMI）；
   **写完自动 TC358743 HPD 脉冲（0x8544 拉低2s）经 MS9332 传导让电脑
   免拔线重读**；守卫：TMDS无且服务未起（开机门控期）才不脉冲。
   冷启动端到端验证通过；4-lane 硬件下 1080p60 裸采集 ~56-60fps。

## 坑（不要再踩）

- **PHY_RST 寄存器 0x8535 永远不要用**：实验发现会导致永久失锁，
  HPD 重锁都救不回，只能断电重启
- **rkcif DMA 相位规律（§27 实锤，旧认知全作废）**：首流/静音后/脏重锁
  后布防早于 TX 出数 → 相位随机（实测卷 78~590 行）；TX 已流式时重启
  采集管线 → 按 FS 帧头重锁，**相位恒 0**。想消卷就"信号流式后再
  _config_stream 一次"；别再用"服务重启相位不变/只能重启板子造随机
  相位"的旧模型推理
- nudge（手动微调）是软件消卷，重启服务即失效，不能用来模拟硬件卷动
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
- 客户现场不接受任何人工对齐步骤，不接受依赖画面内容的方案；
  **静态画面的检测假阳性是稳定的**，"两帧一致"闸门过滤不掉，
  自动路径禁止任何图像识别（§25）
- vepu 硬编 JPEG 输出恒为 4:2:0（mpp HAL 写死，不可配）——彩色细线
  保色必须走 ljpeg422 软编 shim；别再试图从 vepu 侧修
- 板上 libjpeg.so.8 是 jpeg8 ABI；交叉编译用 buildroot sysroot 头需把
  jconfig.h 的 JPEG_LIB_VERSION 改成 80，链接用板上拉回的
  libjpeg.so.8.2.2
- **内核 i2c3 必须保持 disabled**（§28）：GPIO1_A0/A1 已被 i2c_sniff.ko
  以 bit-bang 占用访问 MS9332，改回 okay 会与 bit-bang 冲突
- **MS8005F 单片机不能离开 I2C 总线**：断开后 MS9332 上行口几分钟内
  彻底关 HPD，电脑认不到屏
- 改 EDID 后 Windows 按 厂商+产品码+序列号 缓存，必须换序列号才能看到
- **挂死复位禁止 unbind/bind**（§28c 实捕）：旧 ccs_chip_reset.sh 的
  unbind/bind 会让 rkcif pipeline subdevs[] 缓存野指针，streamoff
  OOPS 在持 stream_lock 上下文 → 锁泄漏 → open() 永久 D 态只能重启
  整机。硬复位一律走 `echo 1 > /sys/bus/i2c/devices/4-000f/self_reset`
- **对齐卡死有 align_supervisor 兜底**（170s 自杀自愈）；再遇到先
  `kill -USR1 $(pgrep -f "ccs_webcam.py --port")` 让 faulthandler 喷
  全线程栈到 journal 取证，别直接重启
- **"信号在但无帧"≠芯片死**（§28d 实捕）：gst 管线会静默卡死（芯片
  锁定、rkcif 中断照跑、appsink 永远 None、总线无 ERROR），复位芯片
  无用且有害。看门狗已分流：qd>2s 才复位芯片，否则重建管线/重启服务
  变化；让电脑重读不用再物理拔线——写 TC358743 HPD_CTL(0x8544) 拉低 2s
  再拉高即可（经 MS9332 传导，§28b），但**开机门控期（ccs_webcam 未
  active）绝不可碰 HPD**
- MS9332 的 0x000c bit7 窗口开着时电脑读不到 EDID——任何 EDID 操作
  结局必须关窗（写 0x00），脚本已保证，手工调寄存器时注意
- bit-bang 访问 MS9332 会偶发与 MS8005F 轮询撞车（NACK）——事务级
  重试 5×50ms 兜底；看门狗抽查不匹配必须复核再重写，否则撞车污染的
  读值会造成假"被冲"连环重写（§28b 事故）

## 换机器后怎么开始工作

1. `git clone git@github.com:huangzhao98/rk3568.git`（本仓库）
2. `git clone git@github.com:huangzhao98/rk3568-kernel.git`（内核完整源码）
3. 完整 SDK（几十个 GB，repo 管理）不在 GitHub 上，需要的话从原开发机
   拷贝或用 rockchip 官方渠道重新同步，然后把 kernel-patches/ 里 11 个
   补丁 `git am` 到 SDK 的 kernel 目录
4. 读 `README_开发流程.md`、`61_文件地图.md` 建立全局认识
