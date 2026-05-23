// SPDX-License-Identifier: GPL-2.0 OR BSD-2-Clause
/* Copyright (C) 2021 KVASER AB, Sweden. All rights reserved.
 * Parts of this driver are based on the following:
 *  - Kvaser linux pciefd driver (version 5.35)
 *  - Altera Avalon EPCS flash controller driver
 */
#include "kvaser_pciefd.h"
#include "kvaser_pciefd_altera.h"
#include "kvaser_pciefd_sf2.h"
#include "kvaser_pciefd_xilinx.h"
#include "kv_flash.h"
#include "hydra_imgheader.h"
#include "hydra_flash.h"
#include "kcan_ioctl_flash.h"
#include "fpga_meta.h"
#include "spi_flash.h"
#include "util.h"

#include <assert.h>
#include <endian.h> /* le32toh */
#include <errno.h> /* EIO, ENODEV, ENOMEM */
#include <fcntl.h> /* open, O_RDWR, O_SYNC */
#include <libgen.h> /* basename */
#include <linux/kernel.h> /* __le32 */
#include <linux/types.h>
#include <pci/pci.h>
#include <stddef.h> /* size_t */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h> /* calloc, free */
#include <string.h> /* memcpy, strncpy */
#include <sys/mman.h> /* mmap, munmap, MAP_SHARED, MAP_FAILED, PROT_READ, PROT_WRITE */
#include <unistd.h> /* access, close, readlink */

#define KVASER_PCIEFD_DRV_NAME "kvpciefd_mmap"

#define KVASER_PCIEFD_MAX_CAN_CHANNELS 4

#define KVASER_PCIEFD_VENDOR_ID 0x1A07

/* Altera */
#define KVASER_ID_ALTERA_PCIEFD_4HS        0x000d
#define KVASER_ID_ALTERA_PCIEFD_2HS_V2     0x000e
#define KVASER_ID_ALTERA_PCIEFD_1HS_V2     0x000f
#define KVASER_ID_ALTERA_MINIPCIEFD_1HS_V2 0x0010
#define KVASER_ID_ALTERA_MINIPCIEFD_2HS_V2 0x0011

/* Smartfusion2 */
#define KVASER_ID_SF2_PCIEFD_2CAN_V3     0x0012
#define KVASER_ID_SF2_PCIEFD_1CAN_V3     0x0013
#define KVASER_ID_SF2_PCIEFD_4CAN_V2     0x0014
#define KVASER_ID_SF2_MINIPCIEFD_2CAN_V3 0x0015
#define KVASER_ID_SF2_MINIPCIEFD_1CAN_V3 0x0016

/* Xilinx */
#define KVASER_ID_XILINX_M2_4HS 0x0017
#define KVASER_ID_XILINX_PCIEFD_8XCAN 0x0019

/* EPCS flash controller registers */
#define KVASER_PCIEFD_SPI_BASE       0x1fc00
#define KVASER_PCIEFD_SPI_RX_REG     KVASER_PCIEFD_SPI_BASE
#define KVASER_PCIEFD_SPI_TX_REG     (KVASER_PCIEFD_SPI_BASE + 0x4)
#define KVASER_PCIEFD_SPI_STATUS_REG (KVASER_PCIEFD_SPI_BASE + 0x8)
#define KVASER_PCIEFD_SPI_CTRL_REG   (KVASER_PCIEFD_SPI_BASE + 0xc)
#define KVASER_PCIEFD_SPI_SSEL_REG   (KVASER_PCIEFD_SPI_BASE + 0x14)

/* EPCS flash controller definitions */
#define KVASER_PCIEFD_FLASH_MAX_IMAGE_SZ (1024 * 1024)
#define KVASER_PCIEFD_FLASH_BLOCK_CNT    32
#define KVASER_PCIEFD_FLASH_BLOCK_SZ     65536L
#define KVASER_PCIEFD_FLASH_PAGE_SZ      256
#define KVASER_PCIEFD_FLASH_SZ           (KVASER_PCIEFD_FLASH_BLOCK_CNT * KVASER_PCIEFD_FLASH_BLOCK_SZ)
#define KVASER_PCIEFD_CFG_IMG_SZ         (64 * 1024)
#define KVASER_PCIEFD_CFG_IMG_OFFSET     (31 * KVASER_PCIEFD_FLASH_BLOCK_SZ)
#define KVASER_PCIEFD_CFG_MAX_PARAMS     256
#define KVASER_PCIEFD_CFG_MAGIC          0xcafef00d
#define KVASER_PCIEFD_CFG_PARAM_MAX_SZ   24
#define KVASER_PCIEFD_CFG_SYS_VER        1
#define KVASER_PCIEFD_CFG_PARAM_SERIAL   124
#define KVASER_PCIEFD_CFG_PARAM_EAN      129
#define KVASER_PCIEFD_CFG_PARAM_NR_CHAN  130

