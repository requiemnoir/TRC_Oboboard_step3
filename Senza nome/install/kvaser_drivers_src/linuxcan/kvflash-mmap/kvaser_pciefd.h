/*
**             Copyright 2023 by Kvaser AB, Molndal, Sweden
**                         http://www.kvaser.com
**
** This software is dual licensed under the following two licenses:
** BSD-new and GPLv2. You may use either one. See the included
** COPYING file for details.
**
** License: BSD-new
** ==============================================================================
** Redistribution and use in source and binary forms, with or without
** modification, are permitted provided that the following conditions are met:
**     * Redistributions of source code must retain the above copyright
**       notice, this list of conditions and the following disclaimer.
**     * Redistributions in binary form must reproduce the above copyright
**       notice, this list of conditions and the following disclaimer in the
**       documentation and/or other materials provided with the distribution.
**     * Neither the name of the <organization> nor the
**       names of its contributors may be used to endorse or promote products
**       derived from this software without specific prior written permission.
**
** THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
** AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
** IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
** ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
** LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
** CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
** SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR
** BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER
** IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
** ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
** POSSIBILITY OF SUCH DAMAGE.
**
**
** License: GPLv2
** ==============================================================================
** This program is free software; you can redistribute it and/or modify
** it under the terms of the GNU General Public License as published by
** the Free Software Foundation; either version 2 of the License, or
** (at your option) any later version.
**
** This program is distributed in the hope that it will be useful,
** but WITHOUT ANY WARRANTY; without even the implied warranty of
** MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
** GNU General Public License for more details.
**
** You should have received a copy of the GNU General Public License
** along with this program; if not, write to the Free Software
** Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA
**
**
** IMPORTANT NOTICE:
** ==============================================================================
** This source code is made available for free, as an open license, by Kvaser AB,
** for use with its applications. Kvaser AB does not accept any liability
** whatsoever for any third party patent or other immaterial property rights
** violations that may result from any usage of this source code, regardless of
** the combination of source code and various applications that it can be used
** in, or with.
**
** -----------------------------------------------------------------------------
*/

#ifndef _KVASER_PCIEFD_H
#define _KVASER_PCIEFD_H

#include "hydra_flash.h"
#include "kv_flash.h"
#include "spi_flash.h"

#include <linux/types.h>
#include <pci/pci.h>
#include <stdbool.h>

struct kvaser_pciefd {
    int fd;
    void *reg_base;
    u32 ean[2];
    u32 fw[2];
    u32 serial;
    u8 *flash_img;
    u8 nr_channels;
    const struct pciefd_driver_data *driver_data;
    struct hydra_flash_ctx hflash;
};

struct kvaser_libpci {
    u16 vendor_id;
    u16 device_id;
    pciaddr_t base_addr;
    pciaddr_t size; /* Region size */
    char sysfs_path[128];
};

struct kv_pci_device {
    struct kvaser_libpci libpci;
    struct kvaser_pciefd drv_dev;
};

/**
 * struct pciefd_driver_data         Collection of functions and constants that are hardware specific
 *
 * ops:                              Hardware specific driver functions
 * spi_ops:                          SPI flash hardware specific functions
 * irq_def:                          PCI interrupt defines
 * offsets:                          Address offsets
 * hw_const:                         Hardware specific constants
 */
struct pciefd_driver_data {
    const u32 offset_meta;
    const u32 offset_spi;
    const struct hydra_flash_image_def *flash_meta;
    const struct SPI_FLASH_ops *spi_ops;
    const struct hydra_flash_device_ops *hydra_flash_ops;
    const char post_commit_str[256]; /* Messages printed after FIRMWARE_DOWNLOAD_COMMIT command */
    int (*post_finish)(struct kvaser_device *device);
};

#endif /* _KVASER_PCIEFD_H */
