# 04 Linux SDK 使用、修改与编译打包详解（正点原子 ATK-DLRK3568）

> 配套手册：《ATK-DLRK3568嵌入式Linux系统开发手册V1.3》第 4~6 章。
> 本文是"照着做"级别的实操版，覆盖：SDK 结构 → 编译机制 → 改内核/改设备树/
> 换 rootfs → 打包 update.img → 烧录 → 验证生效。
> 环境前提：WSL2 + Ubuntu 20.04（见 00 文档），SDK 已解压在 `~/rk3568`。

## 1. SDK 目录结构（只记用得着的）

```
~/rk3568/
├── build.sh                 # 全自动编译入口（核心脚本）
├── mkfirmware.sh            # 打包镜像到 rockdev（build.sh firmware 就是调它）
├── Makefile -> buildroot    # 顶层 Makefile 是指向 buildroot 的软链
├── device/rockchip/rk356x/  # 板级配置（BoardConfig、parameter 分区表、overlay）
├── kernel/                  # 内核 4.19 源码
├── u-boot/                  # U-Boot 2017.09 源码
├── buildroot/               # buildroot 源码（一期只编一次，提取 vendor 文件）
├── rkbin/                   # RK 闭源 blob（DDR 初始化、miniloader、BL31、OP-TEE）
├── tools/                   # mkimage、boot_merger、resource_tool 等打包工具
├── external/                # RK 自家库源码（mpp、rga、rkaiq 等）
├── docs/                    # RK 官方文档（USB Gadget、DDR、时钟等指南在这）
├── debian/  yocto/          # 另外两种 rootfs（一期不用）
└── rockdev/                 # 【最终产物集散地】全是软链接，烧录前在这看货
```

板级配置文件：`device/rockchip/rk356x/BoardConfig-rk3568-atk-evb1-ddr4-v10.mk`，
它决定了内核用哪个 dts、rootfs 用哪种、分区表用哪份。一般不用改。

## 2. 编译机制：build.sh 做了什么

`./build.sh` 全自动按顺序做四件事，了解顺序才能知道改东西后该重编哪段：

| 顺序 | 动作 | 产物 |
|---|---|---|
| 1 | 编 U-Boot（含 rkbin blob 合并） | `u-boot/uboot.img` |
| 2 | 编内核+dts+打包 resource.img | `kernel/boot.img` |
| 3 | 编 buildroot rootfs（同时把内核模块 .ko 收集进 rootfs） | `buildroot/output/rockchip_rk3568/images/rootfs.ext4` |
| 4 | 编 recovery | recovery.img |

之后两条打包命令：

```bash
./build.sh firmware    # = ./mkfirmware.sh：把上面产物软链接到 rockdev/
./build.sh updateimg   # 把 rockdev/ 里所有镜像打成 rockdev/update.img（整包固件）
```

**rockdev/ 是软链接目录**——理解了它就理解了一切替换技巧：
`rockdev/rootfs.img` 指向 buildroot 的 ext4，我们把它改指到 Ubuntu 的 img，
`./build.sh updateimg` 打出来的就是"原厂内核 + Ubuntu rootfs"的固件。

## 3. 首次基线编译（什么都不改）

```bash
cd ~/rk3568
./build.sh              # 全量，约 1~3 小时（WSL 16G/8 核）
./build.sh firmware     # 链接到 rockdev
./build.sh updateimg    # 打出 update.img
ls -l rockdev/          # 确认 update.img 生成
```

烧录跑通（见第 8 节）再开始改。**基线镜像留一份备份**，后面改坏了能回退。

常用单独编译：

```bash
./build.sh uboot        # 只编 U-Boot
./build.sh kernel       # 只编内核+dts（改 dts 后跑这个）
./build.sh buildroot    # 只编 buildroot rootfs
./build.sh cleanall     # 全清（uboot/kernel/buildroot），别随手用
```

## 4. 修改内核配置