#define KVASER_PCIEFD_SPI_TMT             BIT(5)
#define KVASER_PCIEFD_SPI_TRDY            BIT(6)
#define KVASER_PCIEFD_SPI_RRDY            BIT(7)
#define KVASER_PCIEFD_FLASH_ID_EPCS16     0x14
/* If you need to make multiple accesses to the same slave then you should
 * set the merge bit in the flags for all of them except the first.
 */
#define KVASER_PCIEFD_FLASH_CMD_MERGE     0x1
/* Commands for controlling the onboard flash */
#define KVASER_PCIEFD_FLASH_RES_CMD       0xab
#define KVASER_PCIEFD_FLASH_PP_CMD        0x2
#define KVASER_PCIEFD_FLASH_READ_CMD      0x3
#define KVASER_PCIEFD_FLASH_STATUS_CMD    0x5
#define KVASER_PCIEFD_FLASH_WREN_CMD      0x6
#define KVASER_PCIEFD_FLASH_SEC_ERASE_CMD 0xd8

#define KVASER_PCIEFD_PCI_CLASS_CAN   0x0c09
#define KVASER_PCIEFD_PCI_REGION_SIZE 0x20000

#define le32_to_cpu le32toh

#define ARRAY_SIZE(arr)                                                                          \
    (sizeof(arr) / sizeof((arr)[0]) +                                                            \
     sizeof(typeof(int[1 - 2 * !!__builtin_types_compatible_p(typeof(arr), typeof(&arr[0]))])) * \
         0)

#define kv_print_err(...)  fprintf(stderr, __VA_ARGS__)
#define kv_print_info(...) printf(__VA_ARGS__)
#if DEBUG
#define kv_print_dbg(...) printf(__VA_ARGS__)
#else
#define kv_print_dbg(...)
#endif /* DEBUG */

#define kv_print_dbgv(...)

struct kvaser_pciefd_cfg_param {
    __le32 magic;
    __le32 nr;
    __le32 len;
    u8 data[KVASER_PCIEFD_CFG_PARAM_MAX_SZ];
};

struct kvaser_pciefd_cfg_img {
    __le32 version;
    __le32 magic;
    __le32 crc;
    struct kvaser_pciefd_cfg_param params[KVASER_PCIEFD_CFG_MAX_PARAMS];
};

struct kvaser_pciefd_device_id {
    u16 device_id;
    const struct pciefd_driver_data *driver_data;
};

static const struct kvaser_pciefd_device_id kvaser_pciefd_id_table[] = {
    {
        .device_id = KVASER_ID_ALTERA_PCIEFD_4HS,
        .driver_data = &PCIEFD_DRIVER_DATA_ALTERA,
    },
    {
        .device_id = KVASER_ID_ALTERA_PCIEFD_2HS_V2,
        .driver_data = &PCIEFD_DRIVER_DATA_ALTERA,
    },
    {
        .device_id = KVASER_ID_ALTERA_PCIEFD_1HS_V2,
        .driver_data = &PCIEFD_DRIVER_DATA_ALTERA,
    },
    {
        .device_id = KVASER_ID_ALTERA_MINIPCIEFD_1HS_V2,
        .driver_data = &PCIEFD_DRIVER_DATA_ALTERA,
    },
    {
        .device_id = KVASER_ID_ALTERA_MINIPCIEFD_2HS_V2,
        .driver_data = &PCIEFD_DRIVER_DATA_ALTERA,
    },
    {
        .device_id = KVASER_ID_SF2_PCIEFD_2CAN_V3,
        .driver_data = &PCIEFD_DRIVER_DATA_SF2,
    },
    {
        .device_id = KVASER_ID_SF2_PCIEFD_1CAN_V3,
        .driver_data = &PCIEFD_DRIVER_DATA_SF2,
    },
    {
        .device_id = KVASER_ID_SF2_PCIEFD_4CAN_V2,
        .driver_data = &PCIEFD_DRIVER_DATA_SF2,
    },
    {
        .device_id = KVASER_ID_SF2_MINIPCIEFD_2CAN_V3,
        .driver_data = &PCIEFD_DRIVER_DATA_SF2,
    },
    {
        .device_id = KVASER_ID_SF2_MINIPCIEFD_1CAN_V3,
        .driver_data = &PCIEFD_DRIVER_DATA_SF2,
    },
    {
        .device_id = KVASER_ID_XILINX_M2_4HS,
        .driver_data = &PCIEFD_DRIVER_DATA_XILINX,
    },
    {
        .device_id = KVASER_ID_XILINX_PCIEFD_8XCAN,
        .driver_data = &PCIEFD_DRIVER_DATA_XILINX,
    },
    {
        0,
    },
};

