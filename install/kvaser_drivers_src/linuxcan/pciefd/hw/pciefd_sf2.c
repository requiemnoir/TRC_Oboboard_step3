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
 * Kvaser Smartfusion2 hardware layer
 */

#include <linux/interrupt.h>
#include <linux/delay.h>
#include "debugprint.h"
#include "kcan_int.h"
#include "kcan_led.h"
#include "spi_flash.h"
#include "VCanOsIf.h"
#include "pciefd_hwif.h"
#include "flash_meta_sf2.h"
#include "pciefd_sf2.h"

#if !(PCIEFD_USE_DMA)
#error DMA is required (PCIEFD_USE_DMA=1), FIFO mode does not work on Smartfusion2 devices!
#endif /* PCIEFD_USE_DMA */

#define SF2_GPIO_PIN_TRIG_UPDATE 0

/* clang-format off */
#define ALL_KCAN_INTERRUPTS \
    (KCAN_INT_IRQ_TX(0)     \
    | KCAN_INT_IRQ_TX(1)    \
    | KCAN_INT_IRQ_TX(2)    \
    | KCAN_INT_IRQ_TX(3)    \
    | KCAN_INT_IRQ_RX0)
/* clang-format on */

// Main block offsets (bytes)
#define OFFSET_TECH 0x00000000
#define OFFSET_KCAN 0x00100000
#define OFFSET_MISC 0x00200000

// Smartfusion2 peripherals
#define SF2_SPI0_BASE    0x00001000u
#define SF2_GPIO_BASE    0x00013000u
#define SF2_SERDES0_BASE 0x00028000u
#define OFFSET_SF2_AHB   OFFSET_TECH

#define OFFSET_SPI     (OFFSET_SF2_AHB + SF2_SPI0_BASE)
#define OFFSET_GPIO    (OFFSET_SF2_AHB + SF2_GPIO_BASE)
#define OFFSET_SERDES0 (OFFSET_SF2_AHB + SF2_SERDES0_BASE)

// KCAN sub block offsets (bytes)
#define OFFSET_KCAN_META (OFFSET_KCAN + 0x00000)
#define OFFSET_KCAN_TIME (OFFSET_KCAN + 0x01000)
#define OFFSET_KCAN_INTR (OFFSET_KCAN + 0x02000)
#define OFFSET_KCAN_RX_DATA \
    (OFFSET_KCAN + 0x20000 + 0x4) // +0x4 workaround for broken SERDES 64-bit to 32-bit support
#define OFFSET_KCAN_RX_CTRL (OFFSET_KCAN + 0x21000)
#define OFFSET_KCAN_TX0     (OFFSET_KCAN + 0x40000)
#define OFFSET_KCAN_TX1     (OFFSET_KCAN + 0x42000)
#define OFFSET_KCAN_TX2     (OFFSET_KCAN + 0x44000)
#define OFFSET_KCAN_TX3     (OFFSET_KCAN + 0x46000)
#define CAN_CONTROLLER_SPAN (OFFSET_KCAN_TX1 - OFFSET_KCAN_TX0)

const struct pciefd_irq_defines SF2_IRQ_DEFINES = {
    .rx0 = KCAN_INT_IRQ_RX0,
    .tx = { KCAN_INT_IRQ_TX(0), KCAN_INT_IRQ_TX(1), KCAN_INT_IRQ_TX(2), KCAN_INT_IRQ_TX(3) },
    .all_irq = ALL_KCAN_INTERRUPTS,
};

static void sf2_pci_irq_set_mask(VCanCardData *vCard, u32 mask)
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

static void sf2_pci_irq_set_mask_bits(VCanCardData *vCard, u32 bits)
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

static void sf2_pci_irq_clear_mask_bits(VCanCardData *vCard, u32 bits)
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

static u32 sf2_pci_irq_get(PciCanCardData *hCard)
{
    if (unlikely(hCard->ongoing_firmware_upgrade)) {
        return 0;
    } else {
        return KCAN_INT_get_irq(hCard->io.kcan.kcanIntBase);
    }
}

