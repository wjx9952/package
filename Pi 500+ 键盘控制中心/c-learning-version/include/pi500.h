#ifndef PI500_H
#define PI500_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <time.h>

#define LCD_WIDTH 240
#define LCD_HEIGHT 240
#define LCD_PIXELS (LCD_WIDTH * LCD_HEIGHT)

#define PIN_DC 25
#define PIN_RESET 27
#define PIN_BACKLIGHT 24
#define PIN_KEY1 21
#define PIN_KEY2 20
#define PIN_KEY3 16
#define PIN_JOY_UP 6
#define PIN_JOY_DOWN 19
#define PIN_JOY_LEFT 5
#define PIN_JOY_RIGHT 26
#define PIN_JOY_PRESS 13

typedef enum {
    CODEX_IDLE,
    CODEX_PROCESSING,
    CODEX_COMPLETED,
    CODEX_CONFIRMATION,
} CodexState;

typedef enum {
    LIGHT_IDLE,
    LIGHT_PROCESSING,
    LIGHT_COMPLETED,
    LIGHT_CONFIRMATION,
    LIGHT_KEYBOARD_MODE,
} LightingState;

typedef enum {
    BUTTON_KEY1,
    BUTTON_KEY2,
    BUTTON_KEY3,
    BUTTON_JOY_UP,
    BUTTON_JOY_DOWN,
    BUTTON_JOY_LEFT,
    BUTTON_JOY_RIGHT,
    BUTTON_JOY_PRESS,
    BUTTON_COUNT,
} ButtonId;

typedef struct {
    bool online;
    unsigned consecutive_failures;
    char ipv4[16];
} NetworkState;

typedef struct {
    bool ok;
    int five_hour_remaining;
    int seven_day_remaining;
    time_t fetched_at;
    time_t five_hour_reset;
    time_t seven_day_reset;
} QuotaState;

typedef struct {
    CodexState codex;
    LightingState lighting;
    bool keyboard_mode;
    bool locked;
    NetworkState network;
    QuotaState quota;
} AppState;

typedef struct ButtonBank ButtonBank;
typedef struct Lcd Lcd;

LightingState effective_lighting_state(CodexState codex, bool keyboard_mode);
const char *codex_state_name(CodexState state);

bool network_refresh(NetworkState *state);
CodexState codex_read_combined_status(const char *codex_home);
bool quota_fetch(const char *url, QuotaState *quota);

ButtonBank *buttons_open(void);
int buttons_add_to_epoll(ButtonBank *bank, int epoll_fd);
bool buttons_read_event(ButtonBank *bank, int fd, ButtonId *id, bool *pressed);
void buttons_close(ButtonBank *bank);

Lcd *lcd_open(const char *spidev_path, uint32_t speed_hz);
bool lcd_write_frame(Lcd *lcd, const uint16_t *rgb565, size_t pixels);
void lcd_set_backlight(Lcd *lcd, bool enabled);
void lcd_close(Lcd *lcd);

void ui_render(uint16_t *frame, const AppState *state);

#endif