struct kvaser_pciefd_dev_name {
    char *name;
    unsigned int ean[2];
};

static const struct kvaser_pciefd_dev_name kvaser_pciefd_dev_name_list[] = {
    { "Kvaser PCIEcan 4xHS", { 0x30006836, 0x00073301 } },
    { "Kvaser PCIEcan 2xHS v2", { 0x30008618, 0x00073301 } },
    { "Kvaser PCIEcan HS v2", { 0x30008663, 0x00073301 } },
    { "Kvaser Mini PCI Express 2xHS v2", { 0x30010291, 0x00073301 } },
    { "Kvaser Mini PCI Express HS v2", { 0x30010383, 0x00073301 } },
    { "Kvaser PCIEcan 2xCAN v3", { 0x30014329, 0x00073301 } },
    { "Kvaser PCIEcan 1xCAN v3", { 0x30014336, 0x00073301 } },
    { "Kvaser PCIEcan 4xCAN v2", { 0x30014145, 0x00073301 } },
    { "Kvaser Mini PCI Express 2xCAN v3", { 0x30014176, 0x00073301 } },
    { "Kvaser Mini PCI Express 1xCAN v3", { 0x30014206, 0x00073301 } },
    { "Kvaser M.2 PCIe 4xCAN", { 0x30013339, 0x00073301 } },
    { "Kvaser PCIEcan 8xCAN", { 0x30015128, 0x00073301 } },
};

static int kvaser_pciefd_setup_board(struct kvaser_pciefd *pcie)
{
    u8 major_rev;
    u8 minor_rev;
    u16 seq_id;
    int ret;

    assert(pcie);
    assert(pcie->reg_base);
    assert(pcie->driver_data);

    ret = HYDRA_FLASH_read_params(&pcie->hflash);
    if (ret != HYDRA_FLASH_STAT_OK) {
        return ret;
    }

    u32 *ean;
    u32 *serial;

    ean = pcie->ean;
    serial = &pcie->serial;

    ean[1] = pcie->hflash.params.ean >> 32;
    ean[0] = pcie->hflash.params.ean & 0xffffffff;
    *serial = pcie->hflash.params.serial_number;
    kv_print_dbg("EAN: " KV_FLASH_EAN_FRMT_STR "\n", ean[1] >> 12,
                 ((ean[1] & 0xfff) << 8) | ((((ean[0])) >> 24) & 0xff), (ean[0] >> 4) & 0xfffff,
                 ean[0] & 0x0f);
    kv_print_dbg("Serial: %u\n", *serial);
    pcie->nr_channels = pcie->hflash.params.nr_channels;

    seq_id = FPGA_META_user_seq_id(pcie->reg_base + pcie->driver_data->offset_meta);
    minor_rev = FPGA_META_minor_rev(pcie->reg_base + pcie->driver_data->offset_meta);
    major_rev = FPGA_META_major_rev(pcie->reg_base + pcie->driver_data->offset_meta);
    kv_print_dbg("Version %u.%u.%u\n", major_rev, minor_rev, seq_id);
    pcie->fw[0] = seq_id;
    pcie->fw[1] = minor_rev;
    pcie->fw[1] |= major_rev << 16;

    return 0;
}

