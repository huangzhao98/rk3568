// i2c_sniff.c — GPIO 轮询 I2C 嗅探内核模块（RK3568 4.19）
// IRQ 版实测有约 5% 回读竞态（2.9us 短脉冲下 IRQ 延迟导致读回旧值），
// 无法可靠解码，故改为轮询：直接读 GPIO1 EXT_PORT 寄存器（0xFE740070，
// bit0=SDA=A0, bit1=SCL=A1），一次读取两线电平，~100-200ns 采样周期，
// 采到的电平是确定值，无需任何猜测。采集期间关本地中断，同步阻塞。
// 用法：
//   insmod i2c_sniff.ko
//   echo 300 > /proc/i2c_sniff_poll     # 触发采集 300ms（10~2000）
//   cat /proc/i2c_sniff_raw > cap.txt   # 读回
// 行格式: <ns时间戳> 2 <电平>   （line 固定 2，电平 bit0=SDA bit1=SCL）
#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/gpio.h>
#include <linux/proc_fs.h>
#include <linux/seq_file.h>
#include <linux/ktime.h>
#include <linux/slab.h>
#include <linux/spinlock.h>
#include <linux/io.h>
#include <linux/uaccess.h>
#include <linux/preempt.h>

static int sda_gpio = 32;   /* GPIO1_A0 */
static int scl_gpio = 33;   /* GPIO1_A1 */
module_param(sda_gpio, int, 0444);
module_param(scl_gpio, int, 0444);

#define GPIO1_BASE      0xFE740000
#define GPIO_EXT_PORT_V2 0x70

#define RING_SIZE (1 << 18)          /* 262144 条 */
struct rec {
	u64 ts;
	u8 line;         /* 2 = 轮询记录（val 含两线电平） */
	u8 val;          /* bit0=SDA bit1=SCL */
};
static struct rec *ring;
static u32 head, tail;
static DEFINE_SPINLOCK(lock);
static u64 dropped;
static void __iomem *gpio1_reg;
static DEFINE_MUTEX(cap_mutex);

static inline void ring_push(u64 ts, u8 line, u8 val)
{
	u32 next = (head + 1) & (RING_SIZE - 1);

	if (next == tail)
		dropped++;
	else {
		ring[head].ts = ts;
		ring[head].line = line;
		ring[head].val = val;
		head = next;
	}
}

static long poll_capture(unsigned long ms)
{
	u64 dur, start, now;
	u32 last, v;
	u32 iter = 0;
	long n = 0;
	unsigned long flags;

	if (ms < 10)
		ms = 10;
	if (ms > 8000)
		ms = 8000;
	dur = (u64)ms * 1000000ull;

	spin_lock_irqsave(&lock, flags);
	head = tail = 0;
	dropped = 0;
	spin_unlock_irqrestore(&lock, flags);

	preempt_disable();
	local_irq_disable();

	start = ktime_get_ns();
	last = readl(gpio1_reg + GPIO_EXT_PORT_V2) & 0x3;
	ring_push(start, 2, (u8)last);

	while (1) {
		v = readl(gpio1_reg + GPIO_EXT_PORT_V2) & 0x3;
		if (v != last) {
			now = ktime_get_ns();
			ring_push(now, 2, (u8)v);
			last = v;
			n++;
		}
		if ((++iter & 0xFF) == 0) {
			if (ktime_get_ns() - start >= dur)
				break;
		}
	}

	local_irq_enable();
	preempt_enable();
	return n;
}

static ssize_t poll_write(struct file *f, const char __user *ubuf,
			  size_t len, loff_t *off)
{
	char buf[24];
	unsigned long ms = 300;
	long cnt;
	u64 used;

	if (len >= sizeof(buf))
		return -EINVAL;
	if (copy_from_user(buf, ubuf, len))
		return -EFAULT;
	buf[len] = 0;
	if (kstrtoul(buf, 10, &ms))
		ms = 300;

	mutex_lock(&cap_mutex);
	cnt = poll_capture(ms);
	used = (head - tail) & (RING_SIZE - 1);
	mutex_unlock(&cap_mutex);

	pr_info("i2c_sniff: poll %lums done, edges=%ld ring_used=%llu dropped=%llu\n",
		ms, cnt, used, dropped);
	return len;
}

