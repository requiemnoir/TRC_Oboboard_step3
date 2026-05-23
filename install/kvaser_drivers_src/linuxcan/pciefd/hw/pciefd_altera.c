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
 * Kvaser Altera hardware layer
 */

#include "debugprint.h"
#include "spi_flash.h"
#include "kcan_led.h"
#include "altera_int.h"
#include "pciefd_hwif.h"
#include "flash_meta_altera.h"
#include "pciefd_altera.h"

/* clang-format off */
#define ALL_ALTERA_PCI_INTERRUPTS \
    (ALTERA_PCI_IRQ_CAN_TX(0)     \
    | ALTERA_PCI_IRQ_CAN_TX(1)    \
    | ALTERA_PCI_IRQ_CAN_TX(2)    \
    | ALTERA_PCI_IRQ_CAN_TX(3)    \
    | ALTERA_PCI_IRQ_CAN_RX0)
/* clang-format on */

// Main block offsets (bytes)
#define OFFSET_TECH 0x00000000
#define OFFSET_KCAN 0x00010000
#define OFFSET_MISC 0x0001f800

// KCAN sub block offsets (bytes)
#define OFFSET_KCAN_META (OFFSET_KCAN + 0x0f020)
#define OFFSET_KCAN_TIME (OFFSET_KCAN + 0x0f040)

#define OFFSET_KCAN_RX_DATA (OFFSET_KCAN + 0x0f200)
#define OFFSET_KCAN_RX_CTRL (OFFSET_KCAN + 0x0f400)
#define OFFSET_KCAN_TX0     (OFFSET_KCAN + 0x00000)
#define OFFSET_KCAN_TX1     (OFFSET_KCAN + 0x01000)
#define OFFSET_KCAN_TX2     (OFFSET_KCAN + 0x02000)
#define OFFSET_KCAN_TX3     (OFFSET_KCAN + 0x03000)
#define CAN_CONTROLLER_SPAN (OFFSET_KCAN_TX1 - OFFSET_KCAN_TX0)

#define OFFSET_PCI_IRQ   (OFFSET_TECH + 0x00040)
#define OFFSET_SPI_FLASH (OFFSET_MISC)

// Avalon Address Translation Table Address Space Bit Encodings
enum {
    AV_ATT_ASBE_MS32 = 0, // 32-bit address (bits 63:32 are ignored)
    AV_ATT_ASBE_MS64 = 1 // 64 bit address
};

/* clang-format off */
const struct pciefd_irq_defines ALTERA_IRQ_DEFINES = {
    .rx0 = ALTERA_PCI_IRQ_CAN_RX0,
    .tx = {
        ALTERA_PCI_IRQ_CAN_TX(0),
        ALTERA_PCI_IRQ_CAN_TX(1),
        ALTERA_PCI_IRQ_CAN_TX(2),
        ALTERA_PCI_IRQ_CAN_TX(3)
    },
    .all_irq = ALL_ALTERA_PCI_INTERRUPTS,
};

static void altera_pci_irq_set_mask(VCanCardData *vCard, u32 mask)
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
    ALTERA_INT_set_enable_mask(hCard->io.interruptBase, mask);
    spin_unlock_irqrestore(&hCard->lock, irqFlags);
}

static void altera_pci_irq_set_mask_bits(VCanCardData *vCard, u32 bits)
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
    ALTERA_INT_set_enable_mask_bits(hCard->io.interruptBase, bits);
    spin_unlock_irqrestore(&hCard->lock, irqFlags);
}

static void altera_pci_irq_clear_mask_bits(VCanCardData *vCard, u32 bits)
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
    ALTERA_INT_clear_enable_mask_bits(hCard->io.interruptBase, bits);
    spin_unlock_irqrestore(&hCard->lock, irqFlags);
}

static u32 altera_pci_irq_get(PciCanCardData *hCard)
{
    return ALTERA_INT_get_irq(hCard->io.interruptBase);
}

static int altera_setup_dma_address_translation(PciCanCardData *hCard, int pos, dmaCtxBuffer_t *bufferCtx)
{
  // Every table entry is 64 bits, two table entries are used for each channel.
  unsigned int tabEntryOffset = 0x1000+pos*8;

#ifdef KV_PCIEFD_DMA_64BIT
  DEBUGPRINT(3,"KV_PCIEFD_DMA_64BIT\n");

  if(hCard->useDmaAddr64)
    {
      DEBUGPRINT(3,"useDmaAddr64\n");

      iowrite32( (uint32_t)(bufferCtx->address | AV_ATT_ASBE_MS64),
                 SUM(hCard->io.pcieBar0Base, tabEntryOffset) );

      iowrite32( (uint32_t)(bufferCtx->address >> 32),
                 SUM(hCard->io.pcieBar0Base, tabEntryOffset+4) );
    }
  else
    {
#endif /* KV_PCIEFD_DMA_64BIT */
      iowrite32( (uint32_t)(bufferCtx->address | AV_ATT_ASBE_MS32),
                 SUM(hCard->io.pcieBar0Base, tabEntryOffset) );
      iowrite32(0, SUM(hCard->io.pcieBar0Base, tabEntryOffset+4) );

#ifdef KV_PCIEFD_DMA_64BIT
    }
#endif /* KV_PCIEFD_DMA_64BIT */

  return VCAN_STAT_OK;
}

const struct pciefd_card_ops ALTERA_CARD_OPS = {
    .setup_dma_address_translation = &altera_setup_dma_address_translation,
    .pci_irq_get = &altera_pci_irq_get,
    .pci_irq_set_mask = &altera_pci_irq_set_mask,
    .pci_irq_set_mask_bits = &altera_pci_irq_set_mask_bits,
    .pci_irq_clear_mask_bits = &altera_pci_irq_clear_mask_bits,
};

static void display_update_state_altera(void *data, bool on)
{
    VCanCardData *vCard = data;
    int i;

    for (i = 0; i < vCard->nrChannels; i++) {
        PciCanChanData *hChd = vCard->chanData[i]->hwChanData;

        KCAN_LED_set(hChd->canControllerBase, on);
    }
}

const struct hydra_flash_device_ops hydra_flash_device_ops_altera = {
    .firmware_upgrade_trigger_update = NULL,
    .display_update_state = &display_update_state_altera,
};

const struct pciefd_driver_data PCIEFD_DRIVER_DATA_ALTERA = {
    .ops = &ALTERA_CARD_OPS,
    .spi_ops = &SPI_FLASH_altera_ops,
    .hydra_flash_ops = &hydra_flash_device_ops_altera,
    .irq_def = &ALTERA_IRQ_DEFINES,
    .offsets = {
        .tech = {
            .spi = OFFSET_SPI_FLASH,
            .interrupt = OFFSET_PCI_IRQ,
        },
        .kcan = {
            .meta = OFFSET_KCAN_META,
            .time = OFFSET_KCAN_TIME,
            .interrupt = 0,
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
        .supported_fpga_major = 2,
        .flash_meta.size = FLASH_SIZE,
        .flash_meta.param_image_size_max = FLASH_PARAM_IMAGE_SIZE_MAX,
        .flash_meta.param_image_offset = FLASH_PARAM_IMAGE_OFFSET,
        .flash_meta.fpga_image_size_max = FLASH_FPGA_IMAGE_SIZE_MAX,
        .flash_meta.fpga_image_offset = FLASH_FPGA_IMAGE_OFFSET,
    },
};