static int kvaser_pciefd_is_device_supported(u16 device_id,
                                             const struct pciefd_driver_data **driver_data)
{
    int i = 0;

    while (kvaser_pciefd_id_table[i].device_id) {
        if (kvaser_pciefd_id_table[i].device_id == device_id) {
            *driver_data = kvaser_pciefd_id_table[i].driver_data;
            return 1;
        }
        i++;
    }

    return 0;
}

static int kvaser_pciefd_scan(struct kvaser_devices *devices)
{
    struct pci_access *pacc;
    struct pci_dev *pci_dev;
    struct pci_filter kvaser_filter;

    assert(devices);
    pacc = pci_alloc();

    if (pacc == NULL) {
        kv_print_err("Error: pci_alloc() failed\n");
        return -ENOMEM;
    }

    pci_filter_init(pacc, &kvaser_filter);
    kvaser_filter.vendor = KVASER_PCIEFD_VENDOR_ID;

    pacc->numeric_ids++;
    pci_init(pacc);
    pci_scan_bus(pacc);
    for (pci_dev = pacc->devices; pci_dev; pci_dev = pci_dev->next) {
        struct kvaser_device *device;
        struct kv_pci_device *kv_pci_dev;
        struct kvaser_libpci *kv_libpci_dev;
        struct kvaser_pciefd *kvaser_pciefd_dev;
        const struct pciefd_driver_data *driver_data;
        char sysfs_base_path[128];
        char sysfs_file_path[256];
        int ret;
        u16 command;

        /* We want vendor and device, class, address and size */
        pci_fill_info(pci_dev, PCI_FILL_IDENT | PCI_FILL_CLASS | PCI_LOOKUP_PROGIF |
                                   PCI_FILL_BASES | PCI_FILL_SIZES);
        /* Does vendor_id match Kvaser? */
        if (!pci_filter_match(&kvaser_filter, pci_dev)) {
            continue;
        }

        if (!kvaser_pciefd_is_device_supported(pci_dev->device_id, &driver_data)) {
            kv_print_info("Warning: Skipping Kvaser device that is not supported: 0x%04x:0x%04x\n",
                          pci_dev->vendor_id, pci_dev->device_id);
            continue;
        }

        /* Verify the PCI class*/
        if (pci_dev->device_class != KVASER_PCIEFD_PCI_CLASS_CAN) {
            kv_print_err(
                "Warning: Wrong device class: Expected 0x%04x, got 0x%04x, ignoring this device\n",
                KVASER_PCIEFD_PCI_CLASS_CAN, pci_dev->device_class);
            continue;
        }

        /* Setup the path to the pci device */
        snprintf(sysfs_base_path, sizeof(sysfs_base_path), "/sys/bus/pci/devices/%04x:%02x:%02x.%d",
                 pci_dev->domain, pci_dev->bus, pci_dev->dev, pci_dev->func);

        /* Check if there is any driver in use */
        snprintf(sysfs_file_path, sizeof(sysfs_file_path), "%s/driver", sysfs_base_path);
        ret = access(sysfs_file_path, F_OK);
        if (ret == 0) {
            char sysfs_driver_link[256];
            size_t len;

            len = readlink(sysfs_file_path, sysfs_driver_link, sizeof(sysfs_driver_link) - 1);
            sysfs_driver_link[len] = '\0';
            kv_print_err("Error: The driver, \"%s\", is using device \"%s\"\n"
                         "       This tool cannot be used when a driver is using the device.\n",
                         basename(sysfs_driver_link), sysfs_base_path);
            return 1;
        }

        kv_print_dbg("Device found: 0x%04x:0x%04x in %s\n", pci_dev->vendor_id, pci_dev->device_id,
                     sysfs_base_path);

        if (devices->count >= MAX_KV_PCI_DEVICES) {
            kv_print_err(
                "Warning: Found more devices than %u, ignoring this device\n"
                "         To support more devices, increase define MAX_KV_PCI_DEVICE and recompile\n",
                MAX_KV_PCI_DEVICES);
            continue;
        }

        device = &devices->devices[devices->count++];
        kv_pci_dev = calloc(sizeof(struct kv_pci_device), 1);
        if (kv_pci_dev == NULL) {
            kv_print_err("Error: calloc kv_pci_dev failed\n");
            return -ENOMEM;
        }

        command = pci_read_word(pci_dev, PCI_COMMAND);
        if (!(command & PCI_COMMAND_MEMORY)) {
            kv_print_dbg("Command memory is disabled: 0x%x\n", command);
            /* Enable command memory */
            pci_write_word(pci_dev, PCI_COMMAND, command | PCI_COMMAND_MEMORY);
        }

        kv_libpci_dev = &kv_pci_dev->libpci;
        kvaser_pciefd_dev = &kv_pci_dev->drv_dev;
        memset(kvaser_pciefd_dev, 0, sizeof(struct kvaser_pciefd));
        kvaser_pciefd_dev->driver_data = driver_data;
        strncpy(device->post_commit_str, driver_data->post_commit_str,
                sizeof(device->post_commit_str));
        device->post_finish = driver_data->post_finish;

        device->index = devices->count - 1;
        kv_libpci_dev->vendor_id = pci_dev->vendor_id;
        kv_libpci_dev->device_id = pci_dev->device_id;
        kv_libpci_dev->base_addr = pci_dev->base_addr[0] & PCI_ADDR_MEM_MASK;
        if ((pci_dev->known_fields & PCI_FILL_SIZES) == 0) {
            kv_print_err("Warning: Cannot determine the region size\n"
                         "         Using default 0x%x.\n",
                         KVASER_PCIEFD_PCI_REGION_SIZE);
            kv_libpci_dev->size = KVASER_PCIEFD_PCI_REGION_SIZE;
        } else {
            kv_libpci_dev->size = pci_dev->size[0];
        }
        strncpy(kv_libpci_dev->sysfs_path, sysfs_base_path, sizeof(kv_libpci_dev->sysfs_path));
        device->lib_data = kv_pci_dev;

        kv_print_dbg("\tIndex: %u\n", device->index);
        kv_print_dbg("\tsysfs: %s\n", kv_libpci_dev->sysfs_path);
        kv_print_dbg("\tID:    %04x:%04x\n", kv_libpci_dev->vendor_id, kv_libpci_dev->device_id);
        kv_print_dbg("\tBase:  %04lx\n", kv_libpci_dev->base_addr);
        kv_print_dbg("\tSize:  0x%lx\n", kv_libpci_dev->size);
        kv_print_dbg("\n");
    }

    pci_cleanup(pacc);
    return 0;
}

