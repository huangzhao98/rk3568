# Makefile — CCS HTTP C SDK（C99，仅 libc + BSD socket，无第三方依赖）
# 交叉编译示例: make CC=arm-linux-gnueabihf-gcc
CC      ?= gcc
CFLAGS  ?= -O2
CFLAGS  += -std=c99 -Wall -Wextra
AR      ?= ar

LIB     = libccshttp.a
LIBOBJ  = ccs_http.o ccs_sdk.o
EXAMPLES = examples/demo_stat examples/demo_snapshot examples/demo_uart

all: $(LIB) $(EXAMPLES)

$(LIB): $(LIBOBJ)
	$(AR) rcs $@ $(LIBOBJ)

%.o: %.c ccs_http.h ccs_sdk.h
	$(CC) $(CFLAGS) -c -o $@ $<

examples/%: examples/%.c $(LIB)
	$(CC) $(CFLAGS) -o $@ $< $(LIB)

clean:
	rm -f $(LIBOBJ) $(LIB) $(EXAMPLES) examples/snapshot.jpg snapshot.jpg

.PHONY: all clean
