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
/**
 * Kvaser Xilinx hardware layer
 */

#include "xspi.h"
#include "debugprint.h"
#include "kcan_int.h"
#include "kcan_led.h"
#include "spi_flash.h"
#include "pciefd_hwif.h"
#include "flash_meta_xilinx.h"
#include "pciefd_xilinx.h"

/* clang-format off */
#define ALL_KCAN_INTERRUPTS \
    ( KCAN_INT_IRQ_SPI0     \
    | KCAN_INT_IRQ_RX0      \
    | KCAN_INT_IRQ_TX(0)    \
    | KCAN_INT_IRQ_TX(1)    \
    | KCAN_INT_IRQ_TX(2)    \
    | KCAN_INT_IRQ_TX(3)    \
    | KCAN_INT_IRQ_TX(4)    \
    | KCAN_INT_IRQ_TX(5)    \
    | KCAN_INT_IRQ_TX(6)    \
    | KCAN_INT_IRQ_TX(7)    \
    )
/* clang-format on */

// Main block offsets (bytes)
#define OFFSET_TECH 0x00000000
#define OFFSET_KCAN 0x00100000
#define OFFSET_MISC 0x00300000

// KCAN sub block offsets (bytes)
#define OFFSET_KCAN_META    (OFFSET_KCAN + 0x00000)
#define OFFSET_KCAN_TIME    (OFFSET_KCAN + 0x01000)
#define OFFSET_KCAN_INTR    (OFFSET_KCAN + 0x02000)
#define OFFSET_KCAN_RX_DATA (OFFSET_KCAN + 0x20000)
#define OFFSET_KCAN_RX_CTRL (OFFSET_KCAN + 0x21000)
#define OFFSET_KCAN_TX0     (OFFSET_KCAN + 0x40000)
#define OFFSET_KCAN_TX1     (OFFSET_KCAN + 0x42000)
#define OFFSET_KCAN_TX2     (OFFSET_KCAN + 0x44000)
#define OFFSET_KCAN_TX3     (OFFSET_KCAN + 0x46000)
#define OFFSET_KCAN_TX4     (OFFSET_KCAN + 0x48000)
#define OFFSET_KCAN_TX5     (OFFSET_KCAN + 0x4A000)
#define OFFSET_KCAN_TX6     (OFFSET_KCAN + 0x4C000)
#define OFFSET_KCAN_TX7     (OFFSET_KCAN + 0x4E000)
#define CAN_CONTROLLER_SPAN (OFFSET_KCAN_TX1 - OFFSET_KCAN_TX0)

// Xilinx peripherals
#define OFFSET_SPI (OFFSET_TECH + 0x1000)

const struct pciefd_irq_defines XILINX_IRQ_DEFINES = {
    .rx0 = KCAN_INT_IRQ_RX0,
    .tx = { \
    KCAN_INT_IRQ_TX(0), \
    KCAN_INT_IRQ_TX(1), \
    KCAN_INT_IRQ_TX(2), \
    KCAN_INT_IRQ_TX(3), \
    KCAN_INT_IRQ_TX(4), \
    KCAN_INT_IRQ_TX(5), \
    KCAN_INT_IRQ_TX(6), \
    KCAN_INT_IRQ_TX(7) },
    .all_irq = ALL_KCAN_INTERRUPTS,
};

static void xilinx_pci_irq_set_mask(VCanCardData *vCard, u32 mask)
{
    PciCanCardData *hCard = vCard->hwCardData;
    unsigned long irqFlags;

    // The card must be present!
    if (unlikely(!vCard->cardPresent)) {
        DEBUGPRINT(3, "Card not present!\n");
        return;
    }

    spin_lock_irqsave(&hCard->lock, irqFlags);
    DEBUGPRINT(3, "Set interrupt mask:%08x\n", mask);
    KCAN_INT_set_enable_mask(hCard->io.kcan.kcanIntBase, mask);
    spin_unlock_irqrestore(&hCard->lock, irqFlags);
}

static void xilinx_pci_irq_set_mask_bits(VCanCardData *vCard, u32 bits)
{
    PciCanCardData *hCard = vCard->hwCardData;
    unsigned long irqFlags;

    // The card must be present!
    if (unlikely(!vCard->cardPresent)) {
        DEBUGPRINT(3, "Card not present!\n");
        return;
    }

    spin_lock_irqsave(&hCard->lock, irqFlags);
    DEBUGPRINT(3, "Set interrupt bits:%08x\n", bits);
    KCAN_INT_set_enable_mask_bits(hCard->io.kcan.kcanIntBase, bits);
    spin_unlock_irqrestore(&hCard->lock, irqFlags);
}

static void xilinx_pci_irq_clear_mask_bits(VCanCardData *vCard, u32 bits)
{
    PciCanCardData *hCard = vCard->hwCardData;
    unsigned long irqFlags;

    // The card must be present!
    if (unlikely(!vCard->cardPresent)) {
        DEBUGPRINT(3, "Card not present!\n");
        return;
    }

    spin_lock_irqsave(&hCard->lock, irqFlags);
    DEBUGPRINT(3, "Set interrupt bits:%08x\n", bits);
    KCAN_INT_clear_enable_mask_bits(hCard->io.kcan.kcanIntBase, bits);
    spin_unlock_irqrestore(&hCard->lock, irqFlags);
}

static u32 xilinx_pci_irq_get(PciCanCardData *hCard)
{
    if (unlikely(hCard->ongoing_firmware_upgrade)) {
        return 0;
    } else {
        return KCAN_INT_get_irq(hCard->io.kcan.kcanIntBase);
    }
}

