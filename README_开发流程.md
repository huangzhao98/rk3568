# RK3568-CCS 开发流程（一期：4.19 内核 + Ubuntu 22.04 rootfs）

> 对应手册：01《ATK-DLRK3568嵌入式Linux系统开发手册V1.3》第 4/6 章
> SDK 包：B 盘 → atk-rk3568_linux_release_v1.0_20230620.tgz（以网盘最新为准）
> 内核 4.19，U-Boot 2017.09。
> **一期路线：内核/U-Boot 沿用 SDK 不动，rootfs 换成 Ubuntu 22.04（要 apt 生态）；
> Buildroot 只在基线编译时编一次，用于提取 vendor 文件。
> 二期再评估升 6.1 内核（ubuntu-rockchip / RK 官方 6.1 SDK）。**

## 路线总览

| 阶段 | 内容 | 文档 |
|---|---|---|
| 1 | WSL2 + Ubuntu 20.04 编译环境 | 00_WSL环境搭建.md |
| 2 | SDK 基线编译（uboot/kernel/buildroot 全量，不裁剪） | 本文第 1~3 节 |
| 3 | 内核配置 + 设备树合入（10_rk3568-atk-ccs.dtsi） | 01_裁剪清单.md 第三节、02_IO与复用配置总表.md |
| 4 | **制作 Ubuntu 22.04 rootfs 并替换烧录** | **03_Ubuntu_rootfs制作.md** |
| 5 | 自启动（systemd） | ccs.service + ccs_init.sh |
| 6 | 驱动移植与功能验证 | 20_驱动移植要点.md、30_测试验证清单.md |

> 编译/修改/打包/烧录的逐条实操命令：**04_SDK修改编译打包详解.md**
> （含"改动→重编→烧录"速查表与验证生效手段）。

## 1. 主机环境（WSL2 + Ubuntu 20.04 + VSCode，详见 00_WSL环境搭建.md）

开发机定为 Win11 + WSL2 + VSCode Remote-WSL。**不要直接用 Ubuntu 24.04 编这套 SDK**，
手册基线是 20.04，24.04 的新 gcc/perl 编老 buildroot 有已知坑；
坚持 24.04 就用 Docker 跑 20.04 容器（00 文档方案 B）。SDK 放 WSL 原生 ext4（~/rk3568），
严禁放 /mnt/c。烧录和串口控制台在 Win11 侧做（RKDevTool + Tabby）。

```bash
sudo apt-get update
sudo apt-get install -y git repo make gcc g++ lsb-core lib32stdc++6 libncurses-dev \
     device-tree-compiler u-boot-tools qemu-user-static binfmt-support
# 阿里云源（手册 3.2 节推荐）；qemu-user-static 是 chroot 配 Ubuntu arm64 rootfs 用的
```

## 2. 安装 SDK

```bash
mkdir -p ~/rk3568 && cd ~/rk3568
tar -xzf atk-rk3568_linux_release_*.tgz
# repo 同步（按手册 4.1 节）
.repo/repo/repo sync -l
```

## 3. 首次全量编译（不裁剪，跑通基线 + 提取 vendor 文件）

```bash
cd ~/rk3568
./build.sh          # 全自动编译 uboot/kernel/buildroot/recovery
# 单独编译：
./build.sh uboot
./build.sh kernel
./build.sh buildroot
```

编完即得：rockdev 里的 uboot.img / boot.img（一期直接沿用），以及
buildroot/output/rockchip_rk3568/target/（vendor 文件来源，见 03 文档第 4 节）。

## 4. 关键路径速查

| 内容 | 路径 |
|---|---|
| 板级配置 | device/rockchip/rk356x/BoardConfig-rk3568-atk-evb1-ddr4-v10.mk |
| 内核 defconfig | kernel/arch/arm64/configs/rockchip_linux_defconfig |
| 内核设备树 | kernel/arch/arm64/boot/dts/rockchip/rk3568-atk-evb1-ddr4-v10-linux.dts |
| 本项目 dts 增量 | buildroot_dev/10_rk3568-atk-ccs.dtsi（include 进上面的 dts） |
| U-Boot defconfig | u-boot/configs/rk3568_defconfig |
| 分区表（rootfs 大小！） | device/rockchip/rk356x/parameter*.txt |
| vendor 文件来源 | buildroot/output/rockchip_rk3568/target/ |
| USB Gadget 官方文档 | SDK docs：Rockchip_Quick_Start_Linux_USB_Gadget_CN.pdf |

## 5. 内核配置与重编（Ubuntu 路线下不变）

```bash
cd kernel
make ARCH=arm64 rockchip_linux_defconfig
make ARCH=arm64 menuconfig        # 增量项见 01_裁剪清单.md 第三节
make ARCH=arm64 savedefconfig
cp defconfig arch/arm64/configs/rockchip_linux_defconfig
cd .. && ./build.sh kernel
```

设备树：把 buildroot_dev/10_rk3568-atk-ccs.dtsi 拷到
kernel/arch/arm64/boot/dts/rockchip/，在 rk3568-atk-evb1-ddr4-v10-linux.dts
末尾加 `#include "rk3568-atk-ccs.dtsi"`，重编 kernel 即进 boot.img。

## 6. rootfs：制作 Ubuntu 22.04（核心步骤，单独成文）

见 **03_Ubuntu_rootfs制作.md**：ubuntu-base 解包 → qemu chroot 装包 →
注入内核模块/mpp 等 vendor 文件 → 放大 rootfs 分区 → 打包替换 rockdev/rootfs.img →
`./build.sh updateimg` → RKDevTool 整片烧录 → 首启 resize2fs。

回退方法：rockdev/rootfs.img 链接指回 buildroot 的 rootfs.ext4 重打 update.img，
boot/uboot 不动，两套 rootfs 随时互换。

## 7. 自启动（systemd）

Ubuntu 用 systemd，不用 BusyBox init。安装一次即可：

```bash
sudo cp ccs_init.sh 40_hid_gadget.sh /usr/sbin/ && sudo chmod 755 /usr/sbin/ccs_init.sh /usr/sbin/40_hid_gadget.sh
sudo cp ccs.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now ccs.service
journalctl -u ccs.service -b      # 看启动日志
```

（buildroot 路线存档：S99ccs，仅备查。）
