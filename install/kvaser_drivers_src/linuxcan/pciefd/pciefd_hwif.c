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

//--------------------------------------------------
// NOTE! module_versioning HAS to be included first
#include "module_versioning.h"
//--------------------------------------------------

// Kvaser CAN driver pciefd hardware specific parts

#include <asm/io.h>
#include <canstat.h>
#include <linux/ctype.h>
#include <linux/version.h>
#include <linux/pci.h>
#include <linux/kernel.h>
#include <linux/init.h>
#include <linux/sched.h>
#include <linux/ptrace.h>
#include <linux/slab.h>
#include <linux/string.h>
#include <linux/timer.h>
#include <linux/spinlock.h>
#include <linux/workqueue.h>
#include <linux/interrupt.h>
#include <linux/delay.h>
#include <linux/moduleparam.h>
#include <linux/firmware.h>
#include <linux/leds.h>
#include <linux/can/error.h>
#if LINUX_VERSION_CODE < KERNEL_VERSION(3, 4, 0)
#include <asm/system.h>
#endif /* KERNEL_VERSION < 3.4.0 */

#include <asm/bitops.h>

#if (LINUX_VERSION_CODE < KERNEL_VERSION(4, 12, 0))
#include <asm/uaccess.h>
#else
#include <linux/uaccess.h>
#endif /* KERNEL_VERSION < 4.12.0 */

#include <asm/delay.h>

// For 64-bit divisions in kernel
#include <asm/div64.h>

// Kvaser definitions
#include "aio.h"
#include "canlib_version.h"
#include "debugprint.h"
#include "VCanOsIf.h"
#include "pciefd_hwif.h"
#include "queue.h"
#include "debug.h"
#include "util.h"
#include "vcan_ioctl.h"
#include "capabilities.h"
#include "pwm_util.h"
#include "kcan_led.h"
#include "fpga_meta.h"
#include "spi_flash.h"
#include "hydra_flash.h"
#include "pciefd_rx_fifo_regs.h"
#include "pciefd_rx_fifo.h"
#include "pciefd_altera.h"
#include "pciefd_sf2.h"
#include "pciefd_xilinx.h"
#include "pciefd.h"
#include "pciefd_packet.h"

#if (LINUX_VERSION_CODE < KERNEL_VERSION(6, 16, 0))
#define timer_container_of from_timer
#endif /* KERNEL_VERSION < 6.16.0 */

#ifdef PCIEFD_DEBUG
// Add debug variables

int debug_level = PCIEFD_DEBUG;
MODULE_PARM_DESC(debug_level, "PCIe CAN debug level");
module_param(debug_level, int, 0644);


#endif /* PCIEFD_DEBUG */

// For measuring delays during debugging
#include <linux/time.h>

#if PCIEFD_USE_DMA
#include <linux/dma-mapping.h>
#endif /* PCIEFD_USE_DMA */

MODULE_LICENSE("Dual BSD/GPL");
MODULE_AUTHOR("KVASER");
MODULE_DESCRIPTION("PCIe CAN module.");
MODULE_VERSION(__stringify(CANLIB_MAJOR_VERSION) "." __stringify(CANLIB_MINOR_VERSION));

#define VENDOR_ID_KVASER 0x1A07

/* Altera */
#define DEVICE_ID_PCIEFD_4HS        0x000d
#define DEVICE_ID_PCIEFD_2HS_V2     0x000e
#define DEVICE_ID_PCIEFD_1HS_V2     0x000f
#define DEVICE_ID_MINIPCIEFD_1HS_V2 0x0010
#define DEVICE_ID_MINIPCIEFD_2HS_V2 0x0011

/* Smartfusion2 */
#define DEVICE_ID_PCIEFD_2CAN_V3     0x0012
#define DEVICE_ID_PCIEFD_1CAN_V3     0x0013
#define DEVICE_ID_PCIEFD_4CAN_V2     0x0014
#define DEVICE_ID_MINIPCIEFD_2CAN_V3 0x0015
#define DEVICE_ID_MINIPCIEFD_1CAN_V3 0x0016

/* Xilinx */
#define DEVICE_ID_M2_4HS 0x0017
#define DEVICE_ID_EDGE 0x0018
#define DEVICE_ID_PCIE_8CAN 0x0019

#define DEVICE_IS_EDGE(d) ((d) == DEVICE_ID_EDGE)

#define CANFD_MAX_PRESCALER_VALUE 2U

#define KCAN_DMA_BUFFER_SZ 4096

#define US_PER_SEC 1000000

static_assert(HYDRA_FLASH_MAX_CARD_CHANNELS == MAX_CARD_CHANNELS);


//======================================================================
// external LED triggers
//======================================================================
#if LED_TRIGGERS
static unsigned long EXT_LED_BLINK_DELAY_ms = 50;
#else
// Remove function calls
#define led_trigger_event(trigger, brightness)
#define led_trigger_blink_oneshot(trigger, delay_on, delay_off, invert)
#endif

//======================================================================
// Bus parameter struct
//======================================================================

typedef struct busParams {
    uint32_t brp;
    uint32_t tseg1;
    uint32_t tseg2;
    uint32_t sjw;
    uint32_t tq;
    uint32_t freq;
} busParams_t;

//======================================================================
// HW function pointers
//======================================================================

static int pciefd_init(void);
static int pciefd_set_busparams(VCanChanData *vChd, VCanBusParams *par);
static int pciefd_get_busparams(VCanChanData *vChd, VCanBusParams *par);
static int pciefd_set_busparams_tq(VCanChanData *vChd, VCanBusParamsTq *par);
static int pciefd_get_busparams_tq(VCanChanData *vChd, VCanBusParamsTq *par);
static int pciefd_get_clock_freq(const VCanChanData *chd, uint32_t *freq_mhz);
static int pciefd_set_output_mode(VCanChanData *vChd, int silent);
static int pciefd_get_output_mode(VCanChanData *vChd, int *silent);
static int pciefd_set_tranceiver_mode(VCanChanData *vChd, int linemode, int resnet);
static int pciefd_req_bus_stats(VCanChanData *vChan);
static int pciefd_update_fw_ioctl(const VCanChanData *vChan, KCAN_FLASH_PROG *fp);
static int pciefd_bus_on(VCanChanData *vChd);
static int pciefd_bus_off(VCanChanData *vChd);
static int pciefd_get_tx_err(VCanChanData *vChd);
static int pciefd_get_rx_err(VCanChanData *vChd);
static int pciefd_is_tx_buffer_empty(VCanChanData *vChd);
static bool pciefd_is_tx_buffer_full(VCanChanData *vChd);
static int pciefd_exit(void);
static int pciefd_get_time(VCanChanData *vChan, uint64_t *time);
static int pciefd_flush_tx_buffer(VCanChanData *vChd);

static int pciefd_req_chipstate(VCanChanData *vChd);
static unsigned long pciefd_get_tx_queue_level(VCanChanData *vChd);
static void pciefd_req_tx(VCanCardData *vCard, VCanChanData *vChd);
static int pciefd_get_cust_ch_name(const VCanChanData *const vChd, unsigned char *const data,
                                   const unsigned int data_size, unsigned int *const status);

static VCanDriverData driverData;

static VCanHWInterface hwIf = {
    .initAllDevices = pciefd_init,
    .setBusParams = pciefd_set_busparams,
    .getBusParams = pciefd_get_busparams,
    .setBusParamsTq = pciefd_set_busparams_tq,
    .getBusParamsTq = pciefd_get_busparams_tq,
    .kvDeviceGetClockFreqMhz = pciefd_get_clock_freq,
    .setOutputMode = pciefd_set_output_mode,
    .getOutputMode = pciefd_get_output_mode,
    .setTranceiverMode = pciefd_set_tranceiver_mode,
    .busOn = pciefd_bus_on,
    .busOff = pciefd_bus_off,
    .txAvailable = pciefd_is_tx_buffer_empty,
    .closeAllDevices = pciefd_exit,
    .getTime = pciefd_get_time,
    .flushSendBuffer = pciefd_flush_tx_buffer,
    .getTxErr = pciefd_get_tx_err,
    .getRxErr = pciefd_get_rx_err,
    .txQLen = pciefd_get_tx_queue_level,
    .requestChipState = pciefd_req_chipstate,
    .requestSend = pciefd_req_tx,
    .getCustChannelName = pciefd_get_cust_ch_name,
    .getCardInfo = vCanGetCardInfo,
    .getCardInfo2 = vCanGetCardInfo2,
    .reqBusStats = pciefd_req_bus_stats,
    .deviceFlashProg = pciefd_update_fw_ioctl,
    .device_name = DEVICE_NAME_STRING,
    .canSetTimestampResolution = true
};

static int pciefd_probe(struct pci_dev *dev, const struct pci_device_id *id);
static void pciefd_remove(struct pci_dev *dev);

static struct pci_device_id pciefd_id_table[] = {
    {
        .vendor = VENDOR_ID_KVASER,
        .device = DEVICE_ID_PCIEFD_4HS,
        .subvendor = PCI_ANY_ID,
        .subdevice = PCI_ANY_ID,
        .driver_data = (kernel_ulong_t)&PCIEFD_DRIVER_DATA_ALTERA,
    },
    {
        .vendor = VENDOR_ID_KVASER,
        .device = DEVICE_ID_PCIEFD_2HS_V2,
        .subvendor = PCI_ANY_ID,
        .subdevice = PCI_ANY_ID,
        .driver_data = (kernel_ulong_t)&PCIEFD_DRIVER_DATA_ALTERA,
    },
    {
        .vendor = VENDOR_ID_KVASER,
        .device = DEVICE_ID_PCIEFD_1HS_V2,
        .subvendor = PCI_ANY_ID,
        .subdevice = PCI_ANY_ID,
        .driver_data = (kernel_ulong_t)&PCIEFD_DRIVER_DATA_ALTERA,
    },
    {
        .vendor = VENDOR_ID_KVASER,
        .device = DEVICE_ID_MINIPCIEFD_1HS_V2,
        .subvendor = PCI_ANY_ID,
        .subdevice = PCI_ANY_ID,
        .driver_data = (kernel_ulong_t)&PCIEFD_DRIVER_DATA_ALTERA,
    },
    {
        .vendor = VENDOR_ID_KVASER,
        .device = DEVICE_ID_MINIPCIEFD_2HS_V2,
        .subvendor = PCI_ANY_ID,
        .subdevice = PCI_ANY_ID,
        .driver_data = (kernel_ulong_t)&PCIEFD_DRIVER_DATA_ALTERA,
    },
#if PCIEFD_USE_DMA
    {
        .vendor = VENDOR_ID_KVASER,
        .device = DEVICE_ID_PCIEFD_2CAN_V3,
        .subvendor = PCI_ANY_ID,
        .subdevice = PCI_ANY_ID,
        .driver_data = (kernel_ulong_t)&PCIEFD_DRIVER_DATA_SF2,
    },
    {
        .vendor = VENDOR_ID_KVASER,
        .device = DEVICE_ID_PCIEFD_1CAN_V3,
        .subvendor = PCI_ANY_ID,
        .subdevice = PCI_ANY_ID,
        .driver_data = (kernel_ulong_t)&PCIEFD_DRIVER_DATA_SF2,
    },
    {
        .vendor = VENDOR_ID_KVASER,
        .device = DEVICE_ID_PCIEFD_4CAN_V2,
        .subvendor = PCI_ANY_ID,
        .subdevice = PCI_ANY_ID,
        .driver_data = (kernel_ulong_t)&PCIEFD_DRIVER_DATA_SF2,
    },
    {
        .vendor = VENDOR_ID_KVASER,
        .device = DEVICE_ID_MINIPCIEFD_2CAN_V3,
        .subvendor = PCI_ANY_ID,
        .subdevice = PCI_ANY_ID,
        .driver_data = (kernel_ulong_t)&PCIEFD_DRIVER_DATA_SF2,
    },
    {
        .vendor = VENDOR_ID_KVASER,
        .device = DEVICE_ID_MINIPCIEFD_1CAN_V3,
        .subvendor = PCI_ANY_ID,
        .subdevice = PCI_ANY_ID,
        .driver_data = (kernel_ulong_t)&PCIEFD_DRIVER_DATA_SF2,
    },
#else
#warning This driver will not support Smartfusion2 devices since they require DMA!
#endif /* PCIEFD_USE_DMA */
    {
        .vendor = VENDOR_ID_KVASER,
        .device = DEVICE_ID_M2_4HS,
        .subvendor = PCI_ANY_ID,
        .subdevice = PCI_ANY_ID,
        .driver_data = (kernel_ulong_t)&PCIEFD_DRIVER_DATA_XILINX,
    },
    {
        .vendor = VENDOR_ID_KVASER,
        .device = DEVICE_ID_EDGE,
        .subvendor = PCI_ANY_ID,
        .subdevice = PCI_ANY_ID,
        .driver_data = (kernel_ulong_t)&PCIEFD_DRIVER_DATA_XILINX_NO_SPI,
    },
    {
        .vendor = VENDOR_ID_KVASER,
        .device = DEVICE_ID_PCIE_8CAN,
        .subvendor = PCI_ANY_ID,
        .subdevice = PCI_ANY_ID,
        .driver_data = (kernel_ulong_t)&PCIEFD_DRIVER_DATA_XILINX,
    },
    {
        0,
    },
};

MODULE_DEVICE_TABLE(pci, pciefd_id_table);

static struct pci_driver pciefd_driver = {
    .name = "kv" DEVICE_NAME_STRING,
    .id_table = pciefd_id_table,
    .probe = pciefd_probe,
    .remove = pciefd_remove,
};

#define BDF_STR_LEN 7
//======================================================================
// Get PCI device BDF as a string (BB:DD.F in hex)
//======================================================================
static void get_bdf(const struct pci_dev *dev, char *bdf)
{
    sprintf(bdf, "%02x:%02x.%x", dev->bus->number, PCI_SLOT(dev->devfn),
            PCI_FUNC(dev->devfn));
}


//======================================================================
// DMA
//======================================================================
#if PCIEFD_USE_DMA

//======================================================================
//
//======================================================================
static int pciefd_dma_init(VCanCardData *vCard)
{
    PciCanCardData *hCard = vCard->hwCardData;
    struct device *dev = &hCard->dev->dev;

#ifdef KV_PCIEFD_DMA_64BIT
    if (!dma_set_mask(dev, DMA_BIT_MASK(64))) {
        hCard->useDmaAddr64 = 1;
        DEBUGPRINT(3, "Use 64-bit dma address mask\n");
    } else
#endif
        if (!dma_set_mask(dev, DMA_BIT_MASK(32))) {
        hCard->useDmaAddr64 = 0;
        DEBUGPRINT(3, "Use 32-bit dma address mask\n");
    } else {
        DEBUGPRINT(1, "No suitable DMA available.\n");
        return VCAN_STAT_NO_RESOURCES;
    }

    return VCAN_STAT_OK;
}

//======================================================================
//
//======================================================================
static int pciefd_map_one_buffer(PciCanCardData *hCard, dmaCtxBuffer_t *bufferCtx)
{
    struct device *dev = &hCard->dev->dev;

    void *buffer;
    dma_addr_t dma_handle;

    buffer = kmalloc(KCAN_DMA_BUFFER_SZ, GFP_KERNEL);

    if (!buffer) {
        DEBUGPRINT(1, "Failed DMA buffer setup\n");
        return VCAN_STAT_NO_MEMORY;
    }

    bufferCtx->data = buffer;

    dma_handle = dma_map_single(dev, buffer, KCAN_DMA_BUFFER_SZ, DMA_FROM_DEVICE);

    if (dma_mapping_error(dev, dma_handle)) {
        /*
     * reduce current DMA mapping usage,
     * delay and try again later or
     * reset driver.
     */
        DEBUGPRINT(1, "Failed DMA mapping\n");
        return VCAN_STAT_FAIL;
    }

    bufferCtx->address = dma_handle;

    return VCAN_STAT_OK;
}

//======================================================================
//
//======================================================================
static int pciefd_setup_dma_mappings(PciCanCardData *hCard)
{
    int i;
    const struct pciefd_card_ops *card_ops = hCard->driver_data->ops;

    for (i = 0; i < 2; i++) {
        int retval;

        retval = pciefd_map_one_buffer(hCard, &(hCard->dmaCtx.bufferCtx[i]));

        if (retval != VCAN_STAT_OK)
            return retval;

        DEBUGPRINT(3, "CAN Receiver DMA Buffer %u Addr:%px : Handle:%x\n", i,
                   hCard->dmaCtx.bufferCtx[i].data,
                   (unsigned int)hCard->dmaCtx.bufferCtx[i].address);

        card_ops->setup_dma_address_translation(hCard, i, &(hCard->dmaCtx.bufferCtx[i]));
    }

    return VCAN_STAT_OK;
}

//======================================================================
//
//======================================================================
static int pciefd_remove_one_buffer(VCanCardData *vCard, dmaCtxBuffer_t *bufferCtx)
{
    PciCanCardData *hCard = vCard->hwCardData;
    struct device *dev = &hCard->dev->dev;

    if (bufferCtx->address) {
        dma_unmap_single(dev, bufferCtx->address, KCAN_DMA_BUFFER_SZ, DMA_FROM_DEVICE);
        bufferCtx->address = 0;
    } else {
        DEBUGPRINT(3, "dmaCtx.address was not initialized\n");
    }

    if (bufferCtx->data) {
        kfree(bufferCtx->data);
        bufferCtx->data = 0;
    } else {
        DEBUGPRINT(3, "dmaCtx.buffer was not initialized\n");
    }

    return VCAN_STAT_OK;
}

//======================================================================
//
//======================================================================
static int pciefd_remove_dma_mappings(VCanCardData *vCard)
{
    PciCanCardData *hCard = vCard->hwCardData;
    int i;

    for (i = 0; i < 2; i++) {
        DEBUGPRINT(3, "%s buffer:%u\n", __func__, i);

        pciefd_remove_one_buffer(vCard, &(hCard->dmaCtx.bufferCtx[i]));
    }

    return 0;
}

//======================================================================
//
//======================================================================
static void pciefd_aquire_dma_buffer(VCanCardData *vCard, int id)
{
    PciCanCardData *hCard = vCard->hwCardData;
    struct device *dev = &hCard->dev->dev;
    dmaCtx_t *dmaCtx = &hCard->dmaCtx;

    if (dmaCtx->active >= 0) {
        DEBUGPRINT(3, "DMA handling error\n");
    }

    dma_sync_single_for_cpu(dev, dmaCtx->bufferCtx[id].address, KCAN_DMA_BUFFER_SZ,
                            DMA_FROM_DEVICE);

    dmaCtx->active = id;
}

//======================================================================
//
//======================================================================
static void pciefd_release_dma_buffer(VCanCardData *vCard, int id)
{
    PciCanCardData *hCard = vCard->hwCardData;
    struct device *dev = &hCard->dev->dev;
    dmaCtx_t *dmaCtx = &hCard->dmaCtx;

    dma_sync_single_for_device(dev, dmaCtx->bufferCtx[id].address, KCAN_DMA_BUFFER_SZ,
                               DMA_FROM_DEVICE);

    dmaCtx->active = -1;
}

