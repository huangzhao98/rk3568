# 03 Ubuntu 22.04 rootfs 制作流程（一期主系统）

> 一期策略：**内核/U-Boot 沿用正点原子 SDK（4.19 vendor，全部硬件驱动不动），
> 只把根文件系统从 Buildroot 换成 Ubuntu 22.04 (jammy) arm64**。
> 动机：要 apt 包管理和完整的 Ubuntu 生态。APT 是 rootfs 层面的事，
> 与内核版本无关——这是改动最小、硬件风险为零的路线。
> 二期再评估升 6.1 内核（见方案文档）。
>
> 参考先例：正点原子 RK3568 + Ubuntu 24.04 全过程记录（CSDN，
> blog.csdn.net/charlie114514191/article/details/157992677）；
> 正点原子 SDK 自带 Debian rootfs 支持（同为 4.19 内核换 rootfs 的官方佐证）。
> 选 22.04 而非 24.04：systemd 249 / glibc 2.35 对 4.19 老内核最宽容。

## 0. 总体流程

```
SDK 基线编译（uboot+kernel 必须） ──► 准备 ubuntu-base 22.04 arm64
        │                                   │
        ▼                                   ▼
  提取 vendor 文件（模块/固件/mpp）      qemu chroot 装包配置
        │                                   │
        └────────► 合并 ◄───────────────────┘
                      │
                      ▼
            打包 rootfs.img（注意分区大小）──► 烧录 ──► 首启验证
```

预计耗时：首次 1~2 天（含排坑），复刻半小时。

## 1. 前置：SDK 基线编译

按 README_开发流程.md 第 1~3 节完成 SDK 全量编译一次（buildroot 也编）。
目的有三：验证环境、拿到 boot.img/uboot.img、拿到 buildroot target 目录
里的 vendor 文件（第 3 步用）。**不裁剪 buildroot**，编完即用。

## 2. 准备 ubuntu-base 并 chroot

```bash
cd ~/rk3568
mkdir -p ubuntu && cd ubuntu
wget https://cdimage.ubuntu.com/ubuntu-base/releases/22.04/release/ubuntu-base-22.04.5-base-arm64.tar.gz
mkdir rootfs && sudo tar -xzf ubuntu-base-22.04.5-base-arm64.tar.gz -C rootfs

# chroot 三件套：qemu 模拟、DNS、binfmt
sudo apt install -y qemu-user-static binfmt-support
sudo cp /usr/bin/qemu-aarch64-static rootfs/usr/bin/
sudo cp /etc/resolv.conf rootfs/etc/resolv.conf    # 不拷则 chroot 里 apt 无法解析域名

sudo chroot rootfs /usr/bin/qemu-aarch64-static /bin/bash
# 以下都在 chroot 里
```

## 3. chroot 内配置（装包 + 系统设置）