static const struct file_operations poll_fops = {
	.owner = THIS_MODULE,
	.write = poll_write,
};

static int sniff_show(struct seq_file *m, void *v)
{
	unsigned long flags;
	int n = 0;

	/* seq_get_buf/seq_commit：写满当前缓冲即停且不再弹环，
	 * 避免 4.19 seq_file 溢出重调期间丢批；缓冲由 single_open_size
	 * 预分配 256KB，单次 read 可吐 ~1 万条，8s 窗口秒级排空。 */
	for (n = 0; n < 12000; n++) {
		struct rec r;
		char *p;
		size_t sz;
		int len;

		sz = seq_get_buf(m, &p);
		if (sz < 24)
			break;
		spin_lock_irqsave(&lock, flags);
		if (tail == head) {
			spin_unlock_irqrestore(&lock, flags);
			break;
		}
		r = ring[tail];
		tail = (tail + 1) & (RING_SIZE - 1);
		spin_unlock_irqrestore(&lock, flags);
		len = scnprintf(p, 24, "%llu %u %u\n", r.ts, r.line, r.val);
		seq_commit(m, len);
	}
	if (n == 0)
		seq_printf(m, "# empty dropped=%llu\n", dropped);
	return 0;
}

static int sniff_open(struct inode *ino, struct file *f)
{
	return single_open_size(f, sniff_show, NULL, 256 * 1024);
}

static const struct file_operations sniff_fops = {
	.owner = THIS_MODULE,
	.open = sniff_open,
	.read = seq_read,
	.llseek = seq_lseek,
	.release = single_release,
};

/* ---------------- 主控模式（bit-bang，开漏模拟） ----------------
 * 用于 MS8005F 已从总线断开（跳线切到 RK3568 侧）时主动读写 MS9332。
 * 协议（嗅探实测定）: 从机地址 0x59；寄存器地址 2 字节，线上顺序 [b1 b2]
 * （b1 像偏移、 b2 像页）；读 = WR[b1 b2] + Sr + RD n 字节；
 * 写 = WR[b1 b2 data...]。
 * /proc/i2c_sniff_io 写:  "r <b1> <b2> <n>" 或 "w <b1> <b2> <d0> <d1>..."
 * （全 hex）; 然后 read 同一节点拿回 "xx xx ..." 或 "ERR ..."。
 */
#include <linux/delay.h>

#define BB_DELAY 6  /* us, ~80kHz，对齐 MS8005F 实测速率 */

static void bb_sda(int out_low)  /* 1=拉低 0=释放 */
{
	if (out_low)
		gpio_direction_output(sda_gpio, 0);
	else
		gpio_direction_input(sda_gpio);
}

static void bb_scl(int out_low)
{
	if (out_low)
		gpio_direction_output(scl_gpio, 0);
	else
		gpio_direction_input(scl_gpio);
}

static int bb_wait_scl_high(void)  /* 时钟伸展等待, 超时返回 -1 */
{
	int i;

	for (i = 0; i < 200; i++) {
		if (gpio_get_value(scl_gpio))
			return 0;
		udelay(5);
	}
	return -1;
}

static void bb_start(void)
{
	bb_sda(0); bb_scl(0); udelay(BB_DELAY);
	bb_sda(1); udelay(BB_DELAY);
	bb_scl(1); udelay(BB_DELAY);
}

static void bb_stop(void)
{
	bb_sda(1); udelay(BB_DELAY);
	bb_scl(0);
	bb_wait_scl_high();
	udelay(BB_DELAY);
	bb_sda(0); udelay(BB_DELAY);
}

static int bb_write_byte(u8 b)  /* 返回 0=ACK 1=NACK -1=伸展超时 */
{
	int i, ack;

	for (i = 7; i >= 0; i--) {
		bb_sda(!((b >> i) & 1));
		udelay(BB_DELAY);
		bb_scl(0);
		if (bb_wait_scl_high()) return -1;
		udelay(BB_DELAY);
		bb_scl(1);
		udelay(BB_DELAY);
	}
	bb_sda(0);  /* 释放，从机拉低=ACK */
	udelay(BB_DELAY);
	bb_scl(0);
	if (bb_wait_scl_high()) return -1;
	udelay(BB_DELAY);
	ack = gpio_get_value(sda_gpio);
	bb_scl(1);
	udelay(BB_DELAY);
	return ack;
}

