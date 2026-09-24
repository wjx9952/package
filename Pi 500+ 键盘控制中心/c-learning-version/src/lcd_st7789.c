#include "pi500.h"

#include <fcntl.h>
#include <linux/spi/spidev.h>
#include <stdlib.h>
#include <sys/ioctl.h>
#include <unistd.h>

struct Lcd {
    int spi_fd;
    uint32_t speed_hz;
    bool backlight;
};

Lcd *lcd_open(const char *spidev_path, uint32_t speed_hz)
{
    Lcd *lcd = calloc(1, sizeof(*lcd));
    if (lcd == NULL)
        return NULL;
    lcd->spi_fd = open(spidev_path, O_RDWR | O_CLOEXEC);
    if (lcd->spi_fd < 0) {
        free(lcd);
        return NULL;
    }

    uint8_t mode = SPI_MODE_0;
    uint8_t bits = 8;
    lcd->speed_hz = speed_hz;
    if (ioctl(lcd->spi_fd, SPI_IOC_WR_MODE, &mode) < 0 ||
        ioctl(lcd->spi_fd, SPI_IOC_WR_BITS_PER_WORD, &bits) < 0 ||
        ioctl(lcd->spi_fd, SPI_IOC_WR_MAX_SPEED_HZ, &speed_hz) < 0) {
        lcd_close(lcd);
        return NULL;
    }

    /*
     * 生产版还需要通过 GPIO 控制 DC、RESET、BACKLIGHT，并发送
     * SWRESET、SLPOUT、COLMOD、MADCTL、DISPON 等 ST7789 初始化命令。
     * 这里保留 SPI 数据通路，避免学习代码误操作正在使用的小屏幕。
     */
    return lcd;
}

bool lcd_write_frame(Lcd *lcd, const uint16_t *rgb565, size_t pixels)
{
    if (lcd == NULL || rgb565 == NULL || pixels != LCD_PIXELS)
        return false;
    const uint8_t *data = (const uint8_t *)rgb565;
    size_t remaining = pixels * sizeof(uint16_t);
    while (remaining > 0) {
        size_t chunk = remaining > 4096 ? 4096 : remaining;
        ssize_t written = write(lcd->spi_fd, data, chunk);
        if (written <= 0)
            return false;
        data += (size_t)written;
        remaining -= (size_t)written;
    }
    return true;
}

void lcd_set_backlight(Lcd *lcd, bool enabled)
{
    if (lcd != NULL)
        lcd->backlight = enabled;
}

void lcd_close(Lcd *lcd)
{
    if (lcd == NULL)
        return;
    if (lcd->spi_fd >= 0)
        close(lcd->spi_fd);
    free(lcd);
}