static int xilinx_setup_dma_address_translation(PciCanCardData *hCard, int bar,
                                                dmaCtxBuffer_t *buffer_ctx)
{
    const u32 AXI_BASE_ADDR_TRANS_REG_OFFSET = 0x0208;
    u32 bar_offset = AXI_BASE_ADDR_TRANS_REG_OFFSET + bar * 8;
    u32 lsw = (buffer_ctx->address & 0xfffff000);
    u32 msw = (hCard->useDmaAddr64 ? (buffer_ctx->address >> 32) : 0U);
// SWT barfs at this (32-bit) but removing the cast results in a compilation error:
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wint-to-pointer-cast"
    DEBUGPRINT(4, "DMA address %px\n", (void *)buffer_ctx->address);
#pragma GCC diagnostic pop
    DEBUGPRINT(4, "AXI translation regs: LSW = 0x%08x\tMSW = 0x%08x\n", lsw, msw);
    iowrite32(msw, hCard->io.pcieBar0Base + bar_offset);
    iowrite32(lsw, hCard->io.pcieBar0Base + bar_offset + 4);
    return VCAN_STAT_OK;
}

static void display_update_state_xilinx(void *data, bool on)
{
    VCanCardData *vCard = data;
    int i;

    for (i = 0; i < vCard->nrChannels; i++) {
        PciCanChanData *hChd = vCard->chanData[i]->hwChanData;

        KCAN_LED_set(hChd->canControllerBase, on);
    }
}

const struct pciefd_card_ops XILINX_CARD_OPS = {
    .setup_dma_address_translation = &xilinx_setup_dma_address_translation,
    .pci_irq_get = &xilinx_pci_irq_get,
    .pci_irq_set_mask = &xilinx_pci_irq_set_mask,
    .pci_irq_set_mask_bits = &xilinx_pci_irq_set_mask_bits,
    .pci_irq_clear_mask_bits = &xilinx_pci_irq_clear_mask_bits,
};

const struct hydra_flash_device_ops hydra_flash_device_ops_xilinx = {
    .firmware_upgrade_trigger_update = NULL,
    .display_update_state = &display_update_state_xilinx,
};

const struct pciefd_driver_data PCIEFD_DRIVER_DATA_XILINX = {
    .ops = &XILINX_CARD_OPS,
    .spi_ops = &SPI_FLASH_xilinx_ops,
    .hydra_flash_ops = &hydra_flash_device_ops_xilinx,
    .irq_def = &XILINX_IRQ_DEFINES,
    .offsets = {
        .tech = {
            .spi = OFFSET_SPI,
        },
        .kcan = {
            .meta = OFFSET_KCAN_META,
            .time = OFFSET_KCAN_TIME,
            .interrupt = OFFSET_KCAN_INTR,
            .rx_data = OFFSET_KCAN_RX_DATA,
            .rx_ctrl = OFFSET_KCAN_RX_CTRL,
            .tx0 = OFFSET_KCAN_TX0,
            .tx1 = OFFSET_KCAN_TX1,
            .tx2 = OFFSET_KCAN_TX2,
            .tx3 = OFFSET_KCAN_TX3,
            .tx4 = OFFSET_KCAN_TX4,
            .tx5 = OFFSET_KCAN_TX5,
            .tx6 = OFFSET_KCAN_TX6,
            .tx7 = OFFSET_KCAN_TX7,
            .controller_span = CAN_CONTROLLER_SPAN,
        },
    },
    .hw_const = {
        .supported_fpga_major = 1,
        .flash_meta.size = FLASH_SIZE,
        .flash_meta.param_image_size_max = FLASH_PARAM_IMAGE_SIZE_MAX,
        .flash_meta.param_image_offset = FLASH_PARAM_IMAGE_OFFSET,
        .flash_meta.fpga_image_size_max = FLASH_FPGA_IMAGE_SIZE_MAX,
        .flash_meta.fpga_image_offset = FLASH_FPGA_IMAGE_OFFSET,
    },
};

const struct pciefd_driver_data PCIEFD_DRIVER_DATA_XILINX_NO_SPI = {
    .ops = &XILINX_CARD_OPS,
    .spi_ops = &SPI_FLASH_dummy_ops,
    .irq_def = &XILINX_IRQ_DEFINES,
    .offsets = {
        .tech = {
            .spi = OFFSET_SPI,
        },
        .kcan = {
            .meta = OFFSET_KCAN_META,
            .time = OFFSET_KCAN_TIME,
            .interrupt = OFFSET_KCAN_INTR,
            .rx_data = OFFSET_KCAN_RX_DATA,
            .rx_ctrl = OFFSET_KCAN_RX_CTRL,
            .tx0 = OFFSET_KCAN_TX0,
            .tx1 = OFFSET_KCAN_TX1,
            .tx2 = OFFSET_KCAN_TX2,
            .tx3 = OFFSET_KCAN_TX3,
            .tx4 = OFFSET_KCAN_TX4,
            .tx5 = OFFSET_KCAN_TX5,
            .tx6 = OFFSET_KCAN_TX6,
            .tx7 = OFFSET_KCAN_TX7,
            .controller_span = CAN_CONTROLLER_SPAN,
        },
    },
    .hw_const = {
        .supported_fpga_major = 1,
        .flash_meta.size = 0U,
        .flash_meta.fpga_image_offset = 0U,
        .flash_meta.param_image_size_max = 0U,
        .flash_meta.param_image_offset = 0U,
        .flash_meta.fpga_image_size_max = 0U,
    },
};