static u8 bb_read_byte(int last, int *err)  /* last=1 时回 NACK */
{
	int i;
	u8 b = 0;

	bb_sda(0);  /* 释放，由从机驱动 */
	for (i = 7; i >= 0; i--) {
		udelay(BB_DELAY);
		bb_scl(0);
		if (bb_wait_scl_high()) { *err = 1; return 0; }
		udelay(BB_DELAY);
		if (gpio_get_value(sda_gpio))
			b |= 1 << i;
		bb_scl(1);
	}
	udelay(BB_DELAY);
	bb_sda(!last);  /* 非最后字节: 拉低=ACK */
	udelay(BB_DELAY);
	bb_scl(0);
	if (bb_wait_scl_high()) { *err = 1; return b; }
	udelay(BB_DELAY);
	bb_scl(1);
	udelay(BB_DELAY);
	bb_sda(0);  /* 释放 */
	return b;
}

static u8 io_result[512];
static int io_result_len;
static char io_err[32];

static int bb_wait_idle(void)  /* 等总线稳定空闲25ms（MS8005F轮询间隙~98ms） */
{
	u64 t0 = ktime_get_ns();
	u64 last_bad = t0;

	while (ktime_get_ns() - t0 < 1000000000ull) {
		if (!gpio_get_value(sda_gpio) || !gpio_get_value(scl_gpio))
			last_bad = ktime_get_ns();
		if (ktime_get_ns() - last_bad > 25000000ull)
			return 0;
		udelay(50);
	}
	snprintf(io_err, sizeof(io_err), "ERR bus busy");
	return -1;
}

static int ms9332_read(u8 b1, u8 b2, u8 *out, int n)
{
	int i, ack, err = 0;

	if (bb_wait_idle())
		return -1;
	local_irq_disable();
	bb_start();
	ack = bb_write_byte(0x59 << 1);
	if (ack) { bb_stop(); local_irq_enable(); snprintf(io_err, sizeof(io_err), "ERR addr-wr nack"); return -1; }
	if (bb_write_byte(b1)) { bb_stop(); local_irq_enable(); snprintf(io_err, sizeof(io_err), "ERR b1 nack"); return -1; }
	if (bb_write_byte(b2)) { bb_stop(); local_irq_enable(); snprintf(io_err, sizeof(io_err), "ERR b2 nack"); return -1; }
	bb_start();  /* repeated start */
	ack = bb_write_byte((0x59 << 1) | 1);
	if (ack) { bb_stop(); local_irq_enable(); snprintf(io_err, sizeof(io_err), "ERR addr-rd nack"); return -1; }
	for (i = 0; i < n; i++)
		out[i] = bb_read_byte(i == n - 1, &err);
	bb_stop();
	local_irq_enable();
	if (err) { snprintf(io_err, sizeof(io_err), "ERR stretch timeout"); return -1; }
	return 0;
}

static int ms9332_write(u8 b1, u8 b2, const u8 *data, int n)
{
	int i;

	if (bb_wait_idle())
		return -1;
	local_irq_disable();
	bb_start();
	if (bb_write_byte(0x59 << 1)) { bb_stop(); local_irq_enable(); snprintf(io_err, sizeof(io_err), "ERR addr nack"); return -1; }
	if (bb_write_byte(b1)) { bb_stop(); local_irq_enable(); snprintf(io_err, sizeof(io_err), "ERR b1 nack"); return -1; }
	if (bb_write_byte(b2)) { bb_stop(); local_irq_enable(); snprintf(io_err, sizeof(io_err), "ERR b2 nack"); return -1; }
	for (i = 0; i < n; i++) {
		if (bb_write_byte(data[i])) {
			bb_stop();
			local_irq_enable();
			snprintf(io_err, sizeof(io_err), "ERR data%d nack", i);
			return -1;
		}
	}
	bb_stop();
	local_irq_enable();
	return 0;
}

