# CCS 板端原生 C SDK — Makefile
#
# 默认交叉编译（SDK prebuilts 里的 Linaro 6.3.1 aarch64 工具链，
# 即内核编译用的那套）：
#     make
# 本机/板端原生编译：
#     make CC=gcc
# 换其他交叉工具链：
#     make CC=aarch64-linux-gnu-gcc        # 若已在 PATH
#
# 产物：
#   libccs_native.a      静态库（ccs_cap + ccs_edid + ccs_align）
#   examples/demo_capture / demo_edid / demo_align

CROSS := /home/huangzhao/rk3568/prebuilts/gcc/linux-x86/aarch64/gcc-linaro-6.3.1-2017.05-x86_64_aarch64-linux-gnu/bin/aarch64-linux-gnu-gcc

# make 内置默认 CC=cc，origin=default 时才换成交叉工具链；
# 命令行 CC=gcc 传入时 origin=command line，不覆盖
ifeq ($(origin CC),default)
CC := $(CROSS)
endif

AR := $(shell command -v $(patsubst %gcc,%ar,$(notdir $(CC))) 2>/dev/null || \
        echo $(dir $(CROSS))$(patsubst %gcc,%ar,$(notdir $(CROSS))))

CFLAGS  ?= -O2
CFLAGS  += -std=c99 -Wall -Wextra
# -std=c99 严格模式下 glibc 隐藏 POSIX 结构体（timeval/timespec/fd_set），
# 而内核 videodev2.h 与 select/mmap 接口需要它们 → 显式打开默认特性集
CPPFLAGS += -I. -D_DEFAULT_SOURCE

LIB     := libccs_native.a
OBJS    := ccs_cap.o ccs_edid.o ccs_align.o
DEMOS   := examples/demo_capture examples/demo_edid examples/demo_align

.PHONY: all examples clean
all: $(LIB) examples

$(LIB): $(OBJS)
	$(AR) rcs $@ $^

%.o: %.c ccs_cap.h ccs_edid.h ccs_align.h
	$(CC) $(CPPFLAGS) $(CFLAGS) -c -o $@ $<

examples: $(DEMOS)

examples/%: examples/%.c $(LIB)
	$(CC) $(CPPFLAGS) $(CFLAGS) -o $@ $< -L. -lccs_native

clean:
	rm -f $(OBJS) $(LIB) $(DEMOS)