//======================================================================
//
//======================================================================
static int pciefd_start_dma(VCanCardData *vCard)
{
    PciCanCardData *hCard = vCard->hwCardData;
    uint32_t tmp;
    uint32_t dmaInterrupts = RXBUF_IRQ_DMA_DONE0_MSK | RXBUF_IRQ_DMA_DONE1_MSK |
                             RXBUF_IRQ_DMA_OVF0_MSK | RXBUF_IRQ_DMA_OVF1_MSK;

    DEBUGPRINT(3, "%s\n", __func__);

    IOWR_RXBUF_IRQ(hCard->io.kcan.rxCtrlBase, dmaInterrupts);

    tmp = IORD_RXBUF_IEN(hCard->io.kcan.rxCtrlBase);
    IOWR_RXBUF_IEN(hCard->io.kcan.rxCtrlBase, tmp | dmaInterrupts);

    hCard->dmaCtx.active = -1;

    armDMA0(hCard->io.kcan.rxCtrlBase);
    armDMA1(hCard->io.kcan.rxCtrlBase);

    if (!dmaIdle(hCard->io.kcan.rxCtrlBase)) {
        DEBUGPRINT(3, "DMA is not idle before start\n");
    }

    enableDMA(hCard->io.kcan.rxCtrlBase);

    hCard->dmaCtx.enabled = 1;

    return 0;
}

//======================================================================
//
//======================================================================
static int pciefd_stop_dma(VCanCardData *vCard)
{
    PciCanCardData *hCard = vCard->hwCardData;
    uint32_t tmp;
    uint32_t dmaInterrupts = RXBUF_IRQ_DMA_DONE0_MSK | RXBUF_IRQ_DMA_DONE1_MSK |
                             RXBUF_IRQ_DMA_OVF0_MSK | RXBUF_IRQ_DMA_OVF1_MSK;

    DEBUGPRINT(3, "%s\n", __func__);

    disableDMA(hCard->io.kcan.rxCtrlBase);
    if (!dmaIdle(hCard->io.kcan.rxCtrlBase)) {
        DEBUGPRINT(3, "Warning: Has not yet disabled DMA\n");
    }

    tmp = IORD_RXBUF_IEN(hCard->io.kcan.rxCtrlBase);
    // Disable interrupts
    IOWR_RXBUF_IEN(hCard->io.kcan.rxCtrlBase, tmp & ~dmaInterrupts);
    // Clear pending interrupts
    IOWR_RXBUF_IRQ(hCard->io.kcan.rxCtrlBase, dmaInterrupts);

    hCard->dmaCtx.active = -1;
    hCard->dmaCtx.enabled = 0;

    return 0;
}
#endif /* PCIEFD_USE_DMA */
static unsigned long timestamp_to_ticks(VCanChanData *vChan, unsigned long long ts)
{
    PciCanCardData* hCard = vChan->vCard->hwCardData;

    ts = div_u64(ts, vChan->usPerTick * hCard->freqToUsDivider);
    DEBUGPRINT(4, "Freq:%u US:%u TS:%llu\n", hCard->frequency, vChan->usPerTick, ts);
    return ts;
}

//======================================================================
// Current Time read from HW
// Must be called with card locked (to avoid access interference).
//
// Important: The LSB part must be read first. The MSB of the timestamp
// is registered when the LSB is read.
//======================================================================
static unsigned long long pciefd_get_hw_time(VCanCardData *vCard)
{
    PciCanCardData *hCard = vCard->hwCardData;
    unsigned long irqFlags;
    uint64_t sw_ts;

    spin_lock_irqsave(&hCard->lock, irqFlags);

    sw_ts = IORD(hCard->io.kcan.timeBase, 0);
    sw_ts |= (uint64_t)(IORD(hCard->io.kcan.timeBase, 1)) << 32;

    spin_unlock_irqrestore(&hCard->lock, irqFlags);

    return sw_ts;
}

//======================================================================
// Current Timestamp read from HW
//======================================================================
static int pciefd_get_time(VCanChanData *vChan, uint64_t *time)
{
    uint64_t sw_ts;

    sw_ts = pciefd_get_hw_time(vChan->vCard);

    *time = timestamp_to_ticks(vChan, sw_ts);

    return VCAN_STAT_OK;
}

//======================================================================
//  All acks received?
//======================================================================
static int pciefd_is_tx_buffer_empty(VCanChanData *vChd)
{
    PciCanChanData *hChd = vChd->hwChanData;

    DEBUGPRINT(4, "%s: %u\n", __func__, atomic_read(&hChd->outstanding_tx));

    return (atomic_read(&hChd->outstanding_tx) == 0);
} //  pciefd_is_tx_buffer_empty

//======================================================================
//  Can we send now?
//======================================================================
static bool pciefd_is_tx_buffer_full(VCanChanData *vChd)
{
    PciCanChanData *hChd = vChd->hwChanData;
    PciCanCardData *hCard = vChd->vCard->hwCardData;

    DEBUGPRINT(4, "%s (outstanding_tx:%u)\n", __func__, atomic_read(&hChd->outstanding_tx));

    return (atomic_read(&hChd->outstanding_tx) >= hCard->max_outstanding_tx);

} // pciefd_is_tx_buffer_full

//======================================================================
//  Calculate busload prescaler
//======================================================================
static uint32_t calcBusloadPrescaler(unsigned int quantaPerCycle, unsigned int brp)
{
    unsigned int blp = (quantaPerCycle * brp) / 2;
    if (blp < BLP_PRESC_MIN) {
        blp = BLP_PRESC_MIN;
    }
    if (blp > BLP_PRESC_MAX) {
        blp = BLP_PRESC_MAX;
    }
    return (PCIEFD_BLP_PRESC(blp) | PCIEFD_BLP_INTERV(BLP_INTERVAL));
}

// Get EAN as a string with hyphens
#define EAN_STR_LEN 16
static void get_ean(VCanCardData *vCard, char* ean)
{
    sprintf(ean, "%x-%05x-%05x-%x",
            ((*(uint32_t *)&(vCard->ean[4])) >> 12),
            (((*(uint32_t *)&(vCard->ean[4])) & 0xfff) << 8) |
            (((*(uint32_t *)&(vCard->ean[4])) >> 24) & 0xff),
            ((*(uint32_t *)&(vCard->ean[0])) >> 4) & 0xfffff,
            ((*(uint32_t *)&(vCard->ean[0])) & 0x0f));
}

//======================================================================
//  Print card related info
//======================================================================
static void printCardInfo(VCanCardData *vCard)
{
    PciCanCardData *hCard = vCard->hwCardData;
    uint32_t freq;
    int i;
    char bdf[BDF_STR_LEN + 1] = {0};
    char ean[EAN_STR_LEN + 1] = {0};

    get_bdf(hCard->dev, bdf);
    get_ean(vCard, ean);
    DEBUGPRINT(1, "Kvaser PCIe device %s kv%s, EAN %s, %u channel(s), max bitrate %u bit/s.\n",
               bdf, DEVICE_NAME_STRING, ean, vCard->nrChannels, vCard->current_max_bitrate);
#ifdef PCIEFD_DEBUG
    INITPRINT("----------------------------------------------------------------\n");
    INITPRINT("                        Parameters\n");
    INITPRINT("----------------------------------------------------------------\n");
    INITPRINT("    * Ean: %s\n", ean);
    INITPRINT("    * HW Revision Major: %u\n", vCard->hwRevisionMajor);
    INITPRINT("    * Serial: %u\n", vCard->serialNumber);
    INITPRINT("    * HW Type: %u\n", vCard->hw_type);
    INITPRINT("    * Default max bitrate: %d\n", (int)vCard->default_max_bitrate);
    INITPRINT("    * Number of channels: %u\n", vCard->nrChannels);

    for (i = 0; i < vCard->nrChannels; i++) {
        INITPRINT("    * Transceiver type ch %c: %u\n", 'A' + i, vCard->chanData[i]->transType);
    }

    for (i = 0; i < vCard->nrChannels; i++) {
        INITPRINT("    * Cust channel name [%u]: %s\n", i, hCard->cust_channel_name[i]);
    }

    INITPRINT("----------------------------------------------------------------\n");
    INITPRINT("                        Card info\n");
    INITPRINT("----------------------------------------------------------------\n");
    INITPRINT("    * Driver build time  : %s\n", __TIMESTAMP__);
    INITPRINT("    * FPGA build time    : %x %x\n", FPGA_META_build_date(hCard->io.kcan.metaBase),
              FPGA_META_build_time(hCard->io.kcan.metaBase));
    INITPRINT("    * FPGA version       : %u.%u.%u\n", FPGA_META_major_rev(hCard->io.kcan.metaBase),
              FPGA_META_minor_rev(hCard->io.kcan.metaBase),
              FPGA_META_user_seq_id(hCard->io.kcan.metaBase));

    INITPRINT(
        "    * FPGA source id     : %08llx (%s)\n", FPGA_META_source_id(hCard->io.kcan.metaBase),
        FPGA_META_is_clean_build(hCard->io.kcan.metaBase) ? "clean" : "has uncommitted changes");

    freq = FPGA_META_pci_freq(hCard->io.kcan.metaBase);
    INITPRINT("    * PCI bus frequency  : %u.%u MHz\n", freq / US_PER_SEC,
              freq / 100000 - 10 * (freq / US_PER_SEC));

    freq = FPGA_META_can_freq(hCard->io.kcan.metaBase);
    INITPRINT("    * CAN ctrl frequency : %u.%u MHz\n", freq / US_PER_SEC,
              freq / 100000 - 10 * (freq / US_PER_SEC));

    INITPRINT("----------------------------------------------------------------\n");
    INITPRINT("                        Capabilites\n");
    INITPRINT("----------------------------------------------------------------\n");

#if PCIEFD_USE_DMA
    if (hwSupportDMA(hCard->io.kcan.rxCtrlBase)) {
        INITPRINT("    * Receiver DMA supported\n");
    } else {
        INITPRINT("    * Receiver DMA Not supported\n");
    }
#endif /* PCIEFD_USE_DMA */

    INITPRINT("    * Max read buffer support %u\n",
              fifoPacketCountRxMax(hCard->io.kcan.rxCtrlBase));
    INITPRINT("    * number of channels %d\n", vCard->nrChannels);

    for (i = 0; i < vCard->nrChannels; i++) {
        VCanChanData *vChd = vCard->chanData[i];

        if (vChd != NULL) {
            PciCanChanData *hChd = vChd->hwChanData;

            INITPRINT("    -----------------------------------------------------------\n");
            INITPRINT("    Channel %u\n", i);
            INITPRINT("    -----------------------------------------------------------\n");

            if (hwSupportCanFD(hChd->canControllerBase)) {
                INITPRINT("        * CAN FD supported\n");
            }

            if (hwSupportCAP(hChd->canControllerBase)) {
                INITPRINT("        * SINGLE SHOT supported\n");
            }

            INITPRINT("        * Max write buffer support %u\n",
                      fifoPacketCountTxMax(hChd->canControllerBase));
        }
    }
    INITPRINT("----------------------------------------------------------------\n");
#else
NOT_USED(i);
NOT_USED(freq);
#endif /* PCIEFD_DEBUG */
}

//================================
// Set fallback parameter values
//================================
static void set_fallback_parameters(VCanCardData *vCard)
{
    PciCanCardData *hCard = vCard->hwCardData;

    if (DEVICE_IS_EDGE(hCard->dev->device)) {
        // EAN for  firmware = 98397-7
        u64 ean = (0x73301ULL << 32) + 0x30983977ULL;
        memcpy(vCard->ean, &ean, sizeof(vCard->ean));
    }
    else {
        // Use all zero EAN
        memset(vCard->ean, 0, sizeof(vCard->ean));
    }

    vCard->hwRevisionMajor = 1;
    vCard->serialNumber = 0UL;
    vCard->hw_type = 76UL;
    vCard->current_max_bitrate = vCard->default_max_bitrate = 8000000;

    if (vCard->nrChannels > 0) {
        vCard->chanData[0]->transType = 22U;
        strncpy(hCard->cust_channel_name[0], "", sizeof(hCard->cust_channel_name[0]));
    }

    if (vCard->nrChannels > 1) {
        vCard->chanData[1]->transType = 22U;
        strncpy(hCard->cust_channel_name[1], "", sizeof(hCard->cust_channel_name[1]));
    }

    if (vCard->nrChannels > 2) {
        vCard->chanData[2]->transType = 22U;
        strncpy(hCard->cust_channel_name[2], "", sizeof(hCard->cust_channel_name[2]));
    }

    if (vCard->nrChannels > 3) {
        vCard->chanData[3]->transType = 22U;
        strncpy(hCard->cust_channel_name[3], "", sizeof(hCard->cust_channel_name[3]));
    }
}

static int pciefd_read_flash_params(VCanCardData *vCard)
{
    PciCanCardData *hCard = vCard->hwCardData;
    int i;
    int status;
    u32 *ean;

    hCard->hflash.params.nr_channels = vCard->nrChannels;
    status = HYDRA_FLASH_read_params(&hCard->hflash);
    if (status != HYDRA_FLASH_STAT_OK)
        return VCAN_STAT_NO_DEVICE;

    ean = (uint32_t *)vCard->ean;
    ean[1] = hCard->hflash.params.ean >> 32;
    ean[0] = hCard->hflash.params.ean & 0xffffffff;

    vCard->serialNumber = hCard->hflash.params.serial_number;
    vCard->hwRevisionMajor = hCard->hflash.params.hw_rev_major;
    if (hCard->hflash.params.nr_channels > vCard->nrChannels) {
        DEBUGPRINT(1, "WARNING: Too many channels in flash params.\n");
    }
    vCard->nrChannels = min((unsigned int)vCard->nrChannels, (unsigned int)hCard->hflash.params.nr_channels);
    vCard->hw_type = hCard->hflash.params.hw_type;

    vCard->current_max_bitrate = vCard->default_max_bitrate = hCard->hflash.params.max_bitrate;
    for (i = 0; i < hCard->hflash.params.nr_channels; i++) {
        vCard->chanData[i]->transType = hCard->hflash.params.trans_type[i];
        // Check if params contain any custom name for channel i
        if (strnlen(hCard->hflash.params.cust_channel_name[i], 1)) {
            memcpy(hCard->cust_channel_name[i], hCard->hflash.params.cust_channel_name[i],
                   sizeof(cust_channel_name_t));
        }
    }

    return VCAN_STAT_OK;
}

//======================================================================
// Find out some info about the H/W
// (*cd) must have pciIf, xilinx and sjaBase initialized
// This is only called before a card is initialized, so no one can
// interfere with the accesses (lock not even initialized at this point).
//======================================================================
static int pciefd_probe_hw(VCanCardData *vCard)
{
    PciCanCardData *hCard = vCard->hwCardData;
    u8 supported_fpga_major_version = hCard->driver_data->hw_const.supported_fpga_major;
    int i;

    if (hCard->io.pcieBar0Base == 0) {
        DEBUGPRINT(1, "%s card_present = 0\n", __func__);
        vCard->cardPresent = 0;
        return VCAN_STAT_NO_DEVICE;
    }

    vCard->firmwareVersionMajor = FPGA_META_major_rev(hCard->io.kcan.metaBase);
    vCard->firmwareVersionMinor = FPGA_META_minor_rev(hCard->io.kcan.metaBase);
    vCard->firmwareVersionBuild = FPGA_META_user_seq_id(hCard->io.kcan.metaBase);

    if (vCard->firmwareVersionMajor < supported_fpga_major_version) {
        vCard->card_flags |= DEVHND_CARD_REFUSE_TO_USE_CAN;
        DEBUGPRINT(
            1,
            "ERROR: Unsupported firmware version %u.%u.%u. Please upgrade firmware to at least %d.0.1\n",
            vCard->firmwareVersionMajor, vCard->firmwareVersionMinor, vCard->firmwareVersionBuild,
            supported_fpga_major_version);
        return VCAN_STAT_NO_DEVICE;
    } else if (vCard->firmwareVersionMajor > supported_fpga_major_version) {
        vCard->card_flags |= DEVHND_CARD_REFUSE_TO_USE_CAN;
        DEBUGPRINT(
            1,
            "ERROR: This driver is outdated, please upgrade to a version which supports firmware %u.%u.%u.\n",
            vCard->firmwareVersionMajor, vCard->firmwareVersionMinor, vCard->firmwareVersionBuild);
        return VCAN_STAT_NO_DEVICE;
    }

    vCard->hwRevisionMajor = vCard->firmwareVersionMajor;
    vCard->hwRevisionMinor = 0;

    vCard->nrChannels = FPGA_META_num_channels(hCard->io.kcan.metaBase);
    vCard->nrChannels = min(vCard->nrChannels, MAX_CARD_CHANNELS);

    set_capability_value(
        vCard,
        VCAN_CHANNEL_CAP_SEND_ERROR_FRAMES | VCAN_CHANNEL_CAP_RECEIVE_ERROR_FRAMES |
            VCAN_CHANNEL_CAP_TIMEBASE_ON_CARD | VCAN_CHANNEL_CAP_BUSLOAD_CALCULATION |
            VCAN_CHANNEL_CAP_ERROR_COUNTERS | VCAN_CHANNEL_CAP_EXTENDED_CAN |
            VCAN_CHANNEL_CAP_TXREQUEST | VCAN_CHANNEL_CAP_TXACKNOWLEDGE | VCAN_CHANNEL_CAP_CANFD |
            VCAN_CHANNEL_CAP_CANFD_NONISO | VCAN_CHANNEL_CAP_SILENTMODE,
        0xFFFFFFFF, 0xFFFFFFFF, MAX_CARD_CHANNELS);

    set_capability_ex_value(
        vCard, 
            VCAN_CHANNEL_EX_CAP_HAS_BUSPARAMS_TQ |
            VCAN_CHANNEL_EX_CAP_HAS_ERRORFRAME_DATA,
        0xFFFFFFFF, 
        0xFFFFFFFF,
        MAX_CARD_CHANNELS
    );

    set_capability_ex_mask(
        vCard, 
            VCAN_CHANNEL_EX_CAP_HAS_BUSPARAMS_TQ | 
            VCAN_CHANNEL_EX_CAP_HAS_ERRORFRAME_DATA, 
        0xFFFFFFFF,
        0xFFFFFFFF,
        MAX_CARD_CHANNELS
    );

#if PCIEFD_USE_DMA
    hCard->useDma = 1;
    if (!hwSupportDMA(hCard->io.kcan.rxCtrlBase)) {
        hCard->useDma = 0;
    }
#endif /* PCIEFD_USE_DMA */

    for (i = 0; i < vCard->nrChannels; i++) {
        VCanChanData *vChd = vCard->chanData[i];
        int tx_max;

        if (vChd != NULL) {
            PciCanChanData *hChd = vChd->hwChanData;
            const struct pciefd_irq_defines *irq_def = hCard->driver_data->irq_def;

            // Multiple instances of a device are placed in
            // consecutive memory space with _SPAN bytes separation.
            hChd->canControllerBase = SUM(
                hCard->io.pcieBar0Base, hCard->driver_data->offsets.kcan.tx0 +
                                            (i * hCard->driver_data->offsets.kcan.controller_span));

            // PCIe interface interrupt mask for this channel
            hChd->tx_irq_msk = irq_def->tx[i];

            vChd->channel = i;
            if (hwSupportCAP(hChd->canControllerBase)) {
                set_capability_value(vCard, VCAN_CHANNEL_CAP_SINGLE_SHOT, 1, (1 << i),
                                     vCard->nrChannels);
                set_capability_mask(vCard, VCAN_CHANNEL_CAP_SINGLE_SHOT, 1, (1 << i),
                                    vCard->nrChannels);
            }

            tx_max = fifoPacketCountTxMax(hChd->canControllerBase);
            if (KCAN_MAX_OUTSTANDING_TX < tx_max) {
                DEBUGPRINT(1, "Error: Driver Tx buffer is smaller than KCAN Tx buffer, %u < %d\n",
                           KCAN_MAX_OUTSTANDING_TX, tx_max);
                return VCAN_STAT_NO_MEMORY;
            }
            hCard->max_outstanding_tx = tx_max;
        } else {
            vCard->cardPresent = 0;
            return VCAN_STAT_NO_DEVICE;
        }
    }

    if (vCard->nrChannels == 0) {
        // No channels found
        vCard->cardPresent = 0;
        return VCAN_STAT_NO_DEVICE;
    }

    vCard->cardPresent = 1;
    hCard->frequency = FPGA_META_can_freq(hCard->io.kcan.metaBase);
    hCard->freqToUsDivider = hCard->frequency / US_PER_SEC; // 1e6 for s to us conversion
    if (hCard->freqToUsDivider == 0)
        hCard->freqToUsDivider = 1;

    return VCAN_STAT_OK;
} // pciefd_probe_hw

