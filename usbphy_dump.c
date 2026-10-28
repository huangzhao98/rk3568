/* usbphy_dump.c — 读取 RK3568 USB2 PHY1 (fe8b0000) GRF 寄存器，
 * 解码 OTG 口（fd800000/fd840000 所在，即 SL2.2s HUB 上行口）的
 * PHY 原始 linestate，用于判断 D+/D- 哪根线被上拉。
 *
 * 寄存器布局见 kernel/drivers/phy/rockchip/phy-rockchip-inno-usb2.c
 * rk3568_phy_cfgs[1]（reg=0xfe8b0000，GRF=usb2phy1_grf 0xfdca8000）：
 *   0x00 [8:0]  otg口 phy_sus：0x1d2=工作中 0x1d1=挂起
 *   0xc0 [5:4]  otg口 utmi LineState：bit4=LineState[0]=D+，bit5=LineState[1]=D-
 *   0xc0 [7]    otg口 utmi host disconnect
 *   0xc0 [17:16] host口（fd880000 对）LineState：[16]=D+ [17]=D-
 *   0xc0 [19]   host口 host disconnect
 * 用法: usbphy_dump   （板端 root 运行，需 /dev/mem）
 */
#include <stdio.h>
#include <stdint.h>
#include <fcntl.h>
#include <sys/mman.h>
#include <unistd.h>

#define GRF1_BASE 0xfdca8000UL
#define MAP_SIZE  0x1000

static const char *ls_desc(unsigned ls)
{
    switch (ls & 3) {
    case 0: return "SE0 (D+=0 D-=0)：无设备/复位中";
    case 1: return "D+=1 D-=0：全速/高速设备上拉（FS/HS 正常特征）";
    case 2: return "D+=0 D-=1：低速设备上拉（LS 特征；高速 HUB 出现此值=异常）";
    default: return "SE1 (D+=1 D-=1)：非法态，D+/D- 可能短接";
    }
}

int main(void)
{
    int fd = open("/dev/mem", O_RDWR | O_SYNC);
    if (fd < 0) { perror("open /dev/mem"); return 1; }

    volatile uint32_t *grf = mmap(NULL, MAP_SIZE, PROT_READ | PROT_WRITE,
                                  MAP_SHARED, fd, GRF1_BASE);
    if (grf == MAP_FAILED) { perror("mmap"); return 1; }

    uint32_t r00 = grf[0x00 / 4];
    uint32_t r80 = grf[0x80 / 4];
    uint32_t r84 = grf[0x84 / 4];
    uint32_t rc0 = grf[0xc0 / 4];

    printf("PHY1 GRF @0x%lx\n", GRF1_BASE);
    printf("  reg00 = 0x%08x   otg口 phy_sus[8:0]=0x%03x -> %s\n",
           r00, r00 & 0x1ff,
           (r00 & 0x1ff) == 0x1d2 ? "工作中(powered on)" :
           (r00 & 0x1ff) == 0x1d1 ? "挂起(suspended)" : "未知");
    printf("  reg80 = 0x%08x   (linestate det enable)\n", r80);
    printf("  reg84 = 0x%08x   (linestate det status)\n", r84);
    printf("  regc0 = 0x%08x   (utmi 状态)\n", rc0);

    unsigned otg_ls = (rc0 >> 4) & 3;
    printf("\n[OTG口 = fd800000/fd840000 = SL2.2s HUB 上行]\n");
    printf("  linestate = %u -> %s\n", otg_ls, ls_desc(otg_ls));
    printf("  host_disconnect(bit7) = %u\n", (rc0 >> 7) & 1);

    unsigned host_ls = (rc0 >> 16) & 3;
    printf("\n[HOST口 = fd880000/fd8c0000 = TMUX121/PICO 方向]\n");
    printf("  linestate = %u -> %s\n", host_ls, ls_desc(host_ls));
    printf("  host_disconnect(bit19) = %u\n", (rc0 >> 19) & 1);

    munmap((void *)grf, MAP_SIZE);
    close(fd);
    return 0;
}