static int kvaser_pciefd_set_device_name(struct kvaser_device *device)
{
    unsigned int len = ARRAY_SIZE(kvaser_pciefd_dev_name_list);
    unsigned int i;

    assert(device);
    if (!device) {
        return 1;
    }

    for (i = 0; i < len; i++) {
        if ((device->ean[0] == kvaser_pciefd_dev_name_list[i].ean[0]) &&
            (device->ean[1] == kvaser_pciefd_dev_name_list[i].ean[1])) {
            strncpy(device->device_name, kvaser_pciefd_dev_name_list[i].name,
                    sizeof(device->device_name));
            device->device_name[sizeof(device->device_name) - 1] = '\0';
            break;
        }
    }
    if (i == len) {
        /* No match found */
        return 1;
    }

    return 0;
}

static int kvaser_pciefd_probe(struct kvaser_device *device)
{
    struct kv_pci_device *kv_pci_dev;
    struct kvaser_pciefd *kvaser_pciefd_dev;
    struct kvaser_libpci *kv_libpci_dev;
    int ret;

    assert(device);
    kv_pci_dev = device->lib_data;
    assert(kv_pci_dev);

    kv_libpci_dev = &kv_pci_dev->libpci;
    kvaser_pciefd_dev = &kv_pci_dev->drv_dev;

    if (kvaser_pciefd_dev->reg_base == NULL) {
        return 1;
    }
    ret = kvaser_pciefd_setup_board(kvaser_pciefd_dev);

    device->ean[0] = kvaser_pciefd_dev->ean[0];
    device->ean[1] = kvaser_pciefd_dev->ean[1];
    device->fw[0] = kvaser_pciefd_dev->fw[0];
    device->fw[1] = kvaser_pciefd_dev->fw[1];
    device->serial = kvaser_pciefd_dev->serial;
    strncpy(device->driver_name, KVASER_PCIEFD_DRV_NAME, sizeof(device->driver_name));
    if (kvaser_pciefd_set_device_name(device)) {
        kv_print_err("Error: Unknown device EAN: " KV_FLASH_EAN_FRMT_STR "\n", device->ean[1] >> 12,
                     ((device->ean[1] & 0xfff) << 8) | (device->ean[0] >> 24),
                     device->ean[0] >> 4 & 0xfffff, device->ean[0] & 0xf);
        strncpy(device->device_name, "Unknown device", sizeof(device->device_name));
    }
    snprintf(device->info_str, sizeof(device->info_str),
             "\tsysfs   %s\n"
             "\tAddr    0x%04lx\n",
             kv_libpci_dev->sysfs_path, kv_libpci_dev->base_addr);

    return ret;
}

