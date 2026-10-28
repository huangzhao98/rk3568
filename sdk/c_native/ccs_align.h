/*
 * ccs_align.h — 垂直卷动相位估计与软件消卷
 *
 * 背景（ccs_webcam.py 文件头注释）：rkcif 的 DMA 相位锁定在"开流后收到
 * 的第一个数据"，有信号时开流会从帧中间开始写 → 画面垂直卷动，且运行中
 * 永不自愈。硬件层面不存在确定性对齐，只能"测相位 + 软件消卷"：
 * 设卷动相位为 k，则 buffer 第 b 行 = 源画面第 (b+k)%h 行；把帧整体循环
 * 上移 k 行（ccs_align_deroll）即恢复正确画面。
 *
 * 本模块移植主服务的三个相位测量函数（ccs_dev/ccs_webcam.py:983~1070）：
 *   ccs_align_measure   ← _measure_roll：参考帧行亮度轮廓循环相关（通用）
 *   ccs_align_split_fix ← _split_fix  ：任务栏被帧边界切成上下两截的
 *                         签名检测（绝对锚点，不依赖参考帧）
 *   ccs_align_seam      ← _seam_roll  ：卷绕帧接缝检测（不依赖具体内容，
 *                         需连续 3 帧做时间稳定性验证）
 * 主服务的完整四级链还有任务栏结构检测（_taskbar_roll 等，依赖 Windows
 * 桌面特征），体量较大且属兜底手段，本 SDK 未移植（见 README 遗留问题）。
 *
 * 输入帧格式：UYVY（YUV 4:2:2 打包，w*h*2 字节），与 ccs_cap 输出一致；
 * 亮度 Y 在奇数字节。
 */
#ifndef CCS_ALIGN_H
#define CCS_ALIGN_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* 传给 ccs_align_nudge 的特值：翻半屏（对应主服务 nudge(99999)） */
#define CCS_ALIGN_FLIP_HALF 99999

/*
 * ccs_align_measure — 用已对齐参考帧测当前帧的卷动相位
 *
 * 算法（_measure_roll 的 C 移植）：
 *   1. 对 cur/ref 各算"行亮度轮廓"：每行亮度均值，隔 8 列采样
 *      （全列相关 1080p 太慢，轮廓形状对相位相关已足够）；
 *   2. 对全部候选相位 k（粗搜步长 2，再 ±2 细化）计算循环移位后
 *      两轮廓的平均绝对差，取最小者；
 *   3. 置信判定：最佳差 < 6 且显著优于全体候选的平均（< 0.5×均值）。
 *      平坦画面里错位匹配的误差也很小，光看阈值会误判，必须双重闸门。
 * 参数：
 *   cur  当前帧（UYVY，w*h*2 字节）
 *   ref  已对齐的参考帧（同分辨率；上次对齐置信时保存的帧）
 *   w,h  帧几何
 *   k    输出：估计的卷动相位（行数，0..h-1）
 * 返回：1 = 结果置信；0 = 不置信（此时 *k 仅为最佳猜测，不建议采用）。
 * 失败（内存不足/参数错误）返回 -1。
 */
int ccs_align_measure(const uint8_t *cur, const uint8_t *ref,
                      int w, int h, int *k);

/*
 * ccs_align_split_fix — "分截签名"检测（绝对锚点，无需参考帧）
 *
 * 场景：Windows 桌面 + 任务栏。卷动把任务栏切成上下两截：帧顶连续若干
 * 行与帧底连续一段都是任务栏纹理（行内亮度方差高、且明显亮过桌面），
 * 中间是大段平坦桌面。命中时正确相位 = h - 顶截行数（顶截恰好接在
 * 贴底截之后）。
 * 返回：>=0 相位 k；-1 未命中（非分截画面）。本检测是主服务对齐链里
 * 优先级最高的绝对锚点，误判率最低。
 */
int ccs_align_split_fix(const uint8_t *cur, int w, int h);

/*
 * ccs_align_seam — 卷绕帧"接缝"检测（通用自举，需连续 3 帧）
 *
 * 卷绕帧的接缝 = 源画面底行与顶行的拼接处，接缝两侧行均亮度跳变通常
 * 异常于普通内容。三重闸门（任一不过即放弃，宁可不判不可误判）：
 *   1) 强度：最大跳变 >= 15 且 >= 4×全体跳变中位数；
 *   2) 唯一性：最强跳变 >= 1.8×（±3 行邻域外的）次强跳变——内容里有
 *      多个可比强度水平边（窗口标题栏等）时宁可不判；
 *   3) 时间稳定性：连续 3 帧候选位置一致（±1 行）——内容边缘随画面
 *      漂移，接缝只随采样时钟亚行级慢漂。
 * 纯色/黑屏或顶底行本就同色的画面检不出接缝，但那种画面错位也看不见。
 * 参数：frames[3] 指向 3 帧 UYVY 数据（取最近 3 帧，时间顺序）。
 * 返回：>=0 相位 k；-1 未检出/不置信。
 */
int ccs_align_seam(const uint8_t *const frames[3], int w, int h);

/*
 * ccs_align_deroll — 软件消卷：把帧整体循环上移 k 行
 *
 * out = in 的后 k 行搬到前面（对应 Python raw[-k*st:] + raw[:-k*st]）。
 * in 与 out 不得重叠；k==0 时为整帧拷贝。out 至少 w*h*2 字节。
 */
void ccs_align_deroll(const uint8_t *in, uint8_t *out, int w, int h, int k);

/*
 * ccs_align_nudge — 相位微调（对应主服务 /nudge 接口）
 * 语义：roll+1 画面上移一行；传 CCS_ALIGN_FLIP_HALF（99999）= 翻半屏。
 * 返回新相位 (roll+delta) mod h，恒在 [0,h)。
 */
int ccs_align_nudge(int roll, int delta, int h);

#ifdef __cplusplus
}
#endif
#endif /* CCS_ALIGN_H */
