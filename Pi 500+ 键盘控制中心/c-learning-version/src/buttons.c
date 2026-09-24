#include "pi500.h"

#include <errno.h>
#include <gpiod.h>
#include <stdlib.h>
#include <sys/epoll.h>

typedef struct {
    ButtonId id;
    unsigned pin;
    struct gpiod_line *line;
    int fd;
} ButtonLine;

struct ButtonBank {
    struct gpiod_chip *chip;
    ButtonLine lines[BUTTON_COUNT];
};

static const unsigned pins[BUTTON_COUNT] = {
    PIN_KEY1, PIN_KEY2, PIN_KEY3, PIN_JOY_UP,
    PIN_JOY_DOWN, PIN_JOY_LEFT, PIN_JOY_RIGHT, PIN_JOY_PRESS,
};

ButtonBank *buttons_open(void)
{
    ButtonBank *bank = calloc(1, sizeof(*bank));
    if (bank == NULL)
        return NULL;
    bank->chip = gpiod_chip_open_by_name("gpiochip0");
    if (bank->chip == NULL) {
        free(bank);
        return NULL;
    }

    for (int i = 0; i < BUTTON_COUNT; ++i) {
        bank->lines[i].id = (ButtonId)i;
        bank->lines[i].pin = pins[i];
        bank->lines[i].line = gpiod_chip_get_line(bank->chip, pins[i]);
        if (bank->lines[i].line == NULL ||
            gpiod_line_request_both_edges_events(
                bank->lines[i].line, "pi500-c-learning") != 0) {
            buttons_close(bank);
            return NULL;
        }
        bank->lines[i].fd = gpiod_line_event_get_fd(bank->lines[i].line);
    }
    return bank;
}

int buttons_add_to_epoll(ButtonBank *bank, int epoll_fd)
{
    for (int i = 0; i < BUTTON_COUNT; ++i) {
        struct epoll_event event = {
            .events = EPOLLIN,
            .data.fd = bank->lines[i].fd,
        };
        if (epoll_ctl(epoll_fd, EPOLL_CTL_ADD, bank->lines[i].fd, &event) != 0)
            return -1;
    }
    return 0;
}

bool buttons_read_event(ButtonBank *bank, int fd, ButtonId *id, bool *pressed)
{
    for (int i = 0; i < BUTTON_COUNT; ++i) {
        if (bank->lines[i].fd != fd)
            continue;
        struct gpiod_line_event event;
        if (gpiod_line_event_read(bank->lines[i].line, &event) != 0)
            return false;
        *id = bank->lines[i].id;
        *pressed = event.event_type == GPIOD_LINE_EVENT_FALLING_EDGE;
        return true;
    }
    errno = ENOENT;
    return false;
}

void buttons_close(ButtonBank *bank)
{
    if (bank == NULL)
        return;
    for (int i = 0; i < BUTTON_COUNT; ++i) {
        if (bank->lines[i].line != NULL)
            gpiod_line_release(bank->lines[i].line);
    }
    if (bank->chip != NULL)
        gpiod_chip_close(bank->chip);
    free(bank);
}