```bash
# --- apt 源：arm64 必须走 ports 源，换国内镜像 ---
cat > /etc/apt/sources.list <<'EOF'
deb http://mirrors.ustc.edu.cn/ubuntu-ports jammy main restricted universe multiverse
deb http://mirrors.ustc.edu.cn/ubuntu-ports jammy-updates main restricted universe multiverse
deb http://mirrors.ustc.edu.cn/ubuntu-ports jammy-security main restricted universe multiverse
EOF
apt update && apt upgrade -y

# --- 基础系统包 ---
DEBIAN_FRONTEND=noninteractive apt install -y \
  sudo openssh-server kmod udev iproute2 net-tools iputils-ping \
  vim less wget curl ca-certificates gnupg \
  locales tzdata dialog whiptail \
  ifupdown wpasupplicant \
  parted e2fsprogs usbutils pciutils \
  bash-completion command-not-found

# --- 本项目调试用包（对应原 buildroot 保留清单）---
DEBIAN_FRONTEND=noninteractive apt install -y \
  v4l-utils media-ctl i2c-tools gpiod evtest alsa-utils \
  iperf3 tcpdump ethtool netcat-openbsd \
  device-tree-compiler u-boot-tools htop rsync

# gstreamer/ffmpeg 二期做推流时再装；libmpp 不从 apt 装（见第 4 节说明）

# --- GStreamer 采集管线（ccs_webcam.py GstBE 后端用，2026-09 加）---
# gst 采集走 Python GI 绑定；gir1.2-gst-plugins-base-1.0 必须装
# （缺了它 GstApp namespace 不存在，appsink 的 try_pull_sample 用不了）
DEBIAN_FRONTEND=noninteractive apt install -y \
  gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0
# plugins-bad（h264parse 验证码流等）+ strace 也已随镜像固化
# （2026-09-30 起，见 50_内核定制修改存档.md 固化节）；filesrc 直喂裸
# Annex-B 给解码器会按 4KB 块切 AU，解出来全是垂直拖影的假象：
DEBIAN_FRONTEND=noninteractive apt install -y gstreamer1.0-plugins-bad strace

# --- mpph264enc 硬编插件（/h264 H264 流支路用，2026-09 加）---
# Ubuntu apt 没有；不从源码编，第 4 节直接从 buildroot target 拷已编好的
# libgstrockchipmpp.so（gst 1.20 ABI 兼容，见第 4 节第 2 步）

# --- 语言/时区/主机名 ---
locale-gen zh_CN.UTF-8 en_US.UTF-8
ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime
echo rk3568-ccs > /etc/hostname
cat > /etc/hosts <<'EOF'
127.0.0.1   localhost
127.0.1.1   rk3568-ccs
EOF

# --- 用户与 ssh（调试期从简，量产再收紧）---
echo 'root:123456' | chpasswd
sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config

# --- 串口控制台：RK 4.19 vendor 内核的调试串口设备是 ttyFIQ0，不是 ttyS2！---
systemctl enable serial-getty@ttyFIQ0.service
systemctl enable ssh

# --- 双网口 DHCP（systemd-networkd，不用 netplan 更直接）---
systemctl enable systemd-networkd
cat > /etc/systemd/network/20-wired.network <<'EOF'
[Match]
Name=eth*

[Network]
DHCP=yes
EOF
ln -sf /run/systemd/resolve/resolv.conf /etc/resolv.conf   # 出 chroot 前恢复成 stub

# --- 最小 fstab（root= 由内核 cmdline 指定；oem/userdata 分区不挂）---
cat > /etc/fstab <<'EOF'
# <file system>         <mount>  <type>  <options>        <dump> <pass>
tmpfs                   /tmp     tmpfs   defaults,nosuid  0      0
EOF

exit    # 退出 chroot
```

## 4. 注入 vendor 文件（硬件功能的关键）

Ubuntu 用户态要能调 RK 的硬件，需要三类文件。**来源：SDK 编出的
buildroot target 目录**（`~/rk3568/buildroot/output/rockchip_rk3568/target`，
若该路径不存在，挂载正点原子出厂 debian 镜像提取亦可，东西一样）：

```bash
T=~/rk3568/buildroot/output/rockchip_rk3568/target
R=~/rk3568/ubuntu/rootfs

# 1) 内核模块（版本目录名必须和 boot.img 内核一致，如 4.19.232）
sudo cp -a $T/lib/modules/.  $R/lib/modules/
sudo cp -a $T/lib/firmware/. $R/lib/firmware/    # WiFi/BT 等固件，有备无患

# 2) 视频编解码用户态库（推流编码用；一期先把文件备好）
sudo cp -a $T/usr/lib/librockchip_mpp* $R/usr/lib/aarch64-linux-gnu/ 2>/dev/null
sudo cp -a $T/usr/lib/librga*          $R/usr/lib/aarch64-linux-gnu/ 2>/dev/null
# mpp 的 GStreamer 插件（mpph264enc 等，GstBE /h264 支路用；gst 1.20 ABI 兼容）
sudo mkdir -p $R/usr/lib/aarch64-linux-gnu/gstreamer-1.0
sudo cp -a $T/usr/lib/gstreamer-1.0/libgstrockchipmpp.so $R/usr/lib/aarch64-linux-gnu/gstreamer-1.0/
# mali GPU 库本项目用不到（无本地显示、编码走 VPU 不走 GPU），不拷

# 3) 依赖刷新
sudo chroot $R /usr/bin/qemu-aarch64-static /sbin/ldconfig
```

