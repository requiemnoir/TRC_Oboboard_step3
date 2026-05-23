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

/* Kvaser CAN driver PCIcan hardware specific parts
** PCIcan definitions
*/

#ifndef _PCIEFD_HWIF_H_
#define _PCIEFD_HWIF_H_

#include <linux/leds.h>
#include "pciefd_config.h"
#include "VCanOsIf.h"
#include "hydra_flash.h"
#include "spi_flash.h"

/*****************************************************************************/
/* defines */
/*****************************************************************************/
// Use this to set alternate implementation.

// Warning:
// Not using DMA is unsupported, and may lead to unexpected behaviour!
#define PCIEFD_USE_DMA 1

#define DEVICE_NAME_STRING  "pciefd"
#define MAX_CARD_CHANNELS   8U
#define MAX_DRIVER_CHANNELS 128

#define MAX_ERROR_COUNT       64 //128
#define ERROR_DISABLE_TIME_MS 200

#define PCIEFD_SRQ_RESP_WAIT_TIME 100

#define BLP_INTERVAL  400000 // About 5Hz
#define BLP_PRESC_MAX 255
#define BLP_PRESC_MIN 1
#define BLP_DIVISOR   (BLP_INTERVAL / 10000) // To get 0.00-100.00%

#define SUM(a, b) ((a) + (b))

#define KCAN_MAX_OUTSTANDING_TX 17

// Force 32-bit DMA addresses
#ifdef KV_PCIEFD_DMA_32BIT
#undef KV_PCIEFD_DMA_64BIT
#else
#ifdef CONFIG_ARCH_DMA_ADDR_T_64BIT
#define KV_PCIEFD_DMA_64BIT 1
#endif /* CONFIG_ARCH_DMA_ADDR_T_64BIT */
#endif /* KV_PCIEFD_DMA_32BIT */

typedef struct {
    uint32_t *data;
    dma_addr_t address;
} dmaCtxBuffer_t;

typedef struct {
    dmaCtxBuffer_t bufferCtx[2];
    int active;
    unsigned int enabled;
} dmaCtx_t;

struct led_triggers {
    struct led_trigger *bus_on;
    struct led_trigger *can_activity;
    struct led_trigger *can_error;
    struct led_trigger *buffer_overrun;
};

/* Channel specific data */
typedef struct PciCanChanData {
    CAN_MSG current_tx_message[KCAN_MAX_OUTSTANDING_TX];

    atomic_t outstanding_tx;

    /* Ports and addresses */
    void __iomem *canControllerBase;

    unsigned long tx_irq_msk;
    struct completion
        busOnCompletion; // Used to make sure that multiple bus on commands in a row is not executed.

    spinlock_t lock;
    struct work_struct txWork;
#ifdef TRY_RT_QUEUE
    struct workqueue_struct *txTaskQ;
#endif

    // Flags set if an overrun has been detected
    int overrun_occured;

    // Bus load
    int load;

    VCanChanData *vChan;
    struct timer_list errorDisableTimer;
    unsigned long bus_load_prescaler;
    struct led_triggers ledtrig;
} PciCanChanData;

typedef struct VCanCardTimerData {
    struct timer_list timer;
    VCanCardData *vCard;
} VCanCardTimerData;

/*  Card specific data */
typedef struct PciCanCardData {
    /* IO addresses */
    struct {
        void __iomem *pcieBar0Base;
        void __iomem *interruptBase;
        void __iomem *spiFlashBase;
        struct {
            void __iomem *metaBase;
            void __iomem *timeBase;
            void __iomem *kcanIntBase;
            void __iomem *rxDataBase;
            void __iomem *rxCtrlBase;
            void __iomem *tx0Base;
            void __iomem *tx1Base;
            void __iomem *tx2Base;
            void __iomem *tx3Base;
        } kcan;
    } io;

    const struct pciefd_driver_data *driver_data;

    u8 max_outstanding_tx;
    unsigned int frequency;
    unsigned int freqToUsDivider;
    int irq;
    struct list_head replyWaitList;
    rwlock_t replyWaitListLock;
    spinlock_t lock;

    struct pci_dev *dev;

    dmaCtx_t dmaCtx;
    int useDmaAddr64;
    int useDma;

    atomic_t status_seq_no;
    cust_channel_name_t cust_channel_name[MAX_CARD_CHANNELS];
    bool ongoing_firmware_upgrade;
    struct hydra_flash_ctx hflash;
} PciCanCardData;

/**
 * struct pciefd_card_ops            Hardware specific driver functions
 *
 * @setup_dma_address_translation:   Setup PCI Slave Window translation for DMA
 * @pci_irq_set:                     Enable/disable all PCI interrupts
 * @pci_irq_get:                     Get active PCI interrupts
 * @firmware_upgrade_trigger_update: Function called to trigger firmware upgrade (optional)
 */
struct pciefd_card_ops {
    int (*setup_dma_address_translation)(PciCanCardData *hCard, int pos, dmaCtxBuffer_t *bufferCtx);
    void (*pci_irq_set_mask)(VCanCardData *vCard, u32 mask);
    void (*pci_irq_set_mask_bits)(VCanCardData *vCard, u32 bits);
    void (*pci_irq_clear_mask_bits)(VCanCardData *vCard, u32 bits);
    u32 (*pci_irq_get)(PciCanCardData *hCard);
};

/**
 * struct pciefd_irq_defines         Hardware specific PCI interrupt bit masks
 *
 * @rx0:                        KCAN receive buffer0 bit mask
 * @tx[MAX_CARD_CHANNELS];      KCAN Tx buffer bit mask, one per channel
 */
struct pciefd_irq_defines {
    u32 rx0;
    u32 tx[MAX_CARD_CHANNELS];
    u32 all_irq;
};

/**
 * struct pciefd_address_offsets     Hardware specific offsets
 *
 * @tech:                            Offsets related to peripherals
 * @kcan:                            Offsets related to KCAN
 */
struct pciefd_address_offsets {
    struct {
        u32 spi;
        u32 interrupt;
    } tech;
    struct {
        u32 meta;
        u32 time;
        u32 interrupt;
        u32 rx_data;
        u32 rx_ctrl;
        u32 tx0;
        u32 tx1;
        u32 tx2;
        u32 tx3;
        u32 tx4;
        u32 tx5;
        u32 tx6;
        u32 tx7;
        u32 controller_span;
    } kcan;
};

/**
 * struct pciefd_driver_data         Collection of functions and constants that are hardware specific
 *
 * ops:                              Hardware specific driver functions
 * spi_ops:                          SPI flash hardware specific functions
 * hydra_flash_ops:                  Hydra flash hardware specific functions
 * irq_def:                          PCI interrupt defines
 * offsets:                          Address offsets
 * hw_const:                         Hardware specific constants
 */
struct pciefd_driver_data {
    const struct pciefd_card_ops *ops;
    const struct SPI_FLASH_ops *spi_ops;
    const struct hydra_flash_device_ops *hydra_flash_ops;
    const struct pciefd_irq_defines *irq_def;
    const struct pciefd_address_offsets offsets;
    const struct {
        u8 supported_fpga_major;
        struct hydra_flash_image_def flash_meta;
    } hw_const;
};
#endif