static inline int pciefd_check_busparams(const PciCanCardData *hCard, const busParams_t *busParams,
                                         const char *typeStr)
{
    int res = 0;
    // Detect overflow
    if (busParams->tq < busParams->tseg1) {
        DEBUGPRINT(1, "Error (" DEVICE_NAME_STRING "): Bad parameter CAN %s (tseg1)\n", typeStr);
        res = 1;
    } else if (busParams->tq < busParams->tseg2) {
        DEBUGPRINT(1, "Error (" DEVICE_NAME_STRING "): Bad parameter CAN %s (tseg2)\n", typeStr);
        res = 2;
    } else if (busParams->tq == 0) {
        DEBUGPRINT(1, "Error (" DEVICE_NAME_STRING "): Bad parameter CAN %s (tq)\n", typeStr);
        res = 3;
    } else if (busParams->brp < 1 || busParams->brp > 8192 || // 13-bits 1 + 0..8191
               busParams->sjw < 1 || busParams->sjw > 16 || // 4-bits  1 + 0..15
               busParams->tseg1 < 1 || busParams->tseg1 > 512 || // 9-bits  1 + 0..511
               busParams->tseg2 < 1 || busParams->tseg2 > 32) { // 5-bits  1 + 0..31
        DEBUGPRINT(1,
                   "Error (" DEVICE_NAME_STRING
                   "): Other checks CAN %s phase brp:%u sjw:%u tseg1:%u tseg2:%u\n",
                   typeStr, busParams->brp, busParams->sjw, busParams->tseg1, busParams->tseg2);

        res = 4;
    }

    if (res == 0 && busParams->freq) {
        uint32_t recalc_freq;

        recalc_freq = hCard->frequency / (busParams->brp * busParams->tq);
        if (recalc_freq != busParams->freq) {
            DEBUGPRINT(
                1,
                "Error (" DEVICE_NAME_STRING
                "): CAN %s phase prescaler residual. The parameters bitrate:%u tseg1:%u tseg2:%u give this bitrate:%u\n",
                typeStr, busParams->freq, busParams->tseg1, busParams->tseg2, recalc_freq);
            res = 5;
        }
    }

    return res;
}

static int pciefd_set_busparams_inner(VCanChanData *vChd, busParams_t *arb, busParams_t *brs,
                                      unsigned int useBrs)
{
    PciCanChanData *hChd = vChd->hwChanData;
    PciCanCardData *hCard = vChd->vCard->hwCardData;
    uint32_t btrn;
    uint32_t btrd = 0;
    uint32_t bus_load_prescaler;
    uint32_t mode;
    unsigned long irqFlags;
    unsigned int trdce_supported = 0;

    if (pciefd_check_busparams(hCard, arb, "arbitration")) {
        return VCAN_STAT_BAD_PARAMETER;
    }

    btrn = PCIEFD_BTR_SEG2(arb->tseg2 - 1) | PCIEFD_BTR_SEG1(arb->tseg1 - 1) |
           PCIEFD_BTR_SJW(arb->sjw - 1) | PCIEFD_BTR_BRP(arb->brp - 1);

    bus_load_prescaler = calcBusloadPrescaler(arb->tq, arb->brp);

    if (useBrs && hwSupportCanFD(hChd->canControllerBase)) {
        if (pciefd_check_busparams(hCard, brs, "FD data")) {
            return VCAN_STAT_BAD_PARAMETER;
        }

        // Is TRDCE implemented in FPGA?
        if (IORD_PCIEFD_STAT(hChd->canControllerBase) & PCIEFD_STAT_CAP_MSK) {
            if (PCIEFD_HW_CAP_TRDCE_GET(IORD_PCIEFD_HW_CAP(hChd->canControllerBase))) {
                trdce_supported = 1;
            } else {
                DEBUGPRINT(2, "TRDCE not supported by HW. Can not change TRDCE setting\n");
            }
        } else {
            DEBUGPRINT(2, "FPGA_CAN_STAT_CAP unavailable. Can not change SSP settings\n");
        }

        if (trdce_supported == 0) {
            if (brs->brp > CANFD_MAX_PRESCALER_VALUE) {
                DEBUGPRINT(1, "Error (pciefd): prescaler (%u) out of limits (>%u)\n",
                           brs->brp, CANFD_MAX_PRESCALER_VALUE);
                return VCAN_STAT_BAD_PARAMETER;
            }
        }

        btrd = PCIEFD_BTR_SEG2(brs->tseg2 - 1) | PCIEFD_BTR_SEG1(brs->tseg1 - 1) |
               PCIEFD_BTR_SJW(brs->sjw - 1) | PCIEFD_BTR_BRP(brs->brp - 1);

        if (OPEN_AS_CAN != vChd->openMode) {
            bus_load_prescaler = calcBusloadPrescaler(brs->tq, brs->brp);
        }
    }

    // Use mutex to allow this process to sleep
    wait_for_completion(&hChd->busOnCompletion);

    if (vChd->isOnBus) {
        DEBUGPRINT(2,
                   "Error (" DEVICE_NAME_STRING
                   "): Trying to set bus parameter when on bus CH:%u\n",
                   vChd->channel);

        complete(&hChd->busOnCompletion);

        pciefd_req_chipstate(vChd);

        return VCAN_STAT_OK;
    }

    spin_lock_irqsave(&hChd->lock, irqFlags);

    IOWR_PCIEFD_BLP(hChd->canControllerBase, bus_load_prescaler);
    hChd->bus_load_prescaler = bus_load_prescaler;

    mode = IORD_PCIEFD_MOD(hChd->canControllerBase);

    /* Put the circuit in Reset Mode */
    IOWR_PCIEFD_MOD(hChd->canControllerBase, mode | PCIEFD_MOD_RM_MSK);

    IOWR_PCIEFD_BTRN(hChd->canControllerBase, btrn);

    if (useBrs && hwSupportCanFD(hChd->canControllerBase)) {
        IOWR_PCIEFD_BTRD(hChd->canControllerBase, btrd);
        if (trdce_supported) {
            uint32_t ssp_ctrl;

            // TRDCE (transmitter delay compensation) shall be enabled if
            // brp is 1 or 2, disabled otherwise.
            // see the kcan specification for details.
            ssp_ctrl = IORD_PCIEFD_SSP_CTRL(hChd->canControllerBase);
            switch (brs->brp) {
            case 1:
            case 2:
                ssp_ctrl |= PCIEFD_SSP_TRDCE(1);
                DEBUGPRINT(3, "canfd brp is %u, TRDCE = 1\n", brs->brp);
                break;

            default:
                ssp_ctrl &= ~PCIEFD_SSP_TRDCE_MSK;
                DEBUGPRINT(3, "canfd brp is %u, TRDCE = 0\n", brs->brp);
                break;
            }
            IOWR_PCIEFD_SSP_CTRL(hChd->canControllerBase, ssp_ctrl);
        }
    }

    mode &= ~PCIEFD_MOD_SSO_MSK;

    // Restore previous reset mode status
    IOWR_PCIEFD_MOD(hChd->canControllerBase, mode);

    spin_unlock_irqrestore(&hChd->lock, irqFlags);

    complete(&hChd->busOnCompletion);

    return VCAN_STAT_OK;
}

//======================================================================
//  Set bit timing
//======================================================================
static int pciefd_set_busparams(VCanChanData *vChd, VCanBusParams *par)
{
    PciCanChanData *hChd = vChd->hwChanData;
    PciCanCardData *hCard = vChd->vCard->hwCardData;
    busParams_t arb;
    busParams_t brs = { 0 };
    unsigned int useBrs = 0;

    arb.freq = par->freq;
    arb.sjw = par->sjw;
    arb.tseg1 = par->tseg1;
    arb.tseg2 = par->tseg2;

    arb.tq = arb.tseg1 + arb.tseg2 + 1;
    DEBUGPRINT(2, "Set CAN arbitration params: bitrate:%u, tseg1:%u, tseg2:%u, sjw:%u, tq:%u\n",
               arb.freq, arb.tseg1, arb.tseg2, arb.sjw, arb.tq);
    if (arb.freq == 0) {
        DEBUGPRINT(1, "Error (" DEVICE_NAME_STRING "): Bad parameter CAN arbitration (freq)\n");
        return VCAN_STAT_BAD_PARAMETER;
    }
    if (arb.tq == 0) {
        DEBUGPRINT(1, "Error (" DEVICE_NAME_STRING "): Bad parameter CAN arbitration (tq)\n");
        return VCAN_STAT_BAD_PARAMETER;
    }
    arb.brp = hCard->frequency / (arb.freq * arb.tq);

    if (hwSupportCanFD(hChd->canControllerBase)) {
        brs.freq = par->freq_brs;
        brs.sjw = par->sjw_brs;
        brs.tseg1 = par->tseg1_brs;
        brs.tseg2 = par->tseg2_brs;

        brs.tq = brs.tseg1 + brs.tseg2 + 1;
        DEBUGPRINT(2, "Set CAN FD data params: bitrate:%u, tseg1:%u, tseg2:%u, sjw:%u, tq:%u\n",
                   brs.freq, brs.tseg1, brs.tseg2, brs.sjw, brs.tq);
        if (brs.freq == 0) {
            DEBUGPRINT(1, "Error (" DEVICE_NAME_STRING "): Bad parameter CAN FD data (freq)\n");
            return VCAN_STAT_BAD_PARAMETER;
        }
        if (brs.tq == 0) {
            DEBUGPRINT(1, "Error (" DEVICE_NAME_STRING "): Bad parameter CAN FD data (tq)\n");
            return VCAN_STAT_BAD_PARAMETER;
        }
        brs.brp = hCard->frequency / (brs.freq * brs.tq);
        useBrs = 1;
    }

    return pciefd_set_busparams_inner(vChd, &arb, &brs, useBrs);
} // pciefd_set_busparams

static int pciefd_set_busparams_tq(VCanChanData *vChd, VCanBusParamsTq *par)
{
    PciCanChanData *hChd = vChd->hwChanData;
    busParams_t arb;
    busParams_t brs = { 0 };
    int stat;
    int ret;
    unsigned int useBrs = par->data_valid;

    arb.brp = par->nominal.prescaler;
    arb.sjw = par->nominal.sjw;
    arb.tseg1 = par->nominal.phase1 + par->nominal.prop;
    arb.tseg2 = par->nominal.phase2;
    arb.tq = par->nominal.tq;
    arb.freq = 0;

    DEBUGPRINT(1, "Set CAN arbitration params: prescaler:%u, tseg1:%u, tseg2:%u, sjw:%u, tq:%u\n",
               arb.brp, arb.tseg1, arb.tseg2, arb.sjw, arb.tq);

    if (useBrs && hwSupportCanFD(hChd->canControllerBase)) {
        brs.brp = par->data.prescaler;
        brs.sjw = par->data.sjw;
        brs.tseg1 = par->data.phase1 + par->data.prop;
        brs.tseg2 = par->data.phase2;
        brs.tq = par->data.tq;
        brs.freq = 0;

        DEBUGPRINT(1, "Set CAN FD data params: prescaler:%u, tseg1:%u, tseg2:%u, sjw:%u, tq:%u\n",
                   brs.brp, brs.tseg1, brs.tseg2, brs.sjw, brs.tq);
    }
    stat = pciefd_set_busparams_inner(vChd, &arb, &brs, useBrs);
    if (stat == VCAN_STAT_BAD_PARAMETER) {
        par->retval_from_driver = 0;
        par->retval_from_device = 1;
        ret = 0;
    } else {
        par->retval_from_driver = stat;
        par->retval_from_device = stat;
        ret = stat;
    }

    return ret;
} // pciefd_set_busparams_tq

//======================================================================
//  Get bit timing
//======================================================================
static int pciefd_get_busparams(VCanChanData *vChd, VCanBusParams *par)
{
    PciCanChanData *hChd = vChd->hwChanData;
    VCanCardData *vCard = vChd->vCard;
    PciCanCardData *hCard = vCard->hwCardData;

    unsigned int quantaPerCycle;
    unsigned long brp;
    unsigned long irqFlags;
    unsigned long btrn, btrd = 0;

    spin_lock_irqsave(&hChd->lock, irqFlags);

    btrn = IORD_PCIEFD_BTRN(hChd->canControllerBase);

    if (hwSupportCanFD(hChd->canControllerBase)) {
        btrd = IORD_PCIEFD_BTRD(hChd->canControllerBase);
    }
    spin_unlock_irqrestore(&hChd->lock, irqFlags);

    par->sjw = 1 + PCIEFD_BTR_SJW_GET(btrn);
    par->tseg1 = 1 + PCIEFD_BTR_SEG1_GET(btrn);
    par->tseg2 = 1 + PCIEFD_BTR_SEG2_GET(btrn);
    par->samp3 = 1;

    quantaPerCycle = par->tseg1 + par->tseg2 + 1;
    brp = 1 + PCIEFD_BTR_BRP_GET(btrn);

    par->freq = hCard->frequency / (quantaPerCycle * brp);

    if (hwSupportCanFD(hChd->canControllerBase)) {
        par->sjw_brs = 1 + PCIEFD_BTR_SJW_GET(btrd);
        par->tseg1_brs = 1 + PCIEFD_BTR_SEG1_GET(btrd);
        par->tseg2_brs = 1 + PCIEFD_BTR_SEG2_GET(btrd);
        par->samp3 = 1;

        quantaPerCycle = par->tseg1_brs + par->tseg2_brs + 1;
        brp = 1 + PCIEFD_BTR_BRP_GET(btrd);

        par->freq_brs = hCard->frequency / (quantaPerCycle * brp);
    }

    return VCAN_STAT_OK;
} // pciefd_get_busparams

static int pciefd_get_busparams_tq(VCanChanData *vChd, VCanBusParamsTq *par)
{
    PciCanChanData *hChd = vChd->hwChanData;
    uint32_t btr;

    btr = IORD_PCIEFD_BTRN(hChd->canControllerBase);
    par->nominal.phase1 = PCIEFD_BTR_SEG1_GET(btr) + 1;
    par->nominal.phase2 = PCIEFD_BTR_SEG2_GET(btr) + 1;
    par->nominal.sjw = PCIEFD_BTR_SJW_GET(btr) + 1;
    par->nominal.prescaler = PCIEFD_BTR_BRP_GET(btr) + 1;
    par->nominal.tq = 1 + par->nominal.phase1 + par->nominal.phase2;
    par->nominal.prop = 0;

    if (hwSupportCanFD(hChd->canControllerBase)) {
        btr = IORD_PCIEFD_BTRD(hChd->canControllerBase);
        par->data.phase1 = PCIEFD_BTR_SEG1_GET(btr) + 1;
        par->data.phase2 = PCIEFD_BTR_SEG2_GET(btr) + 1;
        par->data.sjw = PCIEFD_BTR_SJW_GET(btr) + 1;
        par->data.prescaler = PCIEFD_BTR_BRP_GET(btr) + 1;
        par->data.tq = 1 + par->data.phase1 + par->data.phase2;
        par->data.prop = 0;
        par->data_valid = 1;
    } else {
        par->data.phase1 = 0;
        par->data.phase2 = 0;
        par->data.sjw = 0;
        par->data.prescaler = 0;
        par->data.tq = 0;
        par->data.prop = 0;
        par->data_valid = 0;
    }
    par->retval_from_driver = 0;
    par->retval_from_device = 0;

    return VCAN_STAT_OK;
} // pciefd_get_busparams_tq

static int pciefd_get_clock_freq(const VCanChanData *chd, uint32_t *freq_mhz)
{
    VCanCardData *vCard = chd->vCard;
    PciCanCardData *hCard = vCard->hwCardData;

    if (hCard) {
        *freq_mhz = hCard->frequency / US_PER_SEC;
        return 0;
    } else {
        return -1;
    }
}

//======================================================================
//  Set silent or normal mode
//======================================================================
static int pciefd_set_output_mode(VCanChanData *vChd, int silent)
{
    PciCanChanData *hChd = vChd->hwChanData;
    unsigned long irqFlags;
    uint32_t mode;

    spin_lock_irqsave(&hChd->lock, irqFlags);

    // Save control register
    mode = IORD_PCIEFD_MOD(hChd->canControllerBase);
    // Put the circuit in Reset Mode
    IOWR_PCIEFD_MOD(hChd->canControllerBase, mode | PCIEFD_MOD_RM_MSK);

    if (silent) {
        mode |= PCIEFD_MOD_LOM_MSK;
    } else {
        mode &= ~PCIEFD_MOD_LOM_MSK;
    }

    // Restore control register
    IOWR_PCIEFD_MOD(hChd->canControllerBase, mode);

    spin_unlock_irqrestore(&hChd->lock, irqFlags);

    return VCAN_STAT_OK;
} // pciefd_set_output_mode

//======================================================================
//  Get silent or normal mode
//======================================================================
static int pciefd_get_output_mode(VCanChanData *vChd, int *silent)
{
    PciCanChanData *hChd = vChd->hwChanData;
    unsigned long irqFlags;
    uint32_t mode;

    if (!vChd || !silent)
        return VCAN_STAT_BAD_PARAMETER;

    spin_lock_irqsave(&hChd->lock, irqFlags);

    mode = IORD_PCIEFD_MOD(hChd->canControllerBase);

    *silent = !!(mode & PCIEFD_MOD_LOM_MSK);

    spin_unlock_irqrestore(&hChd->lock, irqFlags);

    return VCAN_STAT_OK;
}

//======================================================================
//  Line mode
//======================================================================
static int pciefd_set_tranceiver_mode(VCanChanData *vChd, int linemode, int resnet)
{
    vChd->lineMode = linemode;
    return VCAN_STAT_OK;
} // pciefd_set_tranceiver_mode

