# 00 WSL2 开发环境搭建（Win11 + VSCode）

## 方案选择（先看这个）

| 方案 | 做法 | 评价 |
|---|---|---|
| **A（推荐）** | WSL2 里直接装 **Ubuntu 20.04** 发行版 | 手册原生基线，编译问题最少，一次跑通概率最高 |
| B | WSL2 装 Ubuntu 24.04 + Docker 里跑 20.04 编译环境 | 想留在 24.04 桌面体验时的折中，编译容器化最干净 |
| C | WSL2 Ubuntu 24.04 直接编 | 能编，但老 buildroot 在新 gcc 下有已知坑（见第 4 节），适合愿意边踩边修的人 |

SDK 编译不看"宿主机新旧"，看的是手册基线。正点原子全部文档和售后都以 20.04 为准，
选 A 最不折腾。VSCode 连接方式三个方案完全一样。

## 1. 安装 WSL2 + Ubuntu（方案 A）

PowerShell（管理员）：

```powershell
wsl --install -d Ubuntu-20.04      # 若列表里没有则先 wsl --update
wsl --list --online                # 查看可用发行版
wsl -l -v                          # 确认 VERSION 是 2
wsl --set-default-version 2
```

.wslconfig（放在 `C:\Users\Administrator\.wslconfig`，编译吃内存，默认减半内存不够）：

```ini
[wsl2]
memory=16GB
processors=8
swap=8GB
```

## 2. 磁盘位置：SDK 必须放 WSL 原生文件系统

```bash
# 对：放 home 下（ext4）
mkdir -p ~/rk3568

# 错：不要放 /mnt/c/...（NTFS 通过 9P 挂载，编译慢 5~10 倍，
#     而且大小写/权限/符号链接问题会让 buildroot 中途莫名失败）
```

Win11 侧要访问源码的话：资源管理器地址栏输 `\\wsl$\Ubuntu-20.04\home\<用户>\rk3568`，
或者 VSCode 直接 Remote-WSL 打开，体验最好。

## 3. VSCode 连接

1. Win11 装 VSCode + 扩展 "WSL"（ms-vscode-remote.remote-wsl）；
2. WSL 里 `cd ~/rk3568 && code .`，自动装 server 端；
3. 推荐再装：C/C++ 扩展、Device Tree 扩展（看 dts 有语法高亮）。

## 4. 若坚持用 Ubuntu 24.04 直编（方案 C 的坑位预案）

24.04 默认 gcc-13 / 新版 perl / python3.12，编 2021~2022 年代的 rockchip buildroot
时的已知高发问题类别：

| 症状 | 原因 | 对策 |
|---|---|---|
| host-m4 / host-flex / host-bison 等 host 包编译报错 | 新 gcc 把老代码的警告升级为错误 | 装 gcc-11/g++-11，`update-alternatives` 切换后再编 |
| repo/git 脚本报 python 异常 | 老脚本对 python3.12 不兼容 | `sudo apt install python-is-python3`，个别脚本手工修 |
| 手册里的 lib32stdc++6 等包装不上 | 24.04 的 t64 包名迁移 | 报缺啥搜啥，装对应 t64 版本 |
| 个别 perl 模块报错 | perl 5.38 语法收紧 | 报错信息贴给正点原子论坛，通常有现成补丁 |

原则：**报错先看是不是 host 侧工具链问题，别把 SDK 本身改坏**。
修不了的终极退路就是方案 B（docker 跑 20.04）。

方案 B 速记（24.04 里）：

```bash
sudo apt install docker.io
sudo docker run -it --name rk3568build -v ~/rk3568:/work ubuntu:20.04 bash
# 容器里按 README_开发流程.md 第 1 节装依赖，然后正常编译
```

## 5. 烧录与串口（Win11 侧做，不进 WSL）

- **烧录**：直接用 Win11 的 RKDevTool + RK3568 USB 驱动（开发板资料里有），
  OTG 线接底板 Type-C（USB4 立贴座），BOOT 滑动开关切 loader。
  比在 WSL 里用 usbipd-win 透传稳定得多，别折腾。
- **串口控制台**：Win11 侧用 Tabby/PuTTY 开 CH340/CH343 对应的 COM 口，115200。
- 非要 WSL 里烧录：装 usbipd-win，PowerShell 里
  `usbipd attach --wsl --busid <总线号>`，但 loader 每切一次模式要重新 attach，烦。

## 6. 网络

- WSL2 默认 NAT 模式，编译下载源码包没问题；
- 板子和 WSL 互传文件：走路由器 LAN，板子 ssh/samba（手册 31）都通；
- 板子直连 PC 网口调试时，WSL 侧访问不到板子（NAT 隔离），
  调试网络功能建议在 Win11 侧或同一路由器下进行。