static ssize_t io_write(struct file *f, const char __user *ubuf,
			size_t len, loff_t *off)
{
	char buf[128];
	char cmd;
	unsigned int b1, b2, n = 1, i;
	int ret = 0;

	if (len >= sizeof(buf))
		return -EINVAL;
	if (copy_from_user(buf, ubuf, len))
		return -EFAULT;
	buf[len] = 0;

	mutex_lock(&cap_mutex);
	io_result_len = 0;
	io_err[0] = 0;
	if (sscanf(buf, "%c %x %x %u", &cmd, &b1, &b2, &n) < 3) {
		snprintf(io_err, sizeof(io_err), "ERR fmt");
		goto out;
	}
	if (cmd == 'r') {
		if (n > sizeof(io_result)) n = sizeof(io_result);
		if (n < 1) n = 1;
		ret = ms9332_read(b1, b2, io_result, n);
		if (!ret) io_result_len = n;
	} else if (cmd == 'w') {
		/* 解析后续 hex 数据字节 */
		char *p = buf, *end = buf + len;
		u8 data[256];
		int dn = 0, v;

		if (sscanf(p, "%c %x %x", &cmd, &b1, &b2) != 3) {
			snprintf(io_err, sizeof(io_err), "ERR fmt"); goto out;
		}
		/* 跳过前三项 */
		for (i = 0; i < 3 && p < end; i++) {
			while (p < end && *p && *p != ' ') p++;
			while (p < end && *p == ' ') p++;
		}
		while (p < end && *p && dn < (int)sizeof(data)) {
			if (sscanf(p, "%x", &v) != 1) break;
			data[dn++] = v;
			while (p < end && *p && *p != ' ') p++;
			while (p < end && *p == ' ') p++;
		}
		if (!dn) { snprintf(io_err, sizeof(io_err), "ERR nodata"); goto out; }
		ret = ms9332_write(b1, b2, data, dn);
	} else {
		snprintf(io_err, sizeof(io_err), "ERR cmd");
	}
out:
	mutex_unlock(&cap_mutex);
	return len;
}

static int io_show(struct seq_file *m, void *v)
{
	int i;

	if (io_err[0])
		seq_printf(m, "%s\n", io_err);
	else {
		for (i = 0; i < io_result_len; i++)
			seq_printf(m, "%s%02x", i ? " " : "", io_result[i]);
		seq_putc(m, '\n');
	}
	return 0;
}

static int io_open(struct inode *ino, struct file *f)
{
	return single_open(f, io_show, NULL);
}

static const struct file_operations io_fops = {
	.owner = THIS_MODULE,
	.open = io_open,
	.read = seq_read,
	.write = io_write,
	.llseek = seq_lseek,
	.release = single_release,
};

static int __init sniff_init(void)
{
	int ret;

	ring = kzalloc(sizeof(struct rec) * RING_SIZE, GFP_KERNEL);
	if (!ring)
		return -ENOMEM;

	gpio1_reg = ioremap(GPIO1_BASE, 0x100);
	if (!gpio1_reg) {
		ret = -ENOMEM;
		goto err_free;
	}

	ret = gpio_request(sda_gpio, "sniff_sda");
	if (ret) goto err_unmap;
	ret = gpio_request(scl_gpio, "sniff_scl");
	if (ret) goto err_sda;
	gpio_direction_input(sda_gpio);
	gpio_direction_input(scl_gpio);

	proc_create("i2c_sniff_raw", 0444, NULL, &sniff_fops);
	proc_create("i2c_sniff_poll", 0222, NULL, &poll_fops);
	proc_create("i2c_sniff_io", 0622, NULL, &io_fops);
	pr_info("i2c_sniff: poll+master mode, sda=gpio%d scl=gpio%d ready\n",
		sda_gpio, scl_gpio);
	return 0;

err_sda:
	gpio_free(sda_gpio);
err_unmap:
	iounmap(gpio1_reg);
err_free:
	kfree(ring);
	return ret;
}

static void __exit sniff_exit(void)
{
	remove_proc_entry("i2c_sniff_io", NULL);
	remove_proc_entry("i2c_sniff_poll", NULL);
	remove_proc_entry("i2c_sniff_raw", NULL);
	gpio_free(scl_gpio);
	gpio_free(sda_gpio);
	iounmap(gpio1_reg);
	kfree(ring);
	pr_info("i2c_sniff: exit, dropped=%llu\n", dropped);
}

module_init(sniff_init);
module_exit(sniff_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("GPIO polling I2C sniffer (deterministic both-line sampler)");
