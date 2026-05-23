/**
 * Kvaser Altera hardware layer
 */

#include "kvaser_pciefd_altera.h"
#include "hydra_flash.h"
#include "kvaser_pciefd.h"
#include "kcan_led.h"
#include "spi_flash.h"
#include "flash_meta_altera.h"

// Main block offsets (bytes)
#define OFFSET_KCAN 0x00010000
#define OFFSET_MISC 0x0001f800

#define OFFSET_SPI_FLASH (OFFSET_MISC)

// KCAN sub block offsets (bytes)
#define OFFSET_KCAN_META    (OFFSET_KCAN + 0x0f020)
#define OFFSET_KCAN_TX0     (OFFSET_KCAN + 0x00000)
#define OFFSET_KCAN_TX1     (OFFSET_KCAN + 0x01000)
#define CAN_CONTROLLER_SPAN (OFFSET_KCAN_TX1 - OFFSET_KCAN_TX0)

static void display_update_state_altera(void *data, bool on)
{
    struct kvaser_pciefd *kvaser_pciefd_dev = data;
    int i;

    // Turn all LED:s on/off
    for (i = 0; i < kvaser_pciefd_dev->nr_channels; i++) {
        KCAN_LED_set(kvaser_pciefd_dev->reg_base + OFFSET_KCAN_TX0 + CAN_CONTROLLER_SPAN * i, on);
    }
}

const struct hydra_flash_device_ops hydra_flash_device_ops_altera = {
    .firmware_upgrade_trigger_update = NULL,
    .display_update_state = &display_update_state_altera,
};

const struct hydra_flash_image_def flash_meta_altera = {
    .size = FLASH_SIZE,
    .fpga_image_offset = FLASH_FPGA_IMAGE_OFFSET,
    .fpga_image_size_max = FLASH_FPGA_IMAGE_SIZE_MAX,
    .param_image_offset = FLASH_PARAM_IMAGE_OFFSET,
    .param_image_size_max = FLASH_PARAM_IMAGE_SIZE_MAX,
};

const struct pciefd_driver_data PCIEFD_DRIVER_DATA_ALTERA = {
    .offset_meta = OFFSET_KCAN_META,
    .offset_spi = OFFSET_SPI_FLASH,
    .flash_meta = &flash_meta_altera,
    .spi_ops = &SPI_FLASH_altera_ops,
    .hydra_flash_ops = &hydra_flash_device_ops_altera,
    .post_commit_str =
        "\tNote: In order to complete the firmware upgrade of the PCIEcan device, a complete shutdown is required",
    .post_finish = NULL,
};
