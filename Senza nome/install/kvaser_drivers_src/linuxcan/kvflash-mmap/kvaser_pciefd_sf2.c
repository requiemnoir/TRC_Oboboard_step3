/**
 * Kvaser Smartfusion2 hardware layer
 */

#include "kvaser_pciefd_sf2.h"
#include "hydra_flash.h"
#include "kv_flash.h"
#include "kvaser_pciefd.h"
#include "kcan_led.h"
#include "spi_flash.h"
#include "util.h"
#include "flash_meta_sf2.h"

#include <errno.h>
#include <fcntl.h>
#include <stdbool.h>
#include <stdio.h> /* snprintf */
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define SF2_GPIO_PIN_TRIG_UPDATE 0

// Main block offsets (bytes)
#define OFFSET_TECH 0x00000000
#define OFFSET_KCAN 0x00100000

// Smartfusion2 peripherals
#define SF2_SPI0_BASE  0x00001000u
#define SF2_GPIO_BASE  0x00013000u
#define OFFSET_SF2_AHB OFFSET_TECH

#define OFFSET_SPI  (OFFSET_SF2_AHB + SF2_SPI0_BASE)
#define OFFSET_GPIO (OFFSET_SF2_AHB + SF2_GPIO_BASE)

// KCAN sub block offsets (bytes)
#define OFFSET_KCAN_META    (OFFSET_KCAN + 0x00000)
#define OFFSET_KCAN_TX0     (OFFSET_KCAN + 0x40000)
#define OFFSET_KCAN_TX1     (OFFSET_KCAN + 0x42000)
#define CAN_CONTROLLER_SPAN (OFFSET_KCAN_TX1 - OFFSET_KCAN_TX0)

#define SF2_GPIO_OUT (OFFSET_GPIO + 0x0088)
static int firmware_upgrade_trigger_update_sf2(void *ctx)
{
    struct kvaser_pciefd *kvaser_pciefd_dev = ctx;
    struct timespec ts = {
        .tv_sec = 50,
        .tv_nsec = 0,
    };
    u32 gpio_out;
    int ret;

    gpio_out = ioread32(kvaser_pciefd_dev->reg_base + SF2_GPIO_OUT);
    gpio_out |= BIT(SF2_GPIO_PIN_TRIG_UPDATE);
    iowrite32(gpio_out, kvaser_pciefd_dev->reg_base + SF2_GPIO_OUT);
    /* Wait 50 seconds for the Smartfusion2 SoC to re-program and restart itself */
    ret = nanosleep(&ts, NULL);
    if (ret) {
        return HYDRA_FLASH_STAT_TIMEOUT;
    }

    return HYDRA_FLASH_STAT_OK;
}

static void display_update_state_sf2(void *data, bool on)
{
    struct kvaser_pciefd *kvaser_pciefd_dev = data;
    int i;

    // Turn all LED:s on/off
    for (i = 0; i < kvaser_pciefd_dev->nr_channels; i++) {
        KCAN_LED_set(kvaser_pciefd_dev->reg_base + OFFSET_KCAN_TX0 + CAN_CONTROLLER_SPAN * i, on);
    }
}

#define PATH_PCI_RESCAN "/sys/bus/pci/rescan"
static int post_finish_sf2(struct kvaser_device *device)
{
    int fd_remove;
    int fd_rescan;
    ssize_t res;

    struct kv_pci_device *kv_pci_dev;
    struct kvaser_libpci *kv_libpci_dev;
    char *sysfs_device_base_path;
    char sysfs_device_remove_path[256];

    assert(device);
    kv_pci_dev = device->lib_data;
    assert(kv_pci_dev);

    kv_libpci_dev = &kv_pci_dev->libpci;
    sysfs_device_base_path = kv_libpci_dev->sysfs_path;
    /* Remove PCI device */
    snprintf(sysfs_device_remove_path, sizeof(sysfs_device_remove_path), "%s/remove",
             sysfs_device_base_path);
    fd_remove = open(sysfs_device_remove_path, O_WRONLY | O_SYNC);
    if (fd_remove < 0) {
        printf("Error: open() failed for \"%s\": %m\n", sysfs_device_remove_path);
        if (errno == EACCES) {
            printf("Make sure you are running as root!\n");
        }
        return 1;
    }
    res = write(fd_remove, "1", 1);
    close(fd_remove);
    if (res != 1) {
        printf("Error: Failed to write \"1\" to \"%s\": %m\n", sysfs_device_remove_path);

        return 1;
    }

    /* Rescan PCI bus */
    fd_rescan = open(PATH_PCI_RESCAN, O_WRONLY | O_SYNC);
    if (fd_rescan < 0) {
        printf("Error: open() failed for \"%s\": %m\n", PATH_PCI_RESCAN);
        return 1;
    }
    res = write(fd_rescan, "1", 1);
    close(fd_rescan);
    if (res != 1) {
        printf("Error: Failed to write \"1\" to \"%s\": %m\n", PATH_PCI_RESCAN);

        return 1;
    }

    return 0;
}

const struct hydra_flash_device_ops hydra_flash_device_ops_sf2 = {
    .firmware_upgrade_trigger_update = &firmware_upgrade_trigger_update_sf2,
    .display_update_state = &display_update_state_sf2,
};

const struct hydra_flash_image_def flash_meta_sf2 = {
    .size = FLASH_SIZE,
    .fpga_image_offset = FLASH_FPGA_IMAGE_OFFSET,
    .fpga_image_size_max = FLASH_FPGA_IMAGE_SIZE_MAX,
    .param_image_offset = FLASH_PARAM_IMAGE_OFFSET,
    .param_image_size_max = FLASH_PARAM_IMAGE_SIZE_MAX,
};

const struct pciefd_driver_data PCIEFD_DRIVER_DATA_SF2 = {
    .offset_meta = OFFSET_KCAN_META,
    .offset_spi = OFFSET_SPI,
    .flash_meta = &flash_meta_sf2,
    .spi_ops = &SPI_FLASH_sf2_ops,
    .hydra_flash_ops = &hydra_flash_device_ops_sf2,
    .post_commit_str = "\tThe Kvaser PCI devices was restarted and PCI bus was rescanned.\n"
                       "\tThe devices is now ready to be used.",
    .post_finish = &post_finish_sf2,
};