//======================================================================
// Query chip status (internal)
// Should not be called with channel locked
//======================================================================
static int pciefd_req_chipstate(VCanChanData *vChd)
{
    PciCanChanData *hChd = vChd->hwChanData;
    VCanCardData *vCard = vChd->vCard;
    PciCanCardData *hCard = vCard->hwCardData;
    WaitNode waitNode;
    unsigned long irqFlags = 0;
    int timeout;
    int current_id = atomic_inc_return(&hCard->status_seq_no);
    int active_id;
    bool doTrace = false;

#ifdef CHIP_STATE_DBG_MSG_DIVISOR
    static u32 dbgCount = 0;

    if (dbgCount++ == CHIP_STATE_DBG_MSG_DIVISOR) {
        dbgCount = 0;
        doTrace = true;
    }
#endif

    if (doTrace)
        DEBUGPRINT(4, "pciefd_req_chipstate\n");

    if (current_id > 255) {
        // Only one unit is allowed to wrap around counter
        spin_lock_irqsave(&hCard->lock, irqFlags);

        if ((atomic_read(&hCard->status_seq_no) > 255)) {
            atomic_set(&hCard->status_seq_no, 1);
            DEBUGPRINT(4, "Wraparound status request id\n");
        }

        spin_unlock_irqrestore(&hCard->lock, irqFlags);

        current_id = atomic_inc_return(&hCard->status_seq_no);
    }

    // Sleep until status request packet is returned or timeout.
    // Must not be called from interrupt context.
    init_completion(&waitNode.waitCompletion);

    waitNode.replyPtr = NULL;
    waitNode.cmdNr = vChd->channel; // Channel ID
    waitNode.transId = current_id;
    waitNode.timedOut = 0;

    // Add to card's list of expected responses
    write_lock_irqsave(&hCard->replyWaitListLock, irqFlags);

    spin_lock(&hChd->lock);

    active_id = statPendingRequest(hChd->canControllerBase);

    if (active_id >= 0) {
        if (doTrace)
            DEBUGPRINT(4, "Found active %d\n", active_id);
        waitNode.transId = active_id;
        current_id = active_id;
    } else {
        if (doTrace)
            DEBUGPRINT(4, "Status request:%u\n", current_id);
        // Request chip state from CAN Controller
        fpgaStatusRequest(hChd->canControllerBase, current_id);
    }

    spin_unlock(&hChd->lock);

    list_add(&waitNode.list, &hCard->replyWaitList);
    write_unlock_irqrestore(&hCard->replyWaitListLock, irqFlags);

    if (doTrace)
        DEBUGPRINT(6, "Waiting for stat ch:%u\n", waitNode.cmdNr);

    timeout = wait_for_completion_timeout(&waitNode.waitCompletion,
                                          msecs_to_jiffies(PCIEFD_SRQ_RESP_WAIT_TIME));

    // Now we either got a response or a timeout
    write_lock_irqsave(&hCard->replyWaitListLock, irqFlags);
    list_del(&waitNode.list);
    write_unlock_irqrestore(&hCard->replyWaitListLock, irqFlags);

    if (timeout == 0) {
        if (doTrace)
            DEBUGPRINT(1, DEVICE_NAME_STRING "WaitResponse: return VCAN_STAT_TIMEOUT ch:%u id:%u\n",
                       waitNode.cmdNr, current_id);
        return VCAN_STAT_TIMEOUT;
    } else {
        DEBUGPRINT(4, "Status ack on ch:%u, id:%u\n", waitNode.cmdNr, current_id);
    }

    return VCAN_STAT_OK;
} // pciefd_req_chipstate

//======================================================================
//  Wait functions for Bus On/Off
//======================================================================

static int waitForIdleMode(int loopmax, VCanChanData *vChd)
/* Make sure that unit is in reset mode before starting flush.
 *
 * Waits a maximum of approximately loopmax ms for unit to reach idle state.
 * Returns number of turns in waiting loop if less than loopmax, otherwise
 * returns -1 to indicate timeout.
 *
 * Only call with spinlock held and interrupts disabled.
 */
{
    PciCanChanData *hChd = vChd->hwChanData;
    int loopcount = loopmax;

    while (loopcount && !statIdle(hChd->canControllerBase)) {
        spin_unlock_irq(&hChd->lock);

        set_current_state(TASK_UNINTERRUPTIBLE);
        schedule_timeout(msecs_to_jiffies(10)); // Wait 10 ms

        spin_lock_irq(&hChd->lock);

        if (vChd->vCard->cardPresent) {
            --loopcount;
        } else {
            loopcount = 0;
        }
    }

    if (loopcount == 0) {
        return -1;
    } else {
        return loopmax - loopcount;
    }
}

static int waitForAbort(int loopmax, VCanChanData *vChd)
/* Wait for abort to complete before continuing.
 *
 * Waits a maximum of approximately loopmax ms for abort command to finish.
 * Returns number of turns in waiting loop if less than loopmax, otherwise
 * returns -1 to indicate timeout.
 *
 * Only call with spinlock held and interrupts disabled.
*/
{
    PciCanChanData *hChd = vChd->hwChanData;
    int loopcount = loopmax;

    while (loopcount) {
        if (istatCheck(hChd->canControllerBase, PCIEFD_IRQ_ABD_MSK)) {
            DEBUGPRINT(4, "Abort Done CH:%u\n", vChd->channel);
            break;
        } else {
            //      DEBUGPRINT(4,"Abort Busy CH:%u\n",vChd->channel);

            spin_unlock_irq(&hChd->lock);

            set_current_state(TASK_UNINTERRUPTIBLE);
            schedule_timeout(msecs_to_jiffies(1)); // Wait 1 ms

            spin_lock_irq(&hChd->lock);
        }

        if (statFlushRequest(hChd->canControllerBase)) {
            DEBUGPRINT(3, "Warning: tx flush req is asserted CH:%u\n", vChd->channel);
        }

        if (vChd->vCard->cardPresent) {
            --loopcount;
        } else {
            loopcount = 0;
        }
    }

    if (loopcount < loopmax - 10) {
        DEBUGPRINT(3, "Waiting for Abort Done more than 10ms (%u)\n", loopmax - loopcount);
    }

    if (!istatCheck(hChd->canControllerBase, PCIEFD_IRQ_ABD_MSK)) {
        DEBUGPRINT(3, "Abort Still Busy, probably failed initiating CH:%u\n", vChd->channel);
    }

    if (loopcount == 0) {
        return -1;
    } else {
        return loopmax - loopcount;
    }
}

static int waitForFlush(int loopmax, VCanChanData *vChd)
/* Wait for flush to complete before continuing.
 *
 * Waits a maximum of approximately loopmax ms for packet flush to finish.
 * Returns number of turns in waiting loop if less than loopmax, otherwise
 * returns -1 to indicate timeout.
 *
 * Only call with spinlock held and interrupts disabled.
 */
{
    PciCanChanData *hChd = vChd->hwChanData;
    int loopcount = loopmax;

    while (loopcount) {
        uint32_t level;

        level = fifoPacketCountTx(hChd->canControllerBase);

        if (level == 0) {
            break;
        }

        DEBUGPRINT(3, "TX FIFO Should have been empty P:%u CH:%u STAT:%x\n", level, vChd->channel,
                   IORD_PCIEFD_STAT(hChd->canControllerBase));

        spin_unlock_irq(&hChd->lock);

        set_current_state(TASK_UNINTERRUPTIBLE);
        schedule_timeout(msecs_to_jiffies(1)); // Wait 1 ms

        spin_lock_irq(&hChd->lock);

        if (vChd->vCard->cardPresent) {
            --loopcount;
        } else {
            loopcount = 0;
        }
    }

    if (loopcount == 0) {
        DEBUGPRINT(1, "Warning: Transmit buffer is not empty:\n");
        DEBUGPRINT(1, " - Probably a failed flush operation [BUSON]\n");
        return -1;
    } else {
        return loopmax - loopcount;
    }
}

static int waitForBusOn(int loopmax, VCanChanData *vChd)
/* Wait for bus on
 *
 * Waits a maximum of approximately loopmax ms for card to go bus on.
 * Returns number of turns in waiting loop if less than loopmax, otherwise
 * returns -1 to indicate timeout.
 *
 * Never call with spinlock held or interrupts disabled.
 */
{
    int loopcount = loopmax;

    // Wait until bus on is entered.
    while (loopcount) {
        if (pciefd_req_chipstate(vChd) == VCAN_STAT_OK) {
            if ((vChd->chipState.state & CHIPSTAT_BUSOFF) != CHIPSTAT_BUSOFF) {
                break;
            }
        } else {
            //      DEBUGPRINT(1, "Stat Failed\n");
        }

        // Wait 1 ms for transceiver to reach bus on
        set_current_state(TASK_UNINTERRUPTIBLE);
        schedule_timeout(msecs_to_jiffies(1));

        if (vChd->vCard->cardPresent) {
            --loopcount;
        } else {
            loopcount = 0;
        }
    }

    if (loopcount == 0) {
        DEBUGPRINT(1, "Bus on failed CH:%u\n", vChd->channel);
        return -1;
    } else {
        DEBUGPRINT(4, "Bus on success CH:%u\n", vChd->channel);
        return loopmax - loopcount;
    }
}

//======================================================================
// Reading bus stats, only bus load is read
//======================================================================
static int pciefd_req_bus_stats(VCanChanData *vChan)
{
    PciCanChanData *hChd = vChan->hwChanData;

    vChan->busStats.busLoad = hChd->load;

    return VCAN_STAT_OK;
}

static int pciefd_update_fw_ioctl(const VCanChanData *vChd, KCAN_FLASH_PROG *fp)
{
    VCanCardData *vCard = vChd->vCard;
    PciCanCardData *hCard = vCard->hwCardData;

    return HYDRA_FLASH_update_fw_ioctl(&hCard->hflash, fp);
}

//======================================================================
//  Go bus on
//======================================================================

static int pciefd_bus_on(VCanChanData *vChd)
{
    PciCanChanData *hChd = vChd->hwChanData;
    unsigned long tmp;
    unsigned long irqFlags;
    int stat, loopmax;

    // Use mutex to allow this process to sleep
    wait_for_completion(&hChd->busOnCompletion);

    if (vChd->isOnBus) {
        DEBUGPRINT(4, "Warning: Already on bus CH:%u\n", vChd->channel);

        complete(&hChd->busOnCompletion);

        pciefd_req_chipstate(vChd);

        return VCAN_STAT_OK;
    }
    DEBUGPRINT(4, "BUS ON CH:%u\n", vChd->channel);

    spin_lock_irqsave(&hChd->lock, irqFlags);

    // Disables all interrupts
    irqInit(hChd->canControllerBase);

    // Make sure that unit is in reset mode
    tmp = IORD_PCIEFD_MOD(hChd->canControllerBase);

    if (!PCIEFD_MOD_RM_GET(tmp)) {
        DEBUGPRINT(4, "Warning: Reset mode not commanded before bus on\n");

        IOWR_PCIEFD_MOD(hChd->canControllerBase, tmp | PCIEFD_MOD_RM_MSK);
    }

    atomic_set(&hChd->outstanding_tx, 0);

    memset(hChd->current_tx_message, 0, sizeof(hChd->current_tx_message));

    vChd->isOnBus = 1;
    vChd->overrun = 0;
    vChd->errorCount = 0;

    // Make sure that unit is in reset mode before starting flush
    loopmax = 100; // Wait a maximum of 100 ms
    stat = waitForIdleMode(loopmax, vChd);

    if (stat < 0) {
        tmp = IORD_PCIEFD_STAT(hChd->canControllerBase);

        DEBUGPRINT(3, "Timeout when waiting for idle state:\n");
        DEBUGPRINT(3, " - reset mode commanded(%c) tx idle(%c) in reset mode(%c) [BUS ON]\n",
                   statResetRequest(hChd->canControllerBase) ? 'x' : ' ',
                   statTransmitterIdle(hChd->canControllerBase) ? 'x' : ' ',
                   statInResetMode(hChd->canControllerBase) ? 'x' : ' ');
    }

    // Clear abort done irq flag
    IOWR_PCIEFD_IRQ(hChd->canControllerBase, PCIEFD_IRQ_ABD_MSK);

    // Clear RXERR, TXERR and Flush buffers
    // * This command will reset the CAN controller. When the reset is done a
    //   status packet will be placed in the receiver buffer. The status packet
    //   will have the associated command sequence number and the init detected
    //   bit set.
    fpgaFlushAll(hChd->canControllerBase, 0);

    // Wait for abort done signal
    loopmax = 100; // Wait a maximum of 100 ms
    stat = waitForAbort(loopmax, vChd);

    // Wait for flush to finish
    loopmax = 10; // Wait a maximum of 10 ms
    stat = waitForFlush(loopmax, vChd);

    // Write a flush recovery control word
    IOWR_PCIEFD_CONTROL(hChd->canControllerBase, PCIEFD_CONTROL_END_FLUSH);

    // Enable bus load packets
    IOWR_PCIEFD_BLP(hChd->canControllerBase, hChd->bus_load_prescaler);

    if (vChd->openMode == OPEN_AS_CANFD_NONISO) {
        enableNonIsoMode(hChd->canControllerBase);
    } else if (vChd->openMode == OPEN_AS_CANFD_ISO) {
        disableNonIsoMode(hChd->canControllerBase);
    }

    tmp = IORD_PCIEFD_MOD(hChd->canControllerBase);
    if (OPEN_AS_CAN == vChd->openMode) {
        tmp |= PCIEFD_MOD_CLASSIC(1);
    } else {
        tmp &= ~PCIEFD_MOD_CLASSIC_MSK;
    }
    IOWR_PCIEFD_MOD(hChd->canControllerBase, tmp & ~PCIEFD_MOD_RM_MSK);

    irqEnableTransmitterError(hChd->canControllerBase);

    vChd->chipState.state = CHIPSTAT_ERROR_ACTIVE;

    spin_unlock_irq(&hChd->lock);

    // Wait for bus on
    loopmax = 1000; // Wait a maximum of 1000 ms
    stat = waitForBusOn(loopmax, vChd);

    spin_lock_irq(&hChd->lock);

    KCAN_LED_ON(hChd->canControllerBase);

    spin_unlock_irqrestore(&hChd->lock, irqFlags);

    complete(&hChd->busOnCompletion);

    led_trigger_event(hChd->ledtrig.can_error, LED_OFF);
    led_trigger_event(hChd->ledtrig.buffer_overrun, LED_OFF);
    led_trigger_event(hChd->ledtrig.bus_on, LED_FULL);

    return VCAN_STAT_OK;
} // pciefd_bus_on

//======================================================================
//  Go bus off
//======================================================================
static int pciefd_bus_off(VCanChanData *vChd)
{
    PciCanChanData *hChd = vChd->hwChanData;
    unsigned long tmp;
    unsigned long irqFlags;
    int stat, loopmax;

    wait_for_completion(&hChd->busOnCompletion);

    DEBUGPRINT(4, "BUS OFF CH:%u\n", vChd->channel);

    // Since we are bus off we have no messages *on the way*
    atomic_set(&hChd->outstanding_tx, 0);

    spin_lock_irqsave(&hChd->lock, irqFlags);

    vChd->isOnBus = 0;
    vChd->overrun = 0;

    memset(hChd->current_tx_message, 0, sizeof(hChd->current_tx_message));

    queue_reinit(&vChd->txChanQueue);

    tmp = IORD_PCIEFD_MOD(hChd->canControllerBase);

    IOWR_PCIEFD_MOD(hChd->canControllerBase, tmp | PCIEFD_MOD_RM_MSK);

    // Make sure that unit is in reset mode before starting flush
    loopmax = 100;
    stat = waitForIdleMode(loopmax, vChd);

    if (stat < 0) {
        tmp = IORD_PCIEFD_STAT(hChd->canControllerBase);

        DEBUGPRINT(3, "Timeout when waiting for idle state:\n");
        DEBUGPRINT(3, " - reset mode commanded(%c) tx idle(%c) in reset mode(%c) [BUS OFF]\n",
                   statResetRequest(hChd->canControllerBase) ? 'x' : ' ',
                   statTransmitterIdle(hChd->canControllerBase) ? 'x' : ' ',
                   statInResetMode(hChd->canControllerBase) ? 'x' : ' ');
    }

    // Disable bus load packets
    IOWR_PCIEFD_BLP(hChd->canControllerBase, 0);

    KCAN_LED_OFF(hChd->canControllerBase);

    spin_unlock_irqrestore(&hChd->lock, irqFlags);

    complete(&hChd->busOnCompletion);

    pciefd_req_chipstate(vChd);

    return VCAN_STAT_OK;
} // pciefd_bus_off

#define ENABLE_ALL_INTERRUPTS(c)               \
    (((PciCanCardData *)((c)->hwCardData))     \
         ->driver_data->ops->pci_irq_set_mask( \
             (c), ((PciCanCardData *)((c)->hwCardData))->driver_data->irq_def->all_irq))
#define DISABLE_ALL_INTERRUPTS(c) \
    (((PciCanCardData *)((c)->hwCardData))->driver_data->ops->pci_irq_set_mask((c), 0U))

//======================================================================
// Process status packet information
//======================================================================
static void updateChipState(VCanChanData *vChd, struct kcan_packet_hdr *packet)
{
    PciCanChanData *hChd = vChd->hwChanData;

    vChd->chipState.txerr = getTransmitErrorCount(packet);
    vChd->chipState.rxerr = getReceiveErrorCount(packet);

    if (statusBusOff(packet) && statusErrorPassive(packet)) {
        DEBUGPRINT(5, "Is bus off due to tx error == 256 (BOFF=1), and Error Passive\n");

        if ((vChd->chipState.state & CHIPSTAT_BUSOFF) == 0) {
            led_trigger_event(hChd->ledtrig.can_error, LED_FULL);
            led_trigger_event(hChd->ledtrig.bus_on, LED_OFF);
        }

        vChd->chipState.state = CHIPSTAT_BUSOFF | CHIPSTAT_ERROR_PASSIVE | CHIPSTAT_ERROR_WARNING;
    } else if (statusBusOff(packet)) {
        DEBUGPRINT(5, "Is bus off due to tx error == 256 (BOFF=1)\n");

        if ((vChd->chipState.state & CHIPSTAT_BUSOFF) == 0) {
            led_trigger_event(hChd->ledtrig.can_error, LED_FULL);
            led_trigger_event(hChd->ledtrig.bus_on, LED_OFF);
        }

        vChd->chipState.state = CHIPSTAT_BUSOFF;
    } else if (statusInResetMode(packet)) {
        DEBUGPRINT(5, "Is bus off in reset mode (BOFF=0)\n");

        if ((vChd->chipState.state & CHIPSTAT_BUSOFF) == 0) {
            led_trigger_event(hChd->ledtrig.bus_on, LED_OFF);
        }

        vChd->chipState.state = CHIPSTAT_BUSOFF;
    } else if (statusErrorPassive(packet)) {
        DEBUGPRINT(5, "Error Passive Limit Reached\n");

        if ((vChd->chipState.state & CHIPSTAT_ERROR_PASSIVE) == 0) {
            led_trigger_event(hChd->ledtrig.can_error, LED_FULL);
        }

        vChd->chipState.state = CHIPSTAT_ERROR_PASSIVE | CHIPSTAT_ERROR_WARNING;
    }
    // Warning level is disabled. See FB 16545 for info.
    /* else if ( statusErrorWarning(packet) ) { */
    /*   DEBUGPRINT(5,"Error Warning Limit Reached\n"); */
    /*   vChd->chipState.state = CHIPSTAT_ERROR_WARNING; */
    /* } */
    else {
        DEBUGPRINT(5, "Bus on\n");

        if ((vChd->chipState.state & CHIPSTAT_ERROR_ACTIVE) == 0) {
            led_trigger_event(hChd->ledtrig.can_error, LED_OFF);
            led_trigger_event(hChd->ledtrig.bus_on, LED_FULL);
        }

        vChd->chipState.state = CHIPSTAT_ERROR_ACTIVE;
    }

    if (statusOverflow(packet)) {
        DEBUGPRINT(5, "Receive Buffer Overrun Error\n");

        if (!hChd->overrun_occured) {
            led_trigger_event(hChd->ledtrig.buffer_overrun, LED_FULL);
        }

        hChd->overrun_occured = 1;
    }
}