static int kvaser_pciefd_open(struct kvaser_device *device)
{
    struct kv_pci_device *kv_pci_dev;
    struct kvaser_pciefd *kvaser_pciefd_dev;
    struct kvaser_libpci *kv_libpci_dev;
    char *sysfs_base_path;
    char sysfs_resource_path[256];
    int fd;
    void *base;
    int ret = 0;

    assert(device);
    kv_pci_dev = device->lib_data;
    assert(kv_pci_dev);

    kv_libpci_dev = &kv_pci_dev->libpci;

    sysfs_base_path = kv_libpci_dev->sysfs_path;
    /* Now we want to open resource0 */
    snprintf(sysfs_resource_path, sizeof(sysfs_resource_path), "%s/resource0", sysfs_base_path);
    fd = open(sysfs_resource_path, O_RDWR | O_SYNC);
    if (fd < 0) {
        kv_print_err("Error: open() failed for \"%s\": %m\n", sysfs_resource_path);
        if (errno == EACCES) {
            kv_print_err("Make sure you are running as root!\n");
        }
        return 1;
    }

    base = mmap((void *)kv_libpci_dev->base_addr, kv_libpci_dev->size, PROT_READ | PROT_WRITE,
                MAP_SHARED, fd, 0);

    if (base == MAP_FAILED || base == NULL) {
        kv_print_err("Error: mmap() failed for \"%s\": %m\n"
                     "       Make sure there is no driver using the device\n",
                     sysfs_resource_path);
        close(fd);
        return 1;
    }

    kvaser_pciefd_dev = &kv_pci_dev->drv_dev;
    kvaser_pciefd_dev->reg_base = base;
    kvaser_pciefd_dev->fd = fd;

    // Initialize SPI
    ret = SPI_FLASH_init(&kvaser_pciefd_dev->hflash.spif,
                         kvaser_pciefd_dev->driver_data->spi_ops,
                         kvaser_pciefd_dev->reg_base + kvaser_pciefd_dev->driver_data->offset_spi);
    if ( ret != SPI_FLASH_STATUS_SUCCESS) {
        kv_print_info("SPI flash init failed.\n");
        goto error_exit;
    }

    ret = SPI_FLASH_start(&kvaser_pciefd_dev->hflash.spif);
    if ( ret != SPI_FLASH_STATUS_SUCCESS) {
        kv_print_info("SPI flash start failed.\n");
        goto spi_deinit;
    }

    if (!SPI_FLASH_verify_jedec(&kvaser_pciefd_dev->hflash.spif)) {
        ret = -1;
        goto spi_stop;
    }

    ret = HYDRA_FLASH_init(&kvaser_pciefd_dev->hflash,
                           kvaser_pciefd_dev->driver_data->flash_meta,
                           kvaser_pciefd_dev->driver_data->hydra_flash_ops,
                           kvaser_pciefd_dev);
    if (ret != HYDRA_FLASH_STAT_OK) {
        goto spi_stop;
    }

    return 0;

spi_stop:
    SPI_FLASH_stop(&kvaser_pciefd_dev->hflash.spif);
spi_deinit:
    SPI_FLASH_deinit(&kvaser_pciefd_dev->hflash.spif);
error_exit:
    return ret;
}