注意：**libmpp 版本必须与 4.19 内核的 mpp_service 驱动匹配，所以只从 SDK 拷，
不要装社区 PPA 里给 5.10/6.1 编的版本。**

## 5. 打包 rootfs.img（分区大小是坑）

ubuntu-base 装完上述包约 1.2~1.5GB，**比 buildroot 大得多**，先检查并放大
SDK 分区表里的 rootfs 分区：

```bash
grep -i rootfs device/rockchip/rk356x/parameter*.txt
# 若 rootfs 分区 < 4GB，改成 0x00C00000(6GB) 或结尾写 grow
# （改 parameter.txt 后必须整片重新烧录，不能只烧 rootfs）
```

制作镜像并替换：

```bash
cd ~/rk3568/ubuntu
dd if=/dev/zero of=rootfs-ubuntu.img bs=1M count=3072      # 先给 3GB
mkfs.ext4 -L rootfs rootfs-ubuntu.img
mkdir mnt && sudo mount rootfs-ubuntu.img mnt
sudo cp -a rootfs/. mnt/
sudo umount mnt
e2fsck -f rootfs-ubuntu.img && resize2fs -M rootfs-ubuntu.img

# 替换 SDK 的 rootfs 链接并打包
cd ~/rk3568/rockdev
ln -sf ~/rk3568/ubuntu/rootfs-ubuntu.img rootfs.img
cd ~/rk3568 && ./build.sh updateimg
# 烧录 rockdev/update.img（Win11 RKDevTool，流程同手册）
# 首启后执行 sudo resize2fs /dev/mmcblk0p6 占满分区（分区号以 parameter.txt 为准）
```

## 6. 首启验证清单

| 项 | 命令/判据 |
|---|---|
| 串口控制台 | Tabby 115200 能看到 systemd 启动日志，出现 login 提示（ttyFIQ0） |
| apt | `apt update` 能拉源（通网前提下）——本次换系统的核心目的 |
| ssh | `ssh root@<板子IP>` 通 |
| 双网口 | `ip a` 见 eth0/eth1，iperf3 打流 |
| 视频采集 | `v4l2-ctl --list-devices` 见 rkcif 节点（dts 已按 10_rk3568-atk-ccs.dtsi 合入） |
| HID gadget | `sh /usr/sbin/ccs_init.sh start` 后 /dev/hidg0/1 出现（见 ccs.service） |
| GPIO 线名 | `gpioinfo` 能看到 dtsi 里配的中文对照线名 |
| RTC | `hwclock -r` 正常读 PCF8563 |

## 7. 已知坑位备忘

1. **ttyFIQ0**：忘了 enable serial-getty@ttyFIQ0，串口就只在内核日志阶段有输出、
   没有 login——这是最经典的一条。
2. chroot 里 apt 报域名解析失败 = 没拷 resolv.conf（第 2 节三件套）。
3. `Failed to load module ...` 一类 systemd 警告若出现，多为 4.19 缺新 syscall，
   只要 journal 无 FAILED 关键服务可忽略；22.04 的 systemd 249 实测对 4.19 友好。
4. rootfs 分区没放大就烧录 = 开机挂载失败或写到一半满。先改 parameter.txt。
5. 若起不来要回退：rockdev 里把 rootfs.img 链接指回 buildroot 的 rootfs.ext4，
   重打 update.img 即可，boot/uboot 不用动——两套 rootfs 可随时互换。
6. Ubuntu 24.04 想尝鲜亦可（有正点原子板子成功案例），但 systemd 255 对 4.19
   的兼容性请自行实测，一期不建议。
