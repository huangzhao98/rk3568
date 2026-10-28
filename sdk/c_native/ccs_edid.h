/*
 * ccs_edid.h — 采集芯片（TC358743）EDID 读写
 *
 * 对应主服务 ccs_webcam.py 的 EDID 部分（get_edid_hex / set_edid_hex /
 * edid_ok）与 ccs_channel.sh 里的 v4l2-ctl --set-edid/--get-edid 调用。
 *
 * 硬件背景：EDID 存在 TC358743 片内 RAM（板子断电即丢，开机由
 * ccs_init.sh 重写）。源设备（显卡）读这份 EDID 决定输出什么分辨率。
 *
 * ioctl 路径：v4l2-ctl --get-edid/--set-edid 对 /dev/v4l-subdevX 发的是
 * VIDIOC_G_EDID / VIDIOC_S_EDID（板端 4.19 内核 v4l2-subdev.c 直接处理
 * 这两个请求号 40/41；新内核里它们的别名是 VIDIOC_SUBDEV_G_EDID /
 * VIDIOC_SUBDEV_S_EDID，号相同）。本模块用同一 ioctl 号与结构体布局，
 * 与 v4l2-ctl 完全等价。
 *
 * ⚠ 写 EDID 会让源端重新协商输出（画面中断几秒）。写之前请确认你拥有
 * 这台板子的采集链路——主服务运行期间乱写会破坏它的对齐状态。
 */
#ifndef CCS_EDID_H
#define CCS_EDID_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define CCS_EDID_BLOCK 128      /* EDID 单块 128 字节（CEA-861 基础块） */

/*
 * ccs_edid_read — 读芯片当前 EDID
 *
 * 功能：等价于 v4l2-ctl -d <subdev> --get-edid。先以 blocks=0 查询
 *       芯片里已写入的块数，再整块读出。对应 ccs_webcam.py get_edid_hex。
 * 参数：
 *   subdev      subdev 节点路径，如 "/dev/v4l-subdev2"
 *               （主服务用 media-ctl -p 按 "tc35874x" 实体名定位）
 *   buf         输出缓冲，至少 max_blocks*128 字节
 *   max_blocks  buf 能容纳的块数（128 字节/块；1=基础块，2=含一个扩展块）
 * 返回：>0 实际读出的字节数（128 的整数倍）；
 *       0  芯片从未写过 EDID（ENODATA）；-1 错误（errno）。
 * 副作用：只读操作，对芯片与采集无影响。
 */
int ccs_edid_read(const char *subdev, uint8_t *buf, int max_blocks);

/*
 * ccs_edid_write — 写 EDID 到芯片 RAM
 *
 * 功能：等价于 v4l2-ctl -d <subdev> --set-edid（加 fix 时等价于附带
 *       --fix-edid-checksums）。对应 ccs_webcam.py set_edid_hex：
 *       先写、再读回逐字节验证；fix=1 时先在内存里修正每个 128 字节
 *       块的校验和再写，验证时跳过每块最后一字节。
 * 参数：
 *   subdev  subdev 节点路径
 *   buf     EDID 数据，长度必须为 128 或 256 字节
 *   len     buf 长度
 *   fix     非 0 = 写入前自动修正校验和（复制到临时缓冲，不改调用方数据）
 * 返回：1 写入并读回验证成功；0 验证失败；-1 参数/系统错误（errno）。
 * 副作用：写芯片 EDID RAM，源端会立即重新协商输出；本函数不触发
 *         重对齐（那是主服务的职责，见 README）。
 */
int ccs_edid_write(const char *subdev, const uint8_t *buf, int len, int fix);

/*
 * ccs_edid_fix_checksum — 原地修正每个 128 字节块的校验和
 * 规则：每块 128 字节之和 mod 256 必须为 0，最后一字节 = (-sum(前127)) & 0xff
 * 对应 v4l2-ctl 的 --fix-edid-checksums 与 ccs_webcam.py build_edid
 * 末尾的 ed[127] = (-sum(ed[:127])) & 0xff。
 */
void ccs_edid_fix_checksum(uint8_t *buf, int len);

/*
 * ccs_edid_valid — EDID 有效性检查，对应 ccs_webcam.py edid_ok
 * 条件：长度 >= 128、负载非全零（排除对齐用的垃圾 EDID）、
 * 首块校验和正确。返回 1 有效 / 0 无效。
 */
int ccs_edid_valid(const uint8_t *buf, int len);

#ifdef __cplusplus
}
#endif
#endif /* CCS_EDID_H */
