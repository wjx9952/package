#define _GNU_SOURCE
#include "pi500.h"

#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/epoll.h>
#include <sys/signalfd.h>
#include <sys/timerfd.h>
#include <unistd.h>

typedef struct {
    bool pressed;
    bool long_fired;
    struct timespec pressed_at;
} PressState;

static int make_periodic_timer(void)
{
    int fd = timerfd_create(CLOCK_MONOTONIC, TFD_CLOEXEC | TFD_NONBLOCK);
    struct itimerspec timer = {
        .it_interval = {.tv_sec = 1, .tv_nsec = 0},
        .it_value = {.tv_sec = 1, .tv_nsec = 0},
    };
    if (fd >= 0 && timerfd_settime(fd, 0, &timer, NULL) != 0) {
        close(fd);
        return -1;
    }
    return fd;
}

static long elapsed_ms(const struct timespec *start, const struct timespec *end)
{
    return (end->tv_sec - start->tv_sec) * 1000L +
           (end->tv_nsec - start->tv_nsec) / 1000000L;
}

static void short_press(AppState *state, ButtonId id)
{
    switch (id) {
    case BUTTON_KEY1:
        state->keyboard_mode = !state->keyboard_mode;
        break;
    case BUTTON_KEY2:
        puts("KEY2: toggle Clash Verge TUN through its local API");
        break;
    case BUTTON_KEY3:
        puts("KEY3: confirm the pending Codex request");
        break;
    case BUTTON_JOY_LEFT:
        puts("Joystick left: switch AOC to DisplayPort");
        break;
    case BUTTON_JOY_RIGHT:
        puts("Joystick right: switch AOC to HDMI");
        break;
    default:
        break;
    }
}

int main(void)
{
    AppState state = {
        .codex = CODEX_IDLE,
        .lighting = LIGHT_IDLE,
        .network = {.online = false, .ipv4 = "--"},
        .quota = {.ok = false},
    };
    PressState presses[BUTTON_COUNT] = {0};
    const char *home = getenv("HOME");
    char codex_home[4096];
    snprintf(codex_home, sizeof(codex_home), "%s/.codex", home ? home : "");
    unsigned ticks = 0;
    uint16_t *frame = calloc(LCD_PIXELS, sizeof(*frame));
    if (frame == NULL)
        return EXIT_FAILURE;

    int epoll_fd = epoll_create1(EPOLL_CLOEXEC);
    int timer_fd = make_periodic_timer();
    ButtonBank *buttons = buttons_open();
    if (epoll_fd < 0 || timer_fd < 0 || buttons == NULL)
        goto fail;

    struct epoll_event timer_event = {.events = EPOLLIN, .data.fd = timer_fd};
    if (epoll_ctl(epoll_fd, EPOLL_CTL_ADD, timer_fd, &timer_event) != 0 ||
        buttons_add_to_epoll(buttons, epoll_fd) != 0)
        goto fail;

    puts("C learning daemon started; this source is not installed by default.");
    for (;;) {
        struct epoll_event events[16];
        int count = epoll_wait(epoll_fd, events, 16, -1);
        if (count < 0) {
            if (errno == EINTR)
                continue;
            break;
        }

        for (int i = 0; i < count; ++i) {
            if (events[i].data.fd == timer_fd) {
                uint64_t expirations = 0;
                (void)read(timer_fd, &expirations, sizeof(expirations));
                ticks += (unsigned)expirations;
                network_refresh(&state.network);
                state.codex = codex_read_combined_status(codex_home);
                if (state.network.online && (ticks == 1 || ticks % 60 == 0)) {
                    /* quota_fetch only overwrites the structure on success. */
                    (void)quota_fetch("http://127.0.0.1:8765/quota", &state.quota);
                }
                state.lighting = effective_lighting_state(
                    state.codex, state.keyboard_mode);
                ui_render(frame, &state);

                struct timespec now;
                clock_gettime(CLOCK_MONOTONIC, &now);
                for (int id = 0; id < BUTTON_COUNT; ++id) {
                    if (presses[id].pressed && !presses[id].long_fired &&
                        elapsed_ms(&presses[id].pressed_at, &now) >= 2000) {
                        presses[id].long_fired = true;
                        printf("button %d long press\n", id);
                    }
                }
                continue;
            }

            ButtonId id;
            bool pressed;
            if (!buttons_read_event(buttons, events[i].data.fd, &id, &pressed))
                continue;
            if (pressed) {
                presses[id].pressed = true;
                presses[id].long_fired = false;
                clock_gettime(CLOCK_MONOTONIC, &presses[id].pressed_at);
            } else {
                if (presses[id].pressed && !presses[id].long_fired)
                    short_press(&state, id);
                presses[id].pressed = false;
            }
        }
    }

fail:
    buttons_close(buttons);
    if (timer_fd >= 0)
        close(timer_fd);
    if (epoll_fd >= 0)
        close(epoll_fd);
    free(frame);
    return EXIT_FAILURE;
}
