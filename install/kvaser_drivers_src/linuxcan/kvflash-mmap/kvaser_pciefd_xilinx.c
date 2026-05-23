/**
 * Kvaser Xilinx hardware layer
 */

#include "hydra_flash.h"
#include "kv_flash.h"
#include "kvaser_pciefd.h"
#include "kcan_led.h"
#include "spi_flash.h"
#include "util.h"
#include "flash_meta_xilinx.h"
#include "kvaser_pciefd_xilinx.h"

// Main block offsets (bytes)
#define OFFSET_TECH 0x00000000
#define OFFSET_KCAN 0x00100000

// Xilinx peripherals
#define OFFSET_SPI (OFFSET_TECH + 0x1000)

// KCAN sub block offsets (bytes)
#define OFFSET_KCAN_META    (OFFSET_KCAN + 0x00000)
#define OFFSET_KCAN_TX0     (OFFSET_KCAN + 0x40000)
#define OFFSET_KCAN_TX1     (OFFSET_KCAN + 0x42000)
#define CAN_CONTROLLER_SPAN (OFFSET_KCAN_TX1 - OFFSET_KCAN_TX0)

static void display_update_state_xilinx(void *data, bool on)
{
    struct kvaser_pciefd *kvaser_pciefd_dev = data;
    int i;

    // Turn all LED:s on/off
    for (i = 0; i < kvaser_pciefd_dev->nr_channels; i++) {
        KCAN_LED_set(kvaser_pciefd_dev->reg_base + OFFSET_KCAN_TX0 + CAN_CONTROLLER_SPAN * i, on);
    }
}

const struct hydra_flash_device_ops hydra_flash_device_ops_xilinx = {
    .firmware_upgrade_trigger_update = NULL,
    .display_update_state = &display_update_state_xilinx,
};

const struct hydra_flash_image_def flash_meta_xilinx = {
    .size = FLASH_SIZE,
    .fpga_image_offset = FLASH_FPGA_IMAGE_OFFSET,
    .fpga_image_size_max = FLASH_FPGA_IMAGE_SIZE_MAX,
    .param_image_offset = FLASH_PARAM_IMAGE_OFFSET,
    .param_image_size_max = FLASH_PARAM_IMAGE_SIZE_MAX,
};

const struct pciefd_driver_data PCIEFD_DRIVER_DATA_XILINX = {
    .offset_meta = OFFSET_KCAN_META,
    .offset_spi = OFFSET_SPI,
    .flash_meta = &flash_meta_xilinx,
    .spi_ops = &SPI_FLASH_xilinx_ops,
    .hydra_flash_ops = &hydra_flash_device_ops_xilinx,
    .post_commit_str =
        "\tNote: In order to complete the firmware upgrade of the PCIEcan device, a complete shutdown is required",
    .post_finish = NULL,
};
