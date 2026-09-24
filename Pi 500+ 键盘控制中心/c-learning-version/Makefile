CC ?= cc
CFLAGS ?= -std=c17 -O2 -Wall -Wextra -Wpedantic
CPPFLAGS += -Iinclude
LDLIBS += -lgpiod -lcurl -ljson-c

SOURCES := \
	src/main.c \
	src/state_machine.c \
	src/network.c \
	src/codex_monitor.c \
	src/quota_client.c \
	src/buttons.c \
	src/lcd_st7789.c \
	src/ui.c
OBJECTS := $(SOURCES:.c=.o)
TARGET := pi500-control-learning

.PHONY: all clean

all: $(TARGET)

$(TARGET): $(OBJECTS)
	$(CC) $(OBJECTS) $(LDLIBS) -o $@

clean:
	$(RM) $(OBJECTS) $(TARGET)