static int kvaser_pciefd_close(struct kvaser_device *device)
{
    struct kv_pci_device *kv_pci_dev;
    struct kvaser_pciefd *kvaser_pciefd_dev;
    struct kvaser_libpci *kv_libpci_dev;
    int ret = -1;

    assert(device);
    kv_pci_dev = device->lib_data;
    assert(kv_pci_dev);

    kv_libpci_dev = &kv_pci_dev->libpci;
    kvaser_pciefd_dev = &kv_pci_dev->drv_dev;

    if (kvaser_pciefd_dev->reg_base == NULL) {
        return 1;
    }

    SPI_FLASH_stop(&kvaser_pciefd_dev->hflash.spif);
    HYDRA_FLASH_deinit(&kvaser_pciefd_dev->hflash);
    ret = munmap((void *)kv_libpci_dev->base_addr, kv_libpci_dev->size);
    close(kvaser_pciefd_dev->fd);
    kvaser_pciefd_dev->fd = -1;
    kvaser_pciefd_dev->reg_base = NULL;
    return ret;
}

static int kvaser_pciefd_probe_devices(struct kvaser_devices *devices)
{
    int i;
    int ret = 0;

    assert(devices);
    for (i = 0; i < devices->count; i++) {
        struct kvaser_device *device = &devices->devices[i];

        if (kvaser_pciefd_open(device)) {
            kv_print_err("Error: probe_devices(): open failed\n");
            ret = 1;
            break;
        }
        if (kvaser_pciefd_probe(device)) {
            kv_print_err("Error: probe_devices(): probe failed\n");
            ret = 1;
            break;
        }
        if (kvaser_pciefd_close(device)) {
            kv_print_err("Error: probe_devices(): close failed\n");
            ret = 1;
            break;
        }
    }

    return ret;
}

/*  kv_flash.h API
 * ======================================================================
 */
int kvaser_fwu_flash_prog(struct kvaser_device *device, KCAN_FLASH_PROG *fp)
{
    struct kv_pci_device *kv_pci_dev;
    struct kvaser_pciefd *kvaser_pciefd_dev;

    assert(device);
    assert(fp);
    kv_pci_dev = device->lib_data;
    kvaser_pciefd_dev = &kv_pci_dev->drv_dev;

    return HYDRA_FLASH_update_fw_ioctl(&kvaser_pciefd_dev->hflash, fp);
}

int kvaser_fwu_deinit_lib(struct kvaser_devices *devices)
{
    if (devices) {
        int i;

        for (i = 0; i < devices->count; i++) {
            struct kvaser_device *device = &devices->devices[i];

            if (device->lib_data) {
                free(device->lib_data);
                device->lib_data = NULL;
            }
        }
        devices->count = 0;
    }

    return 0;
}

int kvaser_fwu_init_lib(struct kvaser_devices *devices)
{
    int ret = 0;

    if (devices == NULL) {
        kv_print_err("Error: kvaser_fwu_init_lib(): devices is NULL\n");
        return 1;
    }

    ret = kvaser_pciefd_scan(devices);
    if (ret) {
        kvaser_fwu_deinit_lib(devices);
        kv_print_err("Error: kvaser_pciefd_scan() failed: %d\n", ret);
        return ret;
    }

    ret = kvaser_pciefd_probe_devices(devices);
    if (ret) {
        kv_print_err("Error: kvaser_pciefd_probe_devices() failed: %d\n", ret);
        return ret;
    }

    return ret;
}

int kvaser_fwu_open(struct kvaser_device *device)
{
    struct kv_pci_device *kv_pci_dev;
    struct kvaser_pciefd *kvaser_pciefd_dev;
    int ret;

    assert(device);
    ret = kvaser_pciefd_open(device);
    if (ret != 0) {
        kv_print_err("Error: %s Could not open device: %d\n", __func__, ret);
        return ret;
    }

    // Re-read parameters which are reset by kvaser_pciefd_open
    // kvaser_pciefd_dev->hflash.params.ean must be set in order to flash device later!
    kv_pci_dev = device->lib_data;
    assert(kv_pci_dev);
    kvaser_pciefd_dev = &kv_pci_dev->drv_dev;
    assert(kvaser_pciefd_dev);
    ret = kvaser_pciefd_setup_board(kvaser_pciefd_dev);
    if (ret != 0) {
        kv_print_err("Error: %s Could read device parameters: %d\n", __func__, ret);
        return ret;
    }

    return ret;
}

int kvaser_fwu_close(struct kvaser_device *device)
{
    assert(device);

    return kvaser_pciefd_close(device);
}