//======================================================================
//  Timeout handler for the waitResponse below
//======================================================================
#if (LINUX_VERSION_CODE < KERNEL_VERSION(4, 15, 0))
static void errorRestart(unsigned long data)
{
    PciCanChanData *hChd = (PciCanChanData *)data;
#else
static void errorRestart(struct timer_list *t)
{
    PciCanChanData *hChd = timer_container_of(hChd, t, errorDisableTimer);
#endif /* KERNEL_VERSION < 4.15.0 */
    unsigned long irqFlags;

    spin_lock_irqsave(&hChd->lock, irqFlags);

    enableErrorPackets(hChd->canControllerBase);

    spin_unlock_irqrestore(&hChd->lock, irqFlags);

    hChd->vChan->errorCount = 0;

    DEBUGPRINT(4, "Reenable error packets CH:%u\n", hChd->vChan->channel);
}

//======================================================================
//
//======================================================================
static int receivedPacketHandler(VCanCardData *vCard, uint32_t size, uint32_t packetBuf[])
{
    unsigned long irqFlags;
    VCAN_EVENT e;
    PciCanCardData *hCard = vCard->hwCardData;
    VCanChanData *vChd;
    PciCanChanData *hChd;
    struct kcan_packet_hdr hdr;
    unsigned int chid;
    unsigned int type;
    uint64_t timestamp;

    memcpy(&hdr, packetBuf, sizeof hdr);
    memcpy(&timestamp, packetBuf + KCAN_NR_OF_HDR_WORDS, sizeof timestamp);
    chid = packetChannelId(&hdr);
    type = RPACKET_PTYPE_GET(hdr.control);


    if (unlikely(chid >= vCard->nrChannels)) {
        DEBUGPRINT(1, "Invalid channel id %u\n", chid);
        return VCAN_STAT_FAIL;
    }

    vChd = vCard->chanData[chid];
    hChd = vChd->hwChanData;

    switch (type) {
        // +----------------------------------------------------------------------
        // | Status packet
        // +----------------------------------------------------------------------
    case RPACKET_PTYPE_STATUS:
    {
        // Copy command and wakeup those who are waiting for this reply.
        struct list_head *currHead, *tmpHead;
        WaitNode *currNode;

        if (unlikely(size != KCAN_NR_OF_RX_HDR_WORDS))
            goto bad_size;

        DEBUGPRINT(4, "CH:%u Status (%x)\n", chid, hdr.id);

        updateChipState(vChd, &hdr);

        e.tag = V_CHIP_STATE;
        e.timeStamp = timestamp_to_ticks(vChd, timestamp);
        e.transId = 0;
        e.tagData.chipState.busStatus = (unsigned char)vChd->chipState.state;
        e.tagData.chipState.txErrorCounter = (unsigned char)vChd->chipState.txerr;
        e.tagData.chipState.rxErrorCounter = (unsigned char)vChd->chipState.rxerr;
        vCanDispatchEvent(vChd, &e);

        read_lock_irqsave(&hCard->replyWaitListLock, irqFlags);

        // TODO Either use list_for_each or remove completed entries here
        list_for_each_safe(currHead, tmpHead, &hCard->replyWaitList) {
            currNode = list_entry(currHead, WaitNode, list);
            if (currNode->cmdNr == chid) {
                unsigned int seqNo = statusCmdSeqNo(&hdr);

                if (seqNo == currNode->transId) {
                    complete(&currNode->waitCompletion);
                    DEBUGPRINT(4, "Matching TID\n");
                } else {
                    int diff = seqNo - currNode->transId;
                    DEBUGPRINT(4, "Not Matching TID: got %u, expected %u diff:%d\n",
                        seqNo, currNode->transId, diff);
                }
            }
        }

        read_unlock_irqrestore(&hCard->replyWaitListLock, irqFlags);
    }
    break;

    // +----------------------------------------------------------------------
    // | Error packet
    // +----------------------------------------------------------------------
    case RPACKET_PTYPE_ERROR:
    {
        VCAN_EVENT e = { 0 };
        int error_code = hdr.control &
                         (EPACKET_TYPE_MSK | EPACKET_SEG_MSK | EPACKET_SEG2_MSK | EPACKET_DIR_MSK);

        if (unlikely(size != KCAN_NR_OF_RX_HDR_WORDS))
            goto bad_size;

        updateChipState(vChd, &hdr);

        vChd->errorCount++;

        DEBUGPRINT(4, "CH:%u Error status: Txerr(%u) Rxerr(%u)\n", chid, vChd->chipState.txerr,
                   vChd->chipState.rxerr);
        DEBUGPRINT(4, "CH:%u ErrorCount(%u)\n", chid, vChd->errorCount);

        if (vChd->errorCount == MAX_ERROR_COUNT) {
            spin_lock_irqsave(&hChd->lock, irqFlags);
            disableErrorPackets(hChd->canControllerBase);
            spin_unlock_irqrestore(&hChd->lock, irqFlags);

            DEBUGPRINT(4, "Disable error packets CH:%u\n", chid);

            // Set a timer to restart interrupts
#if (LINUX_VERSION_CODE < KERNEL_VERSION(4, 15, 0))
            init_timer(&hChd->errorDisableTimer);
            hChd->errorDisableTimer.function = errorRestart;
            hChd->errorDisableTimer.data = (unsigned long)hChd;
#else
            timer_setup(&hChd->errorDisableTimer, errorRestart, 0);
#endif /* KERNEL_VERSION < 4.15.0 */
            hChd->errorDisableTimer.expires = jiffies + msecs_to_jiffies(ERROR_DISABLE_TIME_MS);
            add_timer(&hChd->errorDisableTimer);
        } else if (vChd->errorCount < MAX_ERROR_COUNT) {
            int e_type = EPACKET_TYPE_GET(hdr.control);
            int e_seg  = EPACKET_SEG_GET(hdr.control);
            int e_loc = EPACKET_SEG2_GET(hdr.control);
            unsigned char *err_prot_type = e.tagData.msg.data + 2;
            unsigned char *err_prot_loc  = e.tagData.msg.data + 3;

            DEBUGPRINT(5, "Bus error code: 0x%02x\n isErrorPassive:%u\n", error_code,
                       (vChd->chipState.state & CHIPSTAT_ERROR_PASSIVE) != 0);

            e.tag = V_RECEIVE_MSG;
            e.timeStamp = timestamp_to_ticks(vChd, timestamp);
            e.transId = 0;

            e.tagData.msg.flags = VCAN_MSG_FLAG_ERROR_FRAME;
            e.tagData.msg.dlc = CAN_ERR_DLC;

            /* Borrow the error flags used by SocketCAN for CAN ID 
             * Special case with ACK error */
            e.tagData.msg.id = CAN_ERR_FLAG | CAN_ERR_PROT | CAN_ERR_BUSERROR | CAN_ERR_CNT;

            /* Encode in data byte 2 like SocketCAN (/include/uapi/linux/can/error.h). */
            *err_prot_loc = CAN_ERR_PROT_LOC_UNSPEC;
            switch (e_type) {
                case E_BIT:
                case E_BIT_SSP:
                    *err_prot_type = CAN_ERR_PROT_BIT;
                    break;
                case E_STUFF_ARB:
                case E_STUFF: 
                    *err_prot_type = CAN_ERR_PROT_STUFF;
                    break;
                case E_FORM:
                    *err_prot_type = CAN_ERR_PROT_FORM;
                    break;
                case E_ACK:
                    e.tagData.msg.id |= CAN_ERR_ACK;
                    *err_prot_loc = CAN_ERR_PROT_LOC_ACK;
                    break;
                case E_CRC:
                    *err_prot_type |= CAN_ERR_PROT_BIT;
                    *err_prot_loc = CAN_ERR_PROT_LOC_CRC_SEQ;
                    break;
                default:
                    /* Could we have E_OK? */
                    break;
            }
            switch (e_seg) {
                case ESEG_EXT_ID:
                    /* 5 + 8 + 5 = 18 bits */
                    if (e_loc <= 5)
                        *err_prot_loc = CAN_ERR_PROT_LOC_ID17_13;
                    else if (e_loc <= 13)
                        *err_prot_loc = CAN_ERR_PROT_LOC_ID12_05;
                    else if (e_loc <= 18)
                        *err_prot_loc = CAN_ERR_PROT_LOC_ID04_00;
                    break;
                case ESEG_CRC_SEQ:
                        *err_prot_loc = CAN_ERR_PROT_LOC_CRC_SEQ;
                    break;
                case ESEG_BASE_ID:
                    /* First 8 + 3 = 11 bits in 0..10 */
                    if (e_loc <= 7)
                        *err_prot_loc = CAN_ERR_PROT_LOC_ID28_21;
                    else if (e_loc <= 10)
                        *err_prot_loc = CAN_ERR_PROT_LOC_ID20_18;
                    break;
                case ESEG_DLC:
                    *err_prot_loc = CAN_ERR_PROT_LOC_DLC;
                    break;
                case ESEG_EOF:
                    *err_prot_loc = CAN_ERR_PROT_LOC_EOF;
                    break;
                case ESEG_OVERLOAD:
                case ESEG_ACTIVE_ERROR:
                case ESEG_PASSIVE_ERROR:
                case ESEG_ERROR_DELIM:
                    break;
                case ESEG_IM:
                    *err_prot_loc = CAN_ERR_PROT_LOC_INTERM;
                    break;
                case ESEG_STUFF_CNT:
                    break;
                case ESEG_OTHER:
                    if (e_loc == ESEG_SOF)
                        *err_prot_loc = CAN_ERR_PROT_LOC_SOF;
                    else if (e_loc == ESEG_SRR_RTR)
                        *err_prot_loc = CAN_ERR_PROT_LOC_SRTR;
                    else if (e_loc == ESEG_IDE)
                        *err_prot_loc = CAN_ERR_PROT_LOC_IDE;
                    else if (e_loc == ESEG_RTR)
                        *err_prot_loc = CAN_ERR_PROT_LOC_RTR;
                    else if ((e_loc == ESEG_R0) || (e_loc ==  ESEG_FD_R0))
                        *err_prot_loc = CAN_ERR_PROT_LOC_RES0;
                    else if (e_loc == ESEG_R1)
                        *err_prot_loc = CAN_ERR_PROT_LOC_RES1;
                    else if (e_loc == ESEG_BRS)
                        *err_prot_loc = CAN_ERR_PROT_LOC_UNSPEC;
                    else if (e_loc == ESEG_ESI)
                        *err_prot_loc = CAN_ERR_PROT_LOC_UNSPEC;
                    else if (e_loc == ESEG_DATA)
                        *err_prot_loc = CAN_ERR_PROT_LOC_DATA;
                    else if (e_loc == ESEG_CRC_DELIM)
                        *err_prot_loc = CAN_ERR_PROT_LOC_CRC_DEL;
                    else if (e_loc == ESEG_ACK_SLOT)
                        *err_prot_loc = CAN_ERR_PROT_LOC_ACK;
                    else if (e_loc == ESEG_ACK_DELIM)
                        *err_prot_loc = CAN_ERR_PROT_LOC_ACK_DEL;
                    break;
            }

            if (hdr.control & EPACKET_DIR_MSK)
                *err_prot_type |= CAN_ERR_PROT_TX;

            e.tagData.msg.data[6] = vChd->chipState.txerr;
            e.tagData.msg.data[7] = vChd->chipState.rxerr;
            vCanDispatchEvent(vChd, &e);
        } else {
            DEBUGPRINT(5, "Error status blocked\n");
        }
    }
    break;

    // +----------------------------------------------------------------------
    // | TXACK | Error frame ack
    // +----------------------------------------------------------------------
    case RPACKET_PTYPE_ACK:
    case RPACKET_PTYPE_EFRAME_ACK:
    {
        unsigned int transId = getAckSeqNo(&hdr);
        int isAck = (type == RPACKET_PTYPE_ACK);

        if (unlikely(size != KCAN_NR_OF_RX_HDR_WORDS))
            goto bad_size;

        if (isAck) {
            DEBUGPRINT(4, "CH:%u Ack Packet, ID %u, Outstanding %d\n", chid, transId,
                       atomic_read(&hChd->outstanding_tx));
        } else {
            DEBUGPRINT(4, "CH:%u Error frame ack Packet, ID %u, Outstanding %d\n", chid, transId,
                       atomic_read(&hChd->outstanding_tx));
        }

        if (isFlushed(&hdr)) // Got flushed packet, do nothing
        {
            DEBUGPRINT(4, "Ack was flushed, ID %u\n", transId);
        } else if ((transId == 0) || (transId > hCard->max_outstanding_tx)) {
            DEBUGPRINT(3, "CMD_TX_ACKNOWLEDGE chan %d ERROR transid %d\n", chid, transId);
        } else {
            VCAN_EVENT *e = (VCAN_EVENT *)&hChd->current_tx_message[transId - 1];
            e->tag = V_RECEIVE_MSG;
            e->timeStamp = timestamp_to_ticks(vChd, timestamp);
            e->tagData.msg.flags &= ~VCAN_MSG_FLAG_TXRQ;
            e->tagData.msg.flags |= VCAN_MSG_FLAG_TXACK;

            if (isAck && isNack(&hdr)) {
                if (isABL(&hdr)) {
                    e->tagData.msg.flags |= VCAN_MSG_FLAG_SSM_NACK_ABL;
                } else {
                    e->tagData.msg.flags |= VCAN_MSG_FLAG_SSM_NACK;
                }
            }

            vCanDispatchEvent(vChd, e);

            hChd->current_tx_message[transId - 1].user_data = 0;

            // Wake up those who are waiting for all sending to finish
            if (atomic_add_unless(&hChd->outstanding_tx, -1, 0)) {
                // Is anyone waiting for this ack?
                if ((atomic_read(&hChd->outstanding_tx) == 0) && queue_empty(&vChd->txChanQueue) &&
                    test_and_clear_bit(0, &vChd->waitEmpty)) {
                    wake_up_interruptible(&vChd->flushQ);
                }
                if (!queue_empty(&vChd->txChanQueue)) {
                    pciefd_req_tx(vCard, vChd);
                } else {
                    DEBUGPRINT(4, "Queue empty\n");
                }
            } else {
                DEBUGPRINT(4, "TX ACK when not waiting for one\n");
            }
        }

        led_trigger_blink_oneshot(hChd->ledtrig.can_activity,
                              &EXT_LED_BLINK_DELAY_ms,
                              &EXT_LED_BLINK_DELAY_ms, false);
    }
    break;

    // +----------------------------------------------------------------------
    // | TXRQ Packet
    // +----------------------------------------------------------------------
    case RPACKET_PTYPE_TXRQ:
    {
        unsigned int transId = getTxrqSeqNo(&hdr);

        if (unlikely(size != KCAN_NR_OF_RX_HDR_WORDS))
            goto bad_size;

        DEBUGPRINT(3, "### Txrq packet : chan(%u) seq no(%u)\n", chid, transId);

        // A TxReq. Take the current tx message, modify it to a
        // receive message and send it back.

        if ((transId == 0) || (transId > hCard->max_outstanding_tx)) {
            DEBUGPRINT(3, "CMD_TX_REQUEST chan %d ERROR transid to high %d\n", chid, transId);
        } else {
            if (hChd->current_tx_message[transId - 1].flags & VCAN_MSG_FLAG_TXRQ) {
                VCAN_EVENT *e = (VCAN_EVENT *)&hChd->current_tx_message[transId - 1];
                e->tag = V_RECEIVE_MSG;
                e->timeStamp = timestamp_to_ticks(vChd, timestamp);
                e->tagData.msg.flags &= ~VCAN_MSG_FLAG_TXACK;
                vCanDispatchEvent(vChd, e);
            }
        }
    }
    break;

    // +----------------------------------------------------------------------
    // | Data packet
    // +----------------------------------------------------------------------
    case RPACKET_PTYPE_DATA:
    {
        uint32_t *data = packetBuf + KCAN_NR_OF_RX_HDR_WORDS;
        int id = getId(&hdr);
        unsigned short int flags = 0;
        unsigned int dlc = getDLC(&hdr);
        unsigned int nbytes;

        if (isFlexibleDataRateFormat(&hdr)) {
            flags |= VCAN_MSG_FLAG_FDF;
            DEBUGPRINT(4, "Received CAN FD frame\n");

            if (isAlternateBitRate(&hdr)) {
                flags |= VCAN_MSG_FLAG_BRS;
            }
            if (errorStateIndicated(&hdr)) {
                flags |= VCAN_MSG_FLAG_ESI;
            }

            nbytes = dlcToBytesFD(dlc);
        } else {
            nbytes = dlcToBytes(dlc);
        }

        if (isRemoteRequest(&hdr)) {
            flags |= VCAN_MSG_FLAG_REMOTE_FRAME;
            nbytes = 0;
        }

        if (unlikely(size != KCAN_NR_OF_RX_HDR_WORDS + bytesToWordsCeil(nbytes)))
            goto bad_size;

        DEBUGPRINT(4, "### Data packet : chan(%u) dlc(%u) ext(%i) rem(%i) seq no(%u)\n", chid,
            dlc, isExtendedId(&hdr), isRemoteRequest(&hdr), getSeqNo(&hdr));

        if (errorWarning(&hdr)) {
            DEBUGPRINT(4, "### Error Warning Limit Reached\n");
        }

        if (isErrorPassive(&hdr)) {
            DEBUGPRINT(4, "### Error Passive Limit Reached\n");

            led_trigger_event(hChd->ledtrig.can_error, LED_FULL);
        }

        if (receiverOverflow(&hdr)) {
            DEBUGPRINT(3, "### Receive Buffer Overrun Error\n");

            if (!hChd->overrun_occured) {
                led_trigger_event(hChd->ledtrig.buffer_overrun, LED_FULL);
            }

            hChd->overrun_occured = 1;
        }

        if (isExtendedId(&hdr)) {
            id |= VCAN_EXT_MSG_ID;
        }

        if (hChd->overrun_occured) {
            flags |= VCAN_MSG_FLAG_OVERRUN;
            hChd->overrun_occured = 0;
        }

        e.tag = V_RECEIVE_MSG;
        e.timeStamp = timestamp_to_ticks(vChd, timestamp);
        e.transId = 0;
        e.tagData.msg.id = id;
        e.tagData.msg.flags = flags;
        e.tagData.msg.dlc = dlc & 0x0f;

        memcpy(e.tagData.msg.data, data, nbytes);

        vCanDispatchEvent(vChd, &e);

        if (vChd->errorCount > 0) {
            spin_lock_irqsave(&hChd->lock, irqFlags);
            vChd->errorCount = 0;
            spin_unlock_irqrestore(&hChd->lock, irqFlags);
            led_trigger_event(hChd->ledtrig.can_error, LED_OFF);
        }

        led_trigger_blink_oneshot(hChd->ledtrig.can_activity,
                              &EXT_LED_BLINK_DELAY_ms,
                              &EXT_LED_BLINK_DELAY_ms, false);
    }
    break;

    // +----------------------------------------------------------------------
    // | Bus Load packet
    // +----------------------------------------------------------------------
    case RPACKET_PTYPE_BUS_LOAD:
        if (unlikely(size != KCAN_NR_OF_RX_HDR_WORDS))
            goto bad_size;

#if BUS_LOAD_DBG_MSG_DIVISOR
        {
            static u32 count = 0;

            if (++count == BUS_LOAD_DBG_MSG_DIVISOR) {
                DEBUGPRINT(4, "+%u Bus Load\n", count);
                count = 0;
            }
        }
#else
        DEBUGPRINT(4, "Bus Load\n");
#endif

        hChd->load = getBusLoad(&hdr) / BLP_DIVISOR;
        break;

    // +----------------------------------------------------------------------
    // | End of flush ack packet
    // +----------------------------------------------------------------------
    case RPACKET_PTYPE_EFLUSH_ACK:
        if (unlikely(size != KCAN_NR_OF_RX_HDR_WORDS))
            goto bad_size;

        // Do nothing
        DEBUGPRINT(4, "CH:%u End of flush ack\n", chid);

        if (!isFlushed(&hdr)) {
            DEBUGPRINT(3, "End of flush ack when device was not in flush mode\n");
        }
        break;

    default:
        DEBUGPRINT(3, "Unknown packet CH:%u. id = %#08x, ctl = %#08x\n",
            chid, hdr.id, hdr.control);
        break;
    }
    return VCAN_STAT_OK;

bad_size:
    DEBUGPRINT(1, "Error: Bad packet size CH:%u. type = %u, size = %u\n",
      chid, type, size);
    return VCAN_STAT_FAIL;
}

//======================================================================
//  Timeout handler for the waitResponse below
//======================================================================
#if (LINUX_VERSION_CODE < KERNEL_VERSION(4, 15, 0))
static void interruptRestart(unsigned long data)
{
    VCanCardTimerData *ir = (VCanCardTimerData *)data;
#else
static void interruptRestart(struct timer_list *t)
{
    VCanCardTimerData *ir = timer_container_of(ir, t, timer);
#endif /* KERNEL_VERSION < 4.15.0 */
    VCanCardData *vCard = ir->vCard;
    int i;
    for (i = 0; i < (unsigned)vCard->nrChannels; i++) {
        VCanChanData *vChd = vCard->chanData[i];
#if LED_TRIGGERS
        PciCanChanData *hChd = vChd->hwChanData;
#endif
        DEBUGPRINT(3, "Reset error count %u txerr %u rxerr %u\n", vChd->errorCount,
                   (unsigned char)vChd->chipState.txerr, (unsigned char)vChd->chipState.rxerr);
        vChd->errorCount = 0;
        led_trigger_event(hChd->ledtrig.can_error, LED_OFF);
    }

    DEBUGPRINT(3, "Reenable interrupt\n");
    ENABLE_ALL_INTERRUPTS(vCard);
}

//======================================================================
//  Interrupt handling functions
//  Must be called with channel locked (to avoid access interference).
//  * Will be called without lock as all the receive FIFO accesses are card
//    specific and are accessed only from the interrupt context.
//======================================================================
static void pciefd_kcan_rx_interrupt(VCanCardData *vCard)
{
    PciCanCardData *hCard = vCard->hwCardData;
    VCanCardTimerData timerData;
    // Limit absolute number of loops
    unsigned int loopMax = 10000;
    unsigned long irqFlags;
    unsigned int irqHandled = 0;
    unsigned int irq;

    while (1) {
        union {
            struct kcan_rx_packet x;
            uint32_t buf[sizeof(struct kcan_rx_packet) / sizeof(uint32_t)];
        } packet;
        int packetSize = 0;

        // A loop counter as a safety measure.
        // NOTE: Returning from within the pciefd_kcan_rx_interrupt main loop will result in interrupts being lost.
        if (--loopMax == 0) {
            int i;
            DEBUGPRINT(3, "%s: Loop counter as a safety measure!!!\n", __func__);
            DEBUGPRINT(3, "During test, shutting down card!\n");
            for (i = 0; i < (unsigned)vCard->nrChannels; i++) {
                VCanChanData *vChd = vCard->chanData[i];
                PciCanChanData *hChd = vChd->hwChanData;

                DEBUGPRINT(3, "Errorcount %u\n", vChd->errorCount);

                DEBUGPRINT(3, "ch%u IEN:%08x IRQ:%08x\n", i,
                           IORD_PCIEFD_IEN(hChd->canControllerBase),
                           IORD_PCIEFD_IRQ(hChd->canControllerBase));
            }
            DISABLE_ALL_INTERRUPTS(vCard);

            // Set a timer to restart interrupts
            timerData.vCard = vCard;
#if (LINUX_VERSION_CODE < KERNEL_VERSION(4, 15, 0))
            init_timer(&timerData.timer);
            timerData.timer.function = interruptRestart;
            timerData.timer.data = (unsigned long)&timerData;
#else
            timer_setup(&timerData.timer, interruptRestart, 0);
#endif /* KERNEL_VERSION < 4.15.0 */
            timerData.timer.expires = jiffies + msecs_to_jiffies(100);
            add_timer(&timerData.timer);

            break;
        }

        spin_lock_irqsave(&hCard->lock, irqFlags);

        irq = fifoIrqStatus(hCard->io.kcan.rxCtrlBase);

        if (irq & RXBUF_IRQ_RA_MSK) {
#if PCIEFD_USE_DMA
            if (hCard->useDma && hCard->dmaCtx.enabled) {
                DEBUGPRINT(3, "Warning:FIFO direct access\n");
            }
#endif /* PCIEFD_USE_DMA */
            // +----------------------------------------------------------------------
            // | Read from FIFO
            // | The lock could probably be removed as the FIFO is only accessed from
            // | this point.
            // +----------------------------------------------------------------------
            // 2. Store one packet for each channel with data at beginning of interrupt
            if ((packetSize = readFIFO(vCard, &packet.x)) < 0) {
                uint32_t tmp = IORD_RXBUF_CTRL(hCard->io.kcan.rxCtrlBase);
                DEBUGPRINT(3, "readFIFO error\n");

#if PCIEFD_USE_DMA
                if (hCard->useDma)
                    DEBUGPRINT(3, "IOC:%x EN:%u\n", tmp, hCard->dmaCtx.enabled);
#endif /* PCIEFD_USE_DMA */
                if (tmp & RXBUF_CTRL_DMAEN_MSK) {
                    DEBUGPRINT(3, "DMAEN\n");
                    tmp = IORD_RXBUF_IEN(hCard->io.kcan.rxCtrlBase);
                    IOWR_RXBUF_IEN(hCard->io.kcan.rxCtrlBase, tmp & ~RXBUF_IRQ_RA_MSK);
                }
            }

            // Try to clear receieved interrupt (will not have any effect if more packets are available)
            irqHandled = RXBUF_IRQ_RA_MSK;
        }
#if PCIEFD_USE_DMA
        // DMA Interrupts
        else if (irq & RXBUF_IRQ_DMA_DONE0_MSK) {
            irqHandled |= RXBUF_IRQ_DMA_DONE0_MSK;
            pciefd_aquire_dma_buffer(vCard, 0);
        } else if (irq & RXBUF_IRQ_DMA_DONE1_MSK) {
            irqHandled |= RXBUF_IRQ_DMA_DONE1_MSK;
            pciefd_aquire_dma_buffer(vCard, 1);
        }
#endif /* PCIEFD_USE_DMA */

        spin_unlock_irqrestore(&hCard->lock, irqFlags);

#if PCIEFD_USE_DMA
        if (irq & RXBUF_IRQ_DMA_OVF0_MSK) {
            irqHandled |= RXBUF_IRQ_DMA_OVF0_MSK;

            DEBUGPRINT(3, "DMA OVF 0\n");

            if (hCard->dmaCtx.active == 0) {
                DEBUGPRINT(3, "DMA restart\n");
                hCard->dmaCtx.active = -1;
            }
        }

        if (irq & RXBUF_IRQ_DMA_OVF1_MSK) {
            irqHandled |= RXBUF_IRQ_DMA_OVF1_MSK;

            DEBUGPRINT(3, "DMA OVF 1\n");

            if (hCard->dmaCtx.active == 1) {
                DEBUGPRINT(3, "DMA restart\n");
                hCard->dmaCtx.active = -1;
            }
        }

        if (irq & RXBUF_IRQ_DMA_UNF0_MSK) {
            irqHandled |= RXBUF_IRQ_DMA_UNF0_MSK;

            DEBUGPRINT(3, "DMA UNF 0\n");
        }

        if (irq & RXBUF_IRQ_DMA_UNF1_MSK) {
            irqHandled |= RXBUF_IRQ_DMA_UNF1_MSK;

            DEBUGPRINT(3, "DMA UNF 1\n");
        }

        if ((irq & RXBUF_IRQ_DMA_DONE0_MSK) && (irq & RXBUF_IRQ_DMA_DONE1_MSK)) {
            DEBUGPRINT(3, "Warning: Both buffer signals ready status\n");
        }
#endif /* PCIEFD_USE_DMA */

        if (irq & RXBUF_IRQ_UNDERFLOW_MSK) {
            DEBUGPRINT(3, "RXBUF_IRQ_UNDERFLOW\n");
            irqHandled |= RXBUF_IRQ_UNDERFLOW_MSK;
        }

        if (irq & RXBUF_IRQ_UNALIGNED_READ_MSK) {
            DEBUGPRINT(3, "RXBUF_IRQ_UNALIGNED_READ\n");
            irqHandled |= RXBUF_IRQ_UNALIGNED_READ_MSK;
        }

        if (irq & RXBUF_IRQ_MISSING_TAG_MSK) {
            DEBUGPRINT(3, "RXBUF_IRQ_MISSING_TAG\n");
            irqHandled |= RXBUF_IRQ_MISSING_TAG_MSK;
        }

        fifoIrqClear(hCard->io.kcan.rxCtrlBase, irqHandled);

        if ((irq & ~irqHandled) != 0) {
            DEBUGPRINT(3, "Unhandled interrupt %x\n", irq & ~irqHandled);
            fifoIrqClear(hCard->io.kcan.rxCtrlBase, irq & ~irqHandled);
        }

        if (packetSize > 0) {
            receivedPacketHandler(vCard, packetSize, packet.buf);
        }
#if PCIEFD_USE_DMA
        else if (hCard->useDma && (hCard->dmaCtx.active >= 0)) {
            int bufid = hCard->dmaCtx.active;
            uint32_t *buf = hCard->dmaCtx.bufferCtx[bufid].data,
                *p,
                size;

            for (p = buf; (size = *p); p += size) {
                if (unlikely((buf + KCAN_DMA_BUFFER_SZ) - p < size)) {
                    DEBUGPRINT(1, "Error: DMA buffer pointer wraparound\n");
                    break;
                }
                if (receivedPacketHandler(vCard, size - 1, p + 1))
                    break;
            }

            // Buffer is empty, release to device and rearm DMA
            pciefd_release_dma_buffer(vCard, bufid);
            if (bufid == 0)
                armDMA0(hCard->io.kcan.rxCtrlBase);
            else if (bufid == 1)
                armDMA1(hCard->io.kcan.rxCtrlBase);

            break;
        }
#endif
        else
            break;
    }
} // pciefd_kcan_rx_interrupt

//======================================================================
//  Transmit interrupt handler
//  Must be called with channel locked (to avoid access interference).
//======================================================================
static void pciefd_kcan_tx_interrupt(VCanChanData *vChd)
{
    PciCanChanData *hChd = vChd->hwChanData;
    unsigned int irq;
    unsigned int irqHandled = 0;

    irq = irqStatus(hChd->canControllerBase);

    if (irq & PCIEFD_IRQ_TAR_MSK) {
        DEBUGPRINT(3, "PCIEFD_IRQ_TAR_MSK\n");
        irqHandled |= PCIEFD_IRQ_TAR_MSK;
    }

    if (irq & PCIEFD_IRQ_TAE_MSK) {
        DEBUGPRINT(3, "PCIEFD_IRQ_TAE_MSK\n");
        irqHandled |= PCIEFD_IRQ_TAE_MSK;
    }

    if (irq & PCIEFD_IRQ_BPP_MSK) {
        DEBUGPRINT(3, "PCIEFD_IRQ_BPP_MSK\n");
        irqHandled |= PCIEFD_IRQ_BPP_MSK;
    }

    if (irq & PCIEFD_IRQ_FDIC_MSK) {
        DEBUGPRINT(3, "PCIEFD_IRQ_FDIC_MSK\n");
        irqHandled |= PCIEFD_IRQ_FDIC_MSK;
    }

    if (irq & PCIEFD_IRQ_TOF_MSK) {
        DEBUGPRINT(3, "PCIEFD_IRQ_TOF_MSK\n");
        irqHandled |= PCIEFD_IRQ_TOF_MSK;
    }

    if (irq & PCIEFD_IRQ_TAL_MSK) {
        DEBUGPRINT(3, "PCIEFD_IRQ_TAL_MSK\n");
        irqHandled |= PCIEFD_IRQ_TAL_MSK;
    }

    irqClear(hChd->canControllerBase, irqHandled);

    led_trigger_blink_oneshot(hChd->ledtrig.can_activity,
                            &EXT_LED_BLINK_DELAY_ms,
                            &EXT_LED_BLINK_DELAY_ms, false);
} // pciefd_kcan_tx_interrupt

//======================================================================
//  Main ISR
//======================================================================
static irqreturn_t pciefd_pci_interrupt(int irq, void *dev_id)
{
    VCanCardData *vCard = (VCanCardData *)dev_id;
    PciCanCardData *hCard;
    unsigned int loopmax = 10000;
    int handled = 0;
    u32 pcie_irq;
    const struct pciefd_irq_defines *irq_def;
    const struct pciefd_card_ops *card_ops;

    if (!vCard) {
        DEBUGPRINT(1, "Unassigned IRQ\n");
        return IRQ_NONE;
    }
    hCard = vCard->hwCardData;
    irq_def = hCard->driver_data->irq_def;
    card_ops = hCard->driver_data->ops;

    // Read interrupt status
    while ((pcie_irq = card_ops->pci_irq_get(hCard)) != 0) {
        int i;

        if (--loopmax == 0) {
            // Kill the card.
            DEBUGPRINT(1, "PCIcan runaway, shutting down card!! (pcie_irq:0x%x)\n",
                       (unsigned int)pcie_irq);

            DISABLE_ALL_INTERRUPTS(vCard);
            return IRQ_HANDLED;
        }

        handled = 1;

        // Handle shared receive buffer interrupt first
        if (pcie_irq & irq_def->rx0)
            pciefd_kcan_rx_interrupt(vCard);

        // Handle interrupts for each can controller
        for (i = 0; i < vCard->nrChannels; i++) {
            VCanChanData *vChd;
            PciCanChanData *hChd;

            vChd = vCard->chanData[i];
            hChd = vChd->hwChanData;

            if (pcie_irq & hChd->tx_irq_msk)
                pciefd_kcan_tx_interrupt(vChd);
        }
    }

    return IRQ_RETVAL(handled);
} // pciefd_pci_interrupt

//======================================================================
//   pciefd_tx_can_msg
//======================================================================
static int pciefd_tx_can_msg(VCanChanData *vChd, CAN_MSG *m)
{
    PciCanChanData *hChd = vChd->hwChanData;
    PciCanCardData *hCard = vChd->vCard->hwCardData;
    unsigned short int flags = m->flags;
    unsigned long irqFlags;
    int transId;
    union {
        uint32_t buf[KCAN_NR_OF_HDR_WORDS + NR_OF_DATA_WORDS];
        struct kcan_packet_hdr hdr;
    } packet;
    unsigned int nbytes;

    if (!atomic_add_unless(&hChd->outstanding_tx, 1, hCard->max_outstanding_tx)) {
        DEBUGPRINT(1, "Trying to increase outstanding_tx above max\n");
        return VCAN_STAT_NO_RESOURCES;
    }

    transId = atomic_read(&vChd->transId);

    if (hChd->current_tx_message[transId - 1].user_data) {
        DEBUGPRINT(2, "Transid is already in use: %x %d   %x %d CH:%u\n\n",
                   hChd->current_tx_message[transId - 1].id, transId, m->id,
                   atomic_read(&hChd->outstanding_tx), vChd->channel);
    }

    hChd->current_tx_message[transId - 1] = *m;

    if (flags & VCAN_MSG_FLAG_ERROR_FRAME) { // Error frame
        spin_lock_irqsave(&hChd->lock, irqFlags);

        DEBUGPRINT(4, "CH:%u Generate Error Frame %u\n", vChd->channel, transId);

        // Write an error frame control word
        IOWR_PCIEFD_CONTROL(hChd->canControllerBase, PCIEFD_CONTROL_ERROR_FRAME | transId);
    } else {
        if (flags & VCAN_MSG_FLAG_FDF) { // CAN FD Format (Extended Data Length)

            DEBUGPRINT(4, "Send CAN FD Frame\n");

            nbytes =
                setupFDBaseFormat(&packet.hdr, m->id & ~VCAN_EXT_MSG_ID, m->length & 0x0f, transId);

            if (flags & VCAN_MSG_FLAG_BRS) {
                setBitRateSwitch(&packet.hdr);
            }
        } else {
            nbytes = setupBaseFormat(&packet.hdr, m->id & ~VCAN_EXT_MSG_ID, m->length & 0x0f, transId);
            if (flags & VCAN_MSG_FLAG_REMOTE_FRAME) {
                setRemoteRequest(&packet.hdr);
                nbytes = 0;
            }
        }

        setAckRequest(&packet.hdr);

        if (flags & VCAN_MSG_FLAG_TXRQ) {
            setTxRequest(&packet.hdr);
        }

        if (m->id & VCAN_EXT_MSG_ID) { /* Extended CAN */
            setExtendedId(&packet.hdr);
        }

        if (flags & VCAN_MSG_FLAG_SINGLE_SHOT) {
            setSingleShotMode(&packet.hdr);
        }

        memcpy(packet.buf + KCAN_NR_OF_HDR_WORDS, m->data, nbytes);

        spin_lock_irqsave(&hChd->lock, irqFlags);

        if (vChd->errorCount > 0) {
            vChd->errorCount = 0;
            led_trigger_event(hChd->ledtrig.can_error, LED_OFF);
        }

        // Write packet to transmit buffer
        // This region must be done locked
        writeFIFO(vChd, 2 + bytesToWordsCeil(nbytes), packet.buf);
    }

    spin_unlock_irqrestore(&hChd->lock, irqFlags);

    // Update transId after we know QCmd() was succesful!
    if (transId + 1 > hCard->max_outstanding_tx) {
        DEBUGPRINT(4, "transId = %d (%px), rewind\n", transId, &vChd->transId);
        atomic_set(&vChd->transId, 1);
    } else {
        DEBUGPRINT(4, "transId = %d (%px), add\n", transId, &vChd->transId);
        atomic_add(1, &vChd->transId);
    }

    return VCAN_STAT_OK;
} // pciefd_tx_can_msg

//======================================================================
//  Read transmit error counter
//======================================================================
static int pciefd_get_tx_err(VCanChanData *vChd)
{
    pciefd_req_chipstate(vChd);
    return vChd->chipState.txerr;
}

//======================================================================
//  Read transmit error counter
//======================================================================
static int pciefd_get_rx_err(VCanChanData *vChd)
{
    pciefd_req_chipstate(vChd);
    return vChd->chipState.rxerr;
}

//======================================================================
//  Read transmit queue length in hardware/firmware
//======================================================================
static unsigned long pciefd_get_tx_queue_level(VCanChanData *vChd)
{
    PciCanChanData *hChd = vChd->hwChanData;

    return atomic_read(&hChd->outstanding_tx);
}

//======================================================================
//  Clear send buffer on card
//======================================================================
static int pciefd_flush_tx_buffer(VCanChanData *vChd)
{
    PciCanChanData *hChd = vChd->hwChanData;
    unsigned long irqFlags;
    int loopcount = 100;

    wait_for_completion(&hChd->busOnCompletion);

    spin_lock_irqsave(&hChd->lock, irqFlags);

    atomic_set(&hChd->outstanding_tx, 0);

    DEBUGPRINT(4, "Note: Flush Transmit fifo CH:%u\n", vChd->channel);

    // Clear tx flush done irq flag
    irqClear(hChd->canControllerBase, PCIEFD_IRQ_TFD_MSK);

    // Flush Transmit HW Buffer
    fpgaFlushTransmit(hChd->canControllerBase);

    spin_unlock_irqrestore(&hChd->lock, irqFlags);

    while (loopcount) {
        if (istatCheck(hChd->canControllerBase, PCIEFD_IRQ_TFD_MSK)) {
            DEBUGPRINT(4, "Tx Flush Done CH:%u\n", vChd->channel);
            break;
        }

        DEBUGPRINT(4, "Tx Flush Busy CH:%u\n", vChd->channel);

        set_current_state(TASK_UNINTERRUPTIBLE);
        schedule_timeout(msecs_to_jiffies(1)); // Wait 1 ms for transceiver to reach bus on

        if (statAbortRequest(hChd->canControllerBase)) {
            DEBUGPRINT(3, "Warning: Abort req is asserted CH:%u\n", vChd->channel);
        }

        if (vChd->vCard->cardPresent) {
            --loopcount;
        } else {
            return VCAN_STAT_OK;
        }
    }

    if (!loopcount) {
        DEBUGPRINT(1, "Tx Flush Still Busy? Loopmax reached CH:%u\n", vChd->channel);
    }

    if (!istatCheck(hChd->canControllerBase, PCIEFD_IRQ_TFD_MSK)) {
        DEBUGPRINT(1, "Tx Flush Still Busy, probably failed initiating CH:%u\n", vChd->channel);
    }

    DEBUGPRINT(4, "Note: Flush Transmit fifo start CH:%u\n", vChd->channel);

    if (statAbortRequest(hChd->canControllerBase)) {
        DEBUGPRINT(1, "Flush: Abort_Req  CH:%u\n", vChd->channel);
    } else if (statFlushRequest(hChd->canControllerBase)) {
        DEBUGPRINT(1, "Flush: Tx_Flush_Req CH:%u\n", vChd->channel);
    }

    loopcount = 10;
    while (loopcount) {
        uint32_t level;

        level = fifoPacketCountTx(hChd->canControllerBase);

        if (level == 0) {
            break;
        }

        set_current_state(TASK_UNINTERRUPTIBLE);
        schedule_timeout(msecs_to_jiffies(1)); // Wait 1 ms

        if (vChd->vCard->cardPresent) {
            --loopcount;
        } else {
            return VCAN_STAT_OK;
        }
    }

    if (loopcount == 0) {
        DEBUGPRINT(
            1,
            "Warning: Transmit buffer is not empty. Probably a failed flush operation [FLUSH]\n");
    }

    // Write a flush recovery control word
    IOWR_PCIEFD_CONTROL(hChd->canControllerBase, PCIEFD_CONTROL_END_FLUSH);

    queue_reinit(&vChd->txChanQueue);
    complete(&hChd->busOnCompletion);
    return VCAN_STAT_OK;
}

//======================================================================
//  Initialize H/W
//  This is only called before a card is initialized, so no one can
//  interfere with the accesses.
//======================================================================
static int pciefd_init_hw(VCanCardData *vCard)
{
    int chNr;
    PciCanCardData *hCard = vCard->hwCardData;
    unsigned long irqFlags;

    // The card must be present!
    if (!vCard->cardPresent) {
        DEBUGPRINT(1, "Error: The card must be present!\n");
        return VCAN_STAT_NO_DEVICE;
    }

    if (!hCard->io.pcieBar0Base) {
        DEBUGPRINT(1, "Error: In address!\n");
        vCard->cardPresent = 0;
        return VCAN_STAT_FAIL;
    }

    for (chNr = 0; chNr < vCard->nrChannels; chNr++) {
        VCanChanData *vChd = vCard->chanData[chNr];
        PciCanChanData *hChd = vChd->hwChanData;
        void __iomem *addr = hChd->canControllerBase;
        uint32_t mode;

        if (!addr) {
            return VCAN_STAT_FAIL;
        }
        DEBUGPRINT(3, "Ch %d IO base address = %px\n", chNr, addr);

        // Is this lock neccessary? See comment in function header.
        spin_lock_irqsave(&hChd->lock, irqFlags);

        // Default 125 kbit/s, 75%
        IOWR_PCIEFD_BTRN(addr, PCIEFD_BTR_SEG2(4 - 1) | PCIEFD_BTR_SEG1(11 - 1) |
                                   PCIEFD_BTR_SJW(2) | PCIEFD_BTR_BRP(32 - 1));

        if (hwSupportCanFD(hChd->canControllerBase)) {
            IOWR_PCIEFD_BTRD(addr, PCIEFD_BTR_SEG2(4 - 1) | PCIEFD_BTR_SEG1(11 - 1) |
                                       PCIEFD_BTR_SJW(2) | PCIEFD_BTR_BRP(32 - 1));
        }

        // Disable bus load calculation
        IOWR_PCIEFD_BLP(addr, 0);

        mode = PCIEFD_MOD_EWL(96) // Set the EWL value
               | PCIEFD_MOD_CHANNEL(chNr) | PCIEFD_MOD_EEN(1) |
               PCIEFD_MOD_EPEN(1) // Error packets are received
               | PCIEFD_MOD_RM_MSK // Reset the circuit.
               | PCIEFD_MOD_DWH(1);

        IOWR_PCIEFD_MOD(addr, mode);

        // Flush and reset
        fpgaFlushAll(hChd->canControllerBase, 0);

        // Disable receiver interrupt
        irqInit(hChd->canControllerBase);

        KCAN_LED_OFF(hChd->canControllerBase);

        spin_unlock_irqrestore(&hChd->lock, irqFlags);
    }

    {
        uint32_t level;

        level = fifoPacketCountRx(hCard->io.kcan.rxCtrlBase);
        if (level) {
            int to;

            DEBUGPRINT(3, "RX FIFO Has %u old packets\n", level);

            to = (level < 200) ? level : 200;

            while (to && level) {
                struct kcan_rx_packet packet;

                // Reset FIFO
                IOWR_RXBUF_CMD(hCard->io.kcan.rxCtrlBase, RXBUF_CMD_RESET_MSK);

                readFIFO(vCard, &packet);

                level = fifoPacketCountRx(hCard->io.kcan.rxCtrlBase);
                --to;
            }
        }
    }

    fifoIrqInit(hCard->io.kcan.rxCtrlBase);
    fifoIrqEnableUnderflow(hCard->io.kcan.rxCtrlBase);

    // Enable interrupts from card
    ENABLE_ALL_INTERRUPTS(vCard);

    DEBUGPRINT(3, DEVICE_NAME_STRING ": hw initialized. \n");

    return VCAN_STAT_OK;
}

//======================================================================
//  Find out addresses for one card
//======================================================================
static int readPCIAddresses(struct pci_dev *dev, VCanCardData *vCard)
{
    PciCanCardData *hCard = vCard->hwCardData;
    int i;

    if (pci_request_regions(dev, "Kvaser PCIe CAN")) {
        DEBUGPRINT(1, "request regions failed\n");
        return VCAN_STAT_FAIL;
    }

    if (pci_enable_device(dev)) {
        DEBUGPRINT(1, "enable device failed\n");
        pci_release_regions(dev);
        return VCAN_STAT_NO_DEVICE;
    }

    for (i = 0; i < 2; i++) {
        unsigned long bar_start = pci_resource_start(dev, i);
        if (bar_start) {
            unsigned long bar_end = pci_resource_end(dev, i);
            unsigned long bar_flags = pci_resource_flags(dev, i);
            DEBUGPRINT(3, "BAR%d 0x%08lx-0x%08lx flags 0x%08lx\n", i, bar_start, bar_end,
                       bar_flags);
            if (bar_flags & IORESOURCE_IO)
                DEBUGPRINT(3, "IO resource flag\n");
            if (bar_flags & IORESOURCE_MEM)
                DEBUGPRINT(3, "Memory resource flag\n");
            if (bar_flags & IORESOURCE_PREFETCH)
                DEBUGPRINT(3, "Prefetch flag\n");
            if (bar_flags & IORESOURCE_READONLY)
                DEBUGPRINT(3, "Readonly flag\n");
        }
    }

    hCard->io.pcieBar0Base = pci_iomap(dev, 0, 0);

    if (hCard->io.pcieBar0Base == NULL) {
        DEBUGPRINT(1, "pci_iomap of bar 0 failed\n");
        pci_disable_device(dev);
        pci_release_regions(dev);
        return VCAN_STAT_FAIL;
    }

    // Init FPGA bus system base addresses
    hCard->io.kcan.metaBase = SUM(hCard->io.pcieBar0Base, hCard->driver_data->offsets.kcan.meta);
    hCard->io.kcan.timeBase = SUM(hCard->io.pcieBar0Base, hCard->driver_data->offsets.kcan.time);
    hCard->io.kcan.kcanIntBase =
        SUM(hCard->io.pcieBar0Base, hCard->driver_data->offsets.kcan.interrupt);
    hCard->io.kcan.rxDataBase =
        SUM(hCard->io.pcieBar0Base, hCard->driver_data->offsets.kcan.rx_data);
    hCard->io.kcan.rxCtrlBase =
        SUM(hCard->io.pcieBar0Base, hCard->driver_data->offsets.kcan.rx_ctrl);
    hCard->io.spiFlashBase = SUM(hCard->io.pcieBar0Base, hCard->driver_data->offsets.tech.spi);
    hCard->io.interruptBase =
        SUM(hCard->io.pcieBar0Base, hCard->driver_data->offsets.tech.interrupt);

    DEBUGPRINT(2, "io.pcieBar0Base:%lx\n", (long)hCard->io.pcieBar0Base);
    return VCAN_STAT_OK;
}

//======================================================================
// Request send
//======================================================================
void pciefd_req_tx(VCanCardData *vCard, VCanChanData *vChd)
{
    PciCanChanData *hChan = vChd->hwChanData;

    if (!pciefd_is_tx_buffer_full(vChd)) {
#ifndef TRY_RT_QUEUE
        schedule_work(&hChan->txWork);
#else
        queue_work(hChan->txTaskQ, &hChan->txWork);
#endif
    }
#if DEBUG
    else {
        DEBUGPRINT(4, "SEND FAILED \n");
    }
#endif
}

//======================================================================
//  Process send queue - This function is called from the immediate queue
//======================================================================
static void pciefd_tx_can_msgs(struct work_struct *work)
{
    PciCanChanData *devChan = container_of(work, PciCanChanData, txWork);
    VCanChanData *chd = devChan->vChan;
    int queuePos;

    wait_for_completion(&devChan->busOnCompletion);

    while (1) {
        if (!chd->isOnBus) {
            DEBUGPRINT(4, "Attempt to send when not on bus\n");
            break;
        }

        if (chd->minorNr < 0) { // Channel not initialized?
            DEBUGPRINT(1, "Attempt to send on unitialized channel\n");
            break;
        }

        if (pciefd_is_tx_buffer_full(chd)) {
            DEBUGPRINT(4, "Maximum number of messages outstanding reached\n");
            break;
        }

        // Send Messages
        queuePos = queue_front(&chd->txChanQueue);
        if (queuePos >= 0) {
            if (pciefd_tx_can_msg(chd, &chd->txChanBuffer[queuePos]) == VCAN_STAT_OK) {
                queue_pop(&chd->txChanQueue);
                queue_wakeup_on_space(&chd->txChanQueue);
            } else {
                queue_release(&chd->txChanQueue);
                break;
            }
        } else {
            queue_release(&chd->txChanQueue);
            break;
        }
    }

    complete(&devChan->busOnCompletion);
}

//======================================================================
//  Initialize H/W specific data
//======================================================================
static void pciefd_init_channel_data(VCanCardData *vCard)
{
    int chNr;
    PciCanCardData *hCard = vCard->hwCardData;

    vCard->driverData = &driverData;
    vCanInitData(vCard);

    for (chNr = 0; chNr < vCard->nrChannels; chNr++) {
        VCanChanData *vChd = vCard->chanData[chNr];
        queue_irq_lock(&vChd->txChanQueue);
    }

    INIT_LIST_HEAD(&hCard->replyWaitList);

    rwlock_init(&hCard->replyWaitListLock);
    spin_lock_init(&hCard->lock);

    atomic_set(&hCard->status_seq_no, 0);

    for (chNr = 0; chNr < vCard->nrChannels; chNr++) {
        VCanChanData *vChd = vCard->chanData[chNr];
        PciCanChanData *hChd = vCard->chanData[chNr]->hwChanData;
        spin_lock_init(&hChd->lock);
        hChd->vChan = vChd;
        INIT_WORK(&hChd->txWork, pciefd_tx_can_msgs);
#ifdef TRY_RT_QUEUE
        char name[] = "pciefd_txX";
        name[9] = '0' + chNr; // Replace the X with channel number
        // Note that this will not create an RT task if the kernel
        // does not actually support it (only 2.6.28+ do).
        // In that case, you must (for now) do it manually using chrt.
        hChd->txTaskQ = create_rt_workqueue(name, &hChd->txWork);
#endif

        memset(hChd->current_tx_message, 0, sizeof(hChd->current_tx_message));
        atomic_set(&hChd->outstanding_tx, 0);
        atomic_set(&vChd->transId, 1);

        vChd->overrun = 0;
        vChd->errorCount = 0;
        hChd->overrun_occured = 0;

        init_completion(&hChd->busOnCompletion);
        complete(&hChd->busOnCompletion);
    }
}

//======================================================================
// Perform transceiver-specific setup
// Important: Is it too late to start transceivers at bus on?
// Wait for stable power before leaving reset mode?
//======================================================================
static void pciefd_enable_transceiver(VCanChanData *vChd)
{
    PciCanChanData *hChd = vChd->hwChanData;
    VCanCardData *vCard = vChd->vCard;
    PciCanCardData *hCard = vCard->hwCardData;
    int freq = FPGA_META_pci_freq(hCard->io.kcan.metaBase);

    pwmInit(freq);

    // Start with 95% power
    pwmSetFrequency(hChd->canControllerBase, 500000);
    pwmSetDutyCycle(hChd->canControllerBase, 95);
}

static int pciefd_get_cust_ch_name(const VCanChanData *const vChd, unsigned char *const data,
                                   const unsigned int data_size, unsigned int *const status)
{
    const unsigned int channel = vChd->channel;
    PciCanCardData *hCard = vChd->vCard->hwCardData;
    unsigned int nbr_of_bytes_to_copy;

    DEBUGPRINT(2, "[%s,%d]\n", __FUNCTION__, __LINE__);

    nbr_of_bytes_to_copy = HYDRA_FLASH_PARAM_MAX_SIZE;
    if (data_size < nbr_of_bytes_to_copy) {
        nbr_of_bytes_to_copy = data_size;
    }

    if (hCard->cust_channel_name[channel][0] != 0) {
        memcpy(data, hCard->cust_channel_name[channel], nbr_of_bytes_to_copy);
        data[nbr_of_bytes_to_copy - 1] = 0;
        *status = VCAN_STAT_OK;
    } else {
        *status = VCAN_STAT_FAIL;
    }

    return *status;
}


#if LED_TRIGGERS
/**
 * @brief Extended led_trigger_register_simple which
 * allocates memory for the trigger name
 *
 * @param name  Trigger name
 * @param tp    Pointer to pointer to trigger
 *
 * @note: This is done to allow for dynamically generated names in a buffer
 */
static void kv_led_trigger_register_simpler(struct device *dev, const char *name, struct led_trigger **tp)
{
    struct led_trigger *trig;
    int err = 0;

    trig = devm_kzalloc(dev, sizeof(struct led_trigger), GFP_KERNEL);

    if (trig) {
        trig->name = devm_kzalloc(dev, strlen(name) + 1, GFP_KERNEL);
        if (trig->name)
            strcpy((char *)trig->name, (char *)name);
        else
            goto error_exit;

        err = devm_led_trigger_register(dev, trig);
        if (err < 0)
            goto error_exit;
    } else {
        goto error_exit;
    }
    *tp = trig;
    return;

error_exit:
    if (trig)
        kfree(trig);
    if (err < 0)
        pr_warn("LED trigger %s failed to register (%d)\n", name, err);
    else
        pr_warn("LED trigger %s failed to register (no memory)\n", name);
    *tp = NULL;
    return;
}

#endif // LED_TRIGGERS

//======================================================================
// Initialize the HW for one card
//======================================================================
static int pciefd_probe(struct pci_dev *dev, const struct pci_device_id *id)
{
    // Helper struct for allocation
    typedef struct {
        VCanChanData *dataPtrArray[MAX_CARD_CHANNELS];
        VCanChanData vChd[MAX_CARD_CHANNELS];
        PciCanChanData hChd[MAX_CARD_CHANNELS];
    } ChanHelperStruct;

    ChanHelperStruct *chs = NULL;
    PciCanCardData *hCard = NULL;
    VCanCardData *vCard = NULL;
    int i;

#if LED_TRIGGERS
    char bdf[BDF_STR_LEN + 1];
#endif

    DEBUGPRINT(3, "\n######## Start of probe (%s) ######## \n", __func__);
    DEBUGPRINT(3, "%s - max channels:%u\n", __func__, MAX_CARD_CHANNELS);
#ifdef PCIEFD_DEBUG
    dev_dbg(&dev->dev, "<-- PCI BDF\n");
#endif

    // Allocate data area for this card
    vCard = kzalloc(sizeof(VCanCardData) + sizeof(PciCanCardData), GFP_KERNEL);
    if (!vCard) {
        DEBUGPRINT(1, "Failed to allocate memory for card data.\n");
        goto cleanup_end;
    }

    // hwCardData is directly after VCanCardData
    vCard->hwCardData = vCard + 1;
    hCard = vCard->hwCardData;
    hCard->dev = dev; // Needed for DMA mapping
    hCard->driver_data = (const struct pciefd_driver_data *)id->driver_data;

    // Allocate memory for n channels
    chs = kzalloc(sizeof(ChanHelperStruct), GFP_KERNEL);
    if (!chs) {
        DEBUGPRINT(1, "Failed to allocate memory for channels.\n");
        goto cleanup_vcard;
    }

    // Init array and hwChanData
    for (i = 0; i < MAX_CARD_CHANNELS; i++) {
        chs->dataPtrArray[i] = &chs->vChd[i];
        chs->vChd[i].hwChanData = &chs->hChd[i];
        chs->vChd[i].minorNr = -1; // No preset minor number
    }
    vCard->chanData = chs->dataPtrArray;

    // Get PCI controller and FPGA bus system addresses
    if (readPCIAddresses(dev, vCard) != VCAN_STAT_OK) {
        DEBUGPRINT(1, "readPCIAddresses failed\n");
        goto cleanup_channels;
    }

    // Find out type of card i.e. no. of channels etc.
    if (pciefd_probe_hw(vCard) != VCAN_STAT_OK) {
        DEBUGPRINT(1, "pciefd_probe_hw failed\n");
        goto cleanup_pci;
    }

    // start with all interrupts disabled
    DISABLE_ALL_INTERRUPTS(vCard);

#if LINUX_VERSION_CODE >= KERNEL_VERSION(4, 8, 0)
    {
#if (LINUX_VERSION_CODE >= KERNEL_VERSION(6, 10, 0))
        int ret = pci_alloc_irq_vectors(dev, 1, 1, PCI_IRQ_INTX | PCI_IRQ_MSI);
#else
        int ret = pci_alloc_irq_vectors(dev, 1, 1, PCI_IRQ_LEGACY | PCI_IRQ_MSI);
#endif /* KERNEL_VERSION >= 6.10.0 */
        if (ret < 0) {
            DEBUGPRINT(1, "pci_alloc_irq_vectors failed\n");
            goto cleanup_pci;
        }

        ret = pci_irq_vector(dev, 0);
        if (ret < 0) {
            DEBUGPRINT(1, "pci_irq_vector failed\n");
            goto cleanup_msi;
        }
        hCard->irq = ret;
    }
#else
    {
        int ret = pci_enable_msi(dev);
        if (ret < 0) {
            DEBUGPRINT(1, "pci_enable_msi failed\n");
            goto cleanup_pci;
        }
        hCard->irq = dev->irq;
    }
#endif /* >= 4.8.0 */
    DEBUGPRINT(2, "irq:%d\n", hCard->irq);

    if (request_irq(hCard->irq, pciefd_pci_interrupt, IRQF_SHARED, "Kvaser PCIe CAN", vCard)) {
        DEBUGPRINT(1, "request_irq failed\n");
        goto cleanup_msi;
    }

    // Initialize SPI
    if (SPI_FLASH_init(&hCard->hflash.spif,
                       hCard->driver_data->spi_ops,
                       hCard->io.spiFlashBase) != SPI_FLASH_STATUS_SUCCESS) {
        DEBUGPRINT(1, "SPI flash init failed.\n");
        goto cleanup_irq;
    }

    if (SPI_FLASH_start(&hCard->hflash.spif) != SPI_FLASH_STATUS_SUCCESS) {
        goto cleanup_spi_deinit;
    }

    if (HYDRA_FLASH_init(&hCard->hflash,
                         &hCard->driver_data->hw_const.flash_meta,
                         hCard->driver_data->hydra_flash_ops,
                         vCard) != HYDRA_FLASH_STAT_OK)
        goto cleanup_spi_stop;

    // Read/set parameters
    if (DEVICE_IS_EDGE(id->device)) {
        set_fallback_parameters(vCard);
    } else {
        int status = -1;

        if (status != VCAN_STAT_OK) {
            // check SPI FLASH type
            if (!SPI_FLASH_verify_jedec(&hCard->hflash.spif))
                goto cleanup_spi_stop;

            status = pciefd_read_flash_params(vCard);
            if (status != VCAN_STAT_OK) {
                DEBUGPRINT(1, "Error %d reading flash parameters.\n", status);
                goto cleanup_hydra_flash;
            }
        }
    }

    // Init channels
    pciefd_init_channel_data(vCard);
    pci_set_drvdata(dev, vCard);

#if PCIEFD_USE_DMA
    // Setup DMA
    if (hCard->useDma && pciefd_dma_init(vCard)) {
        DEBUGPRINT(3, "pciefd_dma_init failed\n");
        hCard->useDma = 0;
    }
#endif /* PCIEFD_USE_DMA */

    DEBUGPRINT(2, "0x%px = PCI BAR #0 base\n", hCard->io.pcieBar0Base);
    DEBUGPRINT(2, "0x%px = Interrupt base\n", hCard->io.kcan.kcanIntBase);
    DEBUGPRINT(2, "0x%px = RX buffer data base\n", hCard->io.kcan.rxDataBase);
    DEBUGPRINT(2, "0x%px = RX buffer ctrl base\n", hCard->io.kcan.rxCtrlBase);
    DEBUGPRINT(2, "0x%px = Timestamp base\n", hCard->io.kcan.timeBase);
    DEBUGPRINT(2, "0x%px = FPGA meta data base\n", hCard->io.kcan.metaBase);
    if (hCard->io.spiFlashBase != NULL)
        DEBUGPRINT(2, "0x%px = Serial flash base\n", hCard->io.spiFlashBase);

    // Init h/w & enable interrupts in PCI Interface
    if (pciefd_init_hw(vCard) != VCAN_STAT_OK) {
        DEBUGPRINT(1, "pciefd_init_hw failed\n");
        goto cleanup_begin;
    }

    printCardInfo(vCard);

    // Activate all channels
    for (i = 0; i < vCard->nrChannels; i++) {
        VCanChanData *vChd = vCard->chanData[i];
        pciefd_enable_transceiver(vChd);

        // Initialize with valid bus params (0.5 Mbit/s and 2.0 Mbit/s). Values are copied from
        // canTranslateBaud() in 'canlib.c'. If bus parameters are not initialized here it is not possible
        // to set CAN baud rate before CAN FD baudrate due to the prescaler check for CAN FD.
        {
            VCanBusParams busParams = {
                .freq = 500 * 1000,
                .tseg1 = 5U,
                .tseg2 = 2U,
                .sjw = 1U,
                .samp3 = 1U,
                .freq_brs = 2 * 1000 * 1000,
                .tseg1_brs = 15U,
                .tseg2_brs = 4U,
                .sjw_brs = 4U,
            };

            pciefd_set_busparams(vChd, &busParams);
        }
    }

#if PCIEFD_USE_DMA
    if (hCard->useDma && pciefd_setup_dma_mappings(hCard)) {
        DEBUGPRINT(3, "pciefd_setup_dma_mappings failed\n");
        hCard->useDma = 0;
    }

    if (hCard->useDma) {
        pciefd_start_dma(vCard);
    }

    if (!hCard->useDma)
#endif /* PCIEFD_USE_DMA */
    {
        fifoIrqEnableReceivedDataAvailable(hCard->io.kcan.rxCtrlBase);
        fifoIrqEnableMissingTag(hCard->io.kcan.rxCtrlBase);
    }

    set_current_state(TASK_UNINTERRUPTIBLE);
    schedule_timeout(msecs_to_jiffies(5)); // Wait 5 ms for transceiver to power up

    // Insert into list of cards
    spin_lock(&driverData.canCardsLock);
    vCard->next = driverData.canCards;
    driverData.canCards = vCard;
    spin_unlock(&driverData.canCardsLock);

#if PCIEFD_USE_DMA
    if (hCard->useDma)
        pci_set_master(dev);
#endif /* PCIEFD_USE_DMA */

    for (i = 0; i < vCard->nrChannels; i++) {
        VCanChanData *vChd = vCard->chanData[i];

        if (vCanAddCardChannel(vChd) != VCAN_STAT_OK) {
            goto cleanup_begin;
        }
    }

#if LED_TRIGGERS
    DEBUGPRINT(3, "Registering led triggers\n");
    get_bdf(dev, bdf);
    for (i = 0; i < vCard->nrChannels; i++) {
        VCanChanData *vcc = vCard->chanData[i];
        PciCanChanData *pcc = vcc->hwChanData;
        char name[50];

        sprintf(name, "%s-%s#%i-%s", "kv"DEVICE_NAME_STRING, bdf, i, "bus-on");
        DEBUGPRINT(6, "Registering led trigger %s\n", name);
        kv_led_trigger_register_simpler(&dev->dev, name, &pcc->ledtrig.bus_on);

        sprintf(name, "%s-%s#%i-%s", "kv"DEVICE_NAME_STRING, bdf, i, "can-activity");
        DEBUGPRINT(6, "Registering led trigger %s\n", name);
        kv_led_trigger_register_simpler(&dev->dev, name, &pcc->ledtrig.can_activity);

        sprintf(name, "%s-%s#%i-%s", "kv"DEVICE_NAME_STRING, bdf, i, "can-error");
        DEBUGPRINT(6, "Registering led trigger %s\n", name);
        kv_led_trigger_register_simpler(&dev->dev, name, &pcc->ledtrig.can_error);

        sprintf(name, "%s-%s#%i-%s", "kv"DEVICE_NAME_STRING, bdf, i, "buffer-overrun");
        DEBUGPRINT(6, "Registering led trigger %s\n", name);
        kv_led_trigger_register_simpler(&dev->dev, name, &pcc->ledtrig.buffer_overrun);

        led_trigger_event(pcc->ledtrig.bus_on, LED_OFF);
        led_trigger_event(pcc->ledtrig.can_activity, LED_OFF);
        led_trigger_event(pcc->ledtrig.can_error, LED_OFF);
        led_trigger_event(pcc->ledtrig.buffer_overrun, LED_OFF);
    }
#endif // LED_TRIGGERS

    return VCAN_STAT_OK;

// Error handling
cleanup_begin:
    DEBUGPRINT(3, "begin cleanup\n");
    for (i = vCard->nrChannels - 1; i >= 0; i--) {
        if (vCard->chanData[i] != NULL)
            vCanRemoveCardChannel(vCard->chanData[i]);
    }
#if defined(TRY_RT_QUEUE)
    DEBUGPRINT(3, "before loop\n");
    for (chNr = 0; chNr < vCard->nrChannels; chNr++) {
        PciCanChanData *hChd = vCard->chanData[chNr]->hwChanData;
        destroy_workqueue(hChd->txTaskQ);
    }
    DEBUGPRINT(3, "after loop\n");
#endif

cleanup_hydra_flash:
    HYDRA_FLASH_deinit(&hCard->hflash);

cleanup_spi_stop:
    DEBUGPRINT(3, "Stop SPI\n");
    SPI_FLASH_stop(&hCard->hflash.spif);

cleanup_spi_deinit:
    DEBUGPRINT(3, "Deinit SPI\n");
    SPI_FLASH_deinit(&hCard->hflash.spif);

cleanup_irq:
    DEBUGPRINT(3, "Stop interrupts\n");
    DISABLE_ALL_INTERRUPTS(vCard);
    free_irq(hCard->irq, vCard);

cleanup_msi:
#if LINUX_VERSION_CODE >= KERNEL_VERSION(4, 8, 0)
    DEBUGPRINT(3, "Free irq vectors\n");
    pci_free_irq_vectors(dev);
#else
    DEBUGPRINT(3, "Disable MSI\n");
    pci_disable_msi(dev);
#endif /* KERNEL_VERSION >= 4.8.0 */

cleanup_pci:
    DEBUGPRINT(3, "iounmap\n");
    pci_iounmap(dev, hCard->io.pcieBar0Base);
    DEBUGPRINT(3, "disable dev\n");
    pci_disable_device(dev);
    DEBUGPRINT(3, "release regions\n");
    pci_release_regions(dev);
    pci_set_drvdata(dev, NULL);

cleanup_channels:
    DEBUGPRINT(3, "free chandata\n");
    if (chs != NULL)
        kfree(chs);

cleanup_vcard:
    DEBUGPRINT(3, "free vcard\n");
    if (hCard->hflash.flash_image != NULL)
        kfree(hCard->hflash.flash_image);
    kfree(vCard);

cleanup_end:
    DEBUGPRINT(3, "end cleanup\n");

    return VCAN_STAT_FAIL;
} // pciefd_probe

//======================================================================
// Shut down the HW for one card
//======================================================================
static void pciefd_remove(struct pci_dev *dev)
{
    VCanCardData *vCard, *lastCard;
    PciCanCardData *hCard;
    int i;
    char bdf[BDF_STR_LEN + 1] = {0};
    char ean[EAN_STR_LEN + 1] = {0};

    DEBUGPRINT(3, "%s (0/7)\n", __func__);
    vCard = pci_get_drvdata(dev);
    hCard = vCard->hwCardData;
    get_bdf(hCard->dev, bdf);
    get_ean(vCard, ean);

#if LED_TRIGGERS
    DEBUGPRINT(3, "Unregistering led triggers\n");
    for (i = 0; i < vCard->nrChannels; i++) {
        VCanChanData *vcc = vCard->chanData[i];
        PciCanChanData *pcc = vcc->hwChanData;

        led_trigger_event(pcc->ledtrig.bus_on, LED_OFF);
        led_trigger_event(pcc->ledtrig.can_activity, LED_OFF);
        led_trigger_event(pcc->ledtrig.can_error, LED_OFF);
        led_trigger_event(pcc->ledtrig.buffer_overrun, LED_OFF);
    }
#endif // LED_TRIGGERS
    DEBUGPRINT(3, "%s (1/7)\n", __func__);
    HYDRA_FLASH_deinit(&hCard->hflash);
    SPI_FLASH_stop(&hCard->hflash.spif);
    SPI_FLASH_deinit(&hCard->hflash.spif);
#if PCIEFD_USE_DMA
    if (hCard->useDma) {
        pciefd_stop_dma(vCard);
        pci_clear_master(dev);
        pciefd_remove_dma_mappings(vCard);
    }
#endif /* PCIEFD_USE_DMA */
    pci_set_drvdata(dev, NULL);
    free_irq(hCard->irq, vCard);
#if LINUX_VERSION_CODE >= KERNEL_VERSION(4, 8, 0)
    pci_free_irq_vectors(dev);
#else
    pci_disable_msi(dev);
#endif /* KERNEL_VERSION >= 4.8.0 */

    DEBUGPRINT(3, "%s (2/7)\n", __func__);
    vCard->cardPresent = 0;
    DEBUGPRINT(3, DEVICE_NAME_STRING ": Stopping all \"waitQueue's\"\n");
    for (i = 0; i < vCard->nrChannels; i++) {
        vCanRemoveCardChannel(vCard->chanData[i]);
    }
    DEBUGPRINT(3, DEVICE_NAME_STRING ": Stopping all \"WaitNode's\"\n");
    {
        struct list_head *currHead;
        struct list_head *tmpHead;
        WaitNode *currNode;
        unsigned long irqFlags;

        write_lock_irqsave(&hCard->replyWaitListLock, irqFlags);
        list_for_each_safe(currHead, tmpHead, &hCard->replyWaitList) {
            currNode = list_entry(currHead, WaitNode, list);
            currNode->timedOut = 1;
            complete(&currNode->waitCompletion);
        }
        write_unlock_irqrestore(&hCard->replyWaitListLock, irqFlags);
    }
    for (i = 0; i < vCard->nrChannels; i++) {
        VCanChanData *vChan = vCard->chanData[i];
        PciCanChanData *hChd = vChan->hwChanData;

        DEBUGPRINT(3, DEVICE_NAME_STRING ": Waiting for all closed on minor %d\n", vChan->minorNr);
        while (atomic_read(&vChan->fileOpenCount) > 0) {
            set_current_state(TASK_UNINTERRUPTIBLE);
            schedule_timeout(msecs_to_jiffies(10));
        }

        // Disable bus load packets
        IOWR_PCIEFD_BLP(hChd->canControllerBase, 0);
        // Stop pwm
        pwmSetDutyCycle(hChd->canControllerBase, 0);
    }

    DEBUGPRINT(3, "%s - wait done (3/7)\n", __func__);
#if defined(TRY_RT_QUEUE)
    for (chNr = 0; chNr < vCard->nrChannels; chNr++) {
        PciCanChanData *hChd = vCard->chanData[chNr]->hwChanData;
        destroy_workqueue(hChd->txTaskQ);
    }
#endif
    DEBUGPRINT(3, "%s - wait done (4/7)\n", __func__);
    pci_iounmap(dev, hCard->io.pcieBar0Base);
    pci_disable_device(dev);
    pci_release_regions(dev);

    DEBUGPRINT(3, "%s - wait done (5/7)\n", __func__);
    // Remove from canCards list
    spin_lock(&driverData.canCardsLock);
    lastCard = driverData.canCards;
    if (lastCard == vCard) {
        driverData.canCards = vCard->next;
    } else {
        while (lastCard && (lastCard->next != vCard)) {
            lastCard = lastCard->next;
        }
        if (!lastCard) {
            DEBUGPRINT(1, "Card not in list!\n");
        } else {
            lastCard->next = vCard->next;
        }
    }
    spin_unlock(&driverData.canCardsLock);

    DEBUGPRINT(3, "%s - wait done (6/7)\n", __func__);
    kfree(vCard->chanData);
    kfree(vCard);

    DEBUGPRINT(3, "%s - wait done (7/7)\n", __func__);
    DEBUGPRINT(1, "Kvaser PCIe device %s, EAN %s was removed.\n", bdf, ean);
}

//======================================================================
// Find and initialize all cards
//======================================================================
static int pciefd_init(void)
{
    int status;

    driverData.deviceName = DEVICE_NAME_STRING;

    status = pci_register_driver(&pciefd_driver);
    DEBUGPRINT(3, "%s %d\n", __func__, status);

    return status;
} // pciefd_init

//======================================================================
// Shut down and free resources before unloading driver
//======================================================================
static int pciefd_exit(void)
{
    DEBUGPRINT(3, "%s\n", __func__);
    pci_unregister_driver(&pciefd_driver);

    return 0;
} // pciefd_exit

static int __init kvpciefd_init(void)
{
    driverData.hwIf = &hwIf;
    return vCanInit(&driverData, MAX_DRIVER_CHANNELS);
}

static void __exit kvpciefd_exit(void)
{
    vCanCleanup(&driverData);
}

module_init(kvpciefd_init);
module_exit(kvpciefd_exit);