```bash
cd ~/rk3568/kernel
make ARCH=arm64 rockchip_linux_defconfig   # 先载入基线配置
make ARCH=arm64 menuconfig                 # 改配置（本项目增量项见 01 文档第三节）
make ARCH=arm64 savedefconfig              # 导出
cp defconfig arch/arm64/configs/rockchip_linux_defconfig   # 存回 SDK
cd .. && ./build.sh kernel                 # 重编，产物进 boot.img
```

注意：直接 `make menuconfig` 改的 `.config` 会在下次 `./build.sh kernel` 时被
SDK 脚本用 defconfig 覆盖，**必须 savedefconfig 存回 defconfig 文件**才算数。

## 5. 修改设备树（本项目的主战场）

原厂 dts：`kernel/arch/arm64/boot/dts/rockchip/rk3568-atk-evb1-ddr4-v10-linux.dts`。
**不直接改原厂文件**，用增量叠加：

```bash
# 1) 把本项目 dtsi 拷进内核 dts 目录
cp buildroot_dev/10_rk3568-atk-ccs.dtsi \
   ~/rk3568/kernel/arch/arm64/boot/dts/rockchip/rk3568-atk-ccs.dtsi

# 2) 在原厂 dts 末尾加一行（用编辑器打开追加）
echo '#include "rk3568-atk-ccs.dtsi"' >> \
   ~/rk3568/kernel/arch/arm64/boot/dts/rockchip/rk3568-atk-evb1-ddr4-v10-linux.dts

# 3) 重编内核
cd ~/rk3568 && ./build.sh kernel
```

排查 dts 语法错误：编译报错时看输出里的 dtc 错误行号，行号对应的是
**展开后**的内容，可用预处理定位：

```bash
cd kernel
make ARCH=arm64 dtbs 2>&1 | less        # 看完整报错
# 或手工调 dtc 单编一个 dts 看行号
```

## 6. rootfs：Ubuntu 22.04（一期主线）

完整流程见 **03_Ubuntu_rootfs制作.md**，这里只列命令骨架：

```bash
# ubuntu-base 解包 → qemu chroot 装包 → 拷 vendor 文件 → 做 ext4 img
# 替换链接并重打包：
cd ~/rk3568/rockdev
ln -sf ~/rk3568/ubuntu/rootfs-ubuntu.img rootfs.img
cd ~/rk3568 && ./build.sh updateimg
```

回退：`ln -sf` 指回 buildroot 的 rootfs.ext4，重打 update.img 即可。

（buildroot 路线已存档：要编的话 `cd buildroot && source build/envsetup.sh
rockchip_rk3568 && make menuconfig && make savedefconfig && make`，
或 SDK 根目录 `./build.sh buildroot`。）

## 7. 内核模块与 Ubuntu rootfs 的同步（必知的坑）

内核模块（.ko）存在 rootfs 的 `/lib/modules/<内核版本>/` 里，且模块加载时
校验内核版本号。**只要改了内核配置或 dts 重编了内核，Ubuntu rootfs 里的
模块就必须同步更新**，否则 insmod 报 version magic 不匹配。

```bash
# 重编内核后，让 buildroot 重新收集一次模块（它会自动从 kernel 输出拷）
cd ~/rk3568 && ./build.sh buildroot

# 再同步到 Ubuntu rootfs（假设 ubuntu rootfs 挂载在 mnt/）
sudo rsync -a buildroot/output/rockchip_rk3568/target/lib/modules/ \
              ~/rk3568/ubuntu/rootfs/lib/modules/
# 重打 rootfs-ubuntu.img 或直接在线 scp 到板子 /lib/modules/ 也行
```

内置进内核（=y）的驱动不受影响，只有 =m 的模块要管。本项目关键驱动
（tc358743、yt8531c、pcf8563、configfs-hid）在 01 文档清单里都配的是 y，
就是为了少操这份心。

## 8. 烧录（Win11 侧，RKDevTool）

