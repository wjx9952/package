#include "pi500.h"

#include <arpa/inet.h>
#include <string.h>

static uint16_t rgb565(unsigned red, unsigned green, unsigned blue)
{
    uint16_t value = (uint16_t)(((red & 0xf8u) << 8) |
                                ((green & 0xfcu) << 3) |
                                (blue >> 3));
    return htons(value);
}

static void fill(uint16_t *frame, uint16_t colour)
{
    for (size_t i = 0; i < LCD_PIXELS; ++i)
        frame[i] = colour;
}

static void rectangle(uint16_t *frame, int x, int y, int width, int height,
                      uint16_t colour)
{
    for (int row = y; row < y + height && row < LCD_HEIGHT; ++row) {
        for (int column = x; column < x + width && column < LCD_WIDTH; ++column) {
            if (row >= 0 && column >= 0)
                frame[row * LCD_WIDTH + column] = colour;
        }
    }
}

void ui_render(uint16_t *frame, const AppState *state)
{
    const uint16_t black = rgb565(0, 0, 0);
    const uint16_t card = rgb565(10, 15, 21);
    const uint16_t track = rgb565(39, 49, 63);
    const uint16_t blue = rgb565(54, 196, 255);
    const uint16_t purple = rgb565(176, 93, 255);
    const uint16_t online = rgb565(52, 224, 91);
    const uint16_t offline = rgb565(255, 69, 58);

    fill(frame, black);
    rectangle(frame, 8, 62, 224, 82, card);
    rectangle(frame, 8, 152, 224, 80, card);
    rectangle(frame, 18, 34, 8, 8,
              state->network.online ? online : offline);

    rectangle(frame, 18, 116, 204, 9, track);
    rectangle(frame, 18, 116,
              204 * state->quota.five_hour_remaining / 100, 9, blue);
    rectangle(frame, 18, 206, 204, 9, track);
    rectangle(frame, 18, 206,
              204 * state->quota.seven_day_remaining / 100, 9, purple);

    /*
     * 为了让示例聚焦 C 与硬件机制，这里只画布局、状态点和进度条。
     * 完整文字可使用 Cairo/Pango 先渲染 ARGB，再转换为 RGB565。
     */
}