#define SF2_SERDES_PCIE_AXI_SLAVE_WINDOWx_2(x) (OFFSET_SERDES0 + 0x10 * (x) + 0x0c8)
#define SF2_SERDES_PCIE_AXI_SLAVE_WINDOWx_3(x) (OFFSET_SERDES0 + 0x10 * (x) + 0x0cc)
static int sf2_setup_dma_address_translation(PciCanCardData *hCard, int pos,
                                             dmaCtxBuffer_t *bufferCtx)
{
    u32 lsb;
    u32 msb;

    lsb = (u32)(bufferCtx->address & 0xfffff000);
#ifdef KV_PCIEFD_DMA_64BIT
    DEBUGPRINT(3, "KV_PCIEFD_DMA_64BIT\n");
    if (hCard->useDmaAddr64) {
        DEBUGPRINT(3, "usedmaaddr64 = 1\n");
        msb = (u32)(bufferCtx->address >> 32);
    } else {
#endif /* KV_PCIEFD_DMA_64BIT */
        DEBUGPRINT(3, "usedmaaddr64 = 0\n");
        msb = 0x0;
#ifdef KV_PCIEFD_DMA_64BIT
    }
#endif /* KV_PCIEFD_DMA_64BIT */
    DEBUGPRINT(4, "LSB = 0x%08x\tMSB = 0x%08x\n", lsb, msb);
    iowrite32(lsb, SUM(hCard->io.pcieBar0Base, SF2_SERDES_PCIE_AXI_SLAVE_WINDOWx_2(pos)));
    iowrite32(msb, SUM(hCard->io.pcieBar0Base, SF2_SERDES_PCIE_AXI_SLAVE_WINDOWx_3(pos)));

    return VCAN_STAT_OK;
}

#define SF2_GPIO_OUT (OFFSET_GPIO + 0x0088)
static int sf2_firmware_upgrade_trigger_update(void *data)
{
    VCanCardData *vCard = data;
    PciCanCardData *hCard = vCard->hwCardData;
    u32 gpio_out;
    int i;

    vCard->card_flags |= DEVHND_CARD_REFUSE_TO_USE_CAN;
    sf2_pci_irq_set_mask(vCard, 0U);
    hCard->ongoing_firmware_upgrade = true;
    gpio_out = ioread32(SUM(hCard->io.pcieBar0Base, SF2_GPIO_OUT));
    gpio_out |= BIT(SF2_GPIO_PIN_TRIG_UPDATE);
    iowrite32(gpio_out, SUM(hCard->io.pcieBar0Base, SF2_GPIO_OUT));
    DEBUGPRINT(2, "Update triggered by setting GPIO pin %u.\n", SF2_GPIO_PIN_TRIG_UPDATE);
    DEBUGPRINT(2, "Please allow one minute for the card to reprogram and restart itself.\n");
    /* Wait 50 seconds for the Smartfusion2 SoC to re-program and restart itself */
    for (i = 0; i < 10; i++) {
        /* Avoid long sleep to prevent "soft lockup" kernel warning */
        msleep(5 * 1000);
    }

    return FIRMWARE_STATUS_OK;
}

static void display_update_state_sf2(void *data, bool on)
{
    VCanCardData *vCard = data;
    int i;

    for (i = 0; i < vCard->nrChannels; i++) {
        PciCanChanData *hChd = vCard->chanData[i]->hwChanData;

        KCAN_LED_set(hChd->canControllerBase, on);
    }
}

const struct pciefd_card_ops SF2_CARD_OPS = {
    .setup_dma_address_translation = &sf2_setup_dma_address_translation,
    .pci_irq_get = &sf2_pci_irq_get,
    .pci_irq_set_mask = &sf2_pci_irq_set_mask,
    .pci_irq_set_mask_bits = &sf2_pci_irq_set_mask_bits,
    .pci_irq_clear_mask_bits = &sf2_pci_irq_clear_mask_bits,
};

const struct hydra_flash_device_ops hydra_flash_device_ops_sf2 = {
    .firmware_upgrade_trigger_update = &sf2_firmware_upgrade_trigger_update,
    .display_update_state = &display_update_state_sf2,
};

const struct pciefd_driver_data PCIEFD_DRIVER_DATA_SF2 = {
    .ops = &SF2_CARD_OPS,
    .spi_ops = &SPI_FLASH_sf2_ops,
    .hydra_flash_ops = &hydra_flash_device_ops_sf2,
    .irq_def = &SF2_IRQ_DEFINES,
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