工具：开发板资料里的 RKDevTool（+ DriverAssitant 的 USB 驱动）。

**进烧录模式**（底板 OTG 口 = USB3 12P Type-C）：
- **Loader 模式**：板上已有可启动系统时用——按住 UPDATE 键上电/复位；
  或系统里 `reboot loader`。
- **Maskrom 模式**：系统全挂/首次烧录用——更底层，一般操作：断电，
  按住 UPDATE（RECOVERY）键不放再上电，RKDevTool 显示"发现一个
  MASKROM 设备"。

**两种烧法**：
- **整包**：烧 `update.img`（"升级固件"页签 → 选择固件 → 升级）。
  改了分区表（parameter.txt）后必须整包烧。
- **分区烧**：在"下载镜像"页签勾选单个分区烧单个 img——
  改了内核/dts 只勾 `boot`（boot.img）；改了 rootfs 只勾 `rootfs`。
  日常调 dts 就是"改 → build.sh kernel → 拷 boot.img 到 Win11 → 勾 boot 烧"，
  一次两分钟。

Win11 与 WSL 传文件：资源管理器打开 `\\wsl$\Ubuntu-20.04\home\<用户>\rk3568\rockdev`
直接拖。

## 9. 改动 → 重编 → 烧录 速查表

| 改了什么 | 重编命令 | 烧哪个分区 |
|---|---|---|
| dts/dtsi | `./build.sh kernel` | boot |
| 内核配置（=y 项） | `./build.sh kernel` | boot |
| 内核配置（含 =m 模块） | `./build.sh kernel` + `buildroot` + 同步模块（第 7 节） | boot + rootfs |
| Ubuntu rootfs 内容 | 重做/更新 rootfs-ubuntu.img + `./build.sh updateimg` | rootfs |
| parameter.txt 分区表 | `./build.sh updateimg` | 整包 update.img |
| U-Boot（一般用不到） | `./build.sh uboot` | uboot |
| 自启动/脚本 | 直接 scp 进板子或重打 rootfs | rootfs 或在线改 |

## 10. 验证修改是否生效

```bash
# dts 生效了吗：看内核日志里我们节点的 probe 记录
dmesg | grep -iE 'tc358743|pcf8563|yt8531|mipi'

# 看运行中设备树（最权威，改没改一目了然）
ls /proc/device-tree/                     # 节点
cat /proc/device-tree/leds/working/label  # 属性
dtc -I fs /proc/device-tree 2>/dev/null | grep -A5 tc358743   # Ubuntu 装了 dtc 可反编

# GPIO 线名生效了吗（dtsi 里 line-names 的效果）
gpioinfo | head -40

# I2C 设备出现了吗
i2cdetect -y 3    # MS9288C/MS9332 总线
i2cdetect -y 4    # TC358743（当前选中的那路，应见 0x0F）
i2cdetect -y 5    # PCF8563，应见 0x51
```

## 11. 常见问题速查

| 症状 | 大概率原因 |
|---|---|
| `./build.sh kernel` 后配置没生效 | 只改了 .config 没 savedefconfig 存回 defconfig（第 4 节） |
| dts 编译报行号对不上 | 报错行号是展开后的，先 `make dtbs` 看完整输出再定位 |
| 烧录后黑屏/无串口输出 | 分区表动了但没整包烧；或 boot.img 烧错分区 |
| insmod 报 Invalid module format | 内核重编过但 rootfs 模块没同步（第 7 节） |
| Ubuntu 起来没 login | serial-getty@ttyFIQ0 没 enable（03 文档第 3 节） |
| 串口有日志但登录不进 | Ubuntu rootfs 里没设密码/没建用户 |
| RKDevTool 找不到设备 | Win11 侧 USB 驱动（DriverAssitant）没装；线插错口（OTG 是 USB3 Type-C） |
| 编译中途莫名失败 | SDK 放到了 /mnt/c（00 文档第 2 节），挪回 ~/rk3568 |
