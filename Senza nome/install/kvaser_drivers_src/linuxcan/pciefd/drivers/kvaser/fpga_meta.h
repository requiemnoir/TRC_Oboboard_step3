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
 * FPGA meta data interface
 */

#ifndef __FPGA_META_H__
#define __FPGA_META_H__

#ifdef __KERNEL__
#include <linux/io.h>
#else
#include "util.h"
#include <stdbool.h>
#endif /* __KERNEL__ */

// Build data register offsets
#define FPGA_META_OFFSET_DATE     (0 * 4U)
#define FPGA_META_OFFSET_TIME     (1 * 4U)
#define FPGA_META_OFFSET_CH_REV   (2 * 4U)
#define FPGA_META_OFFSET_CAN_FREQ (3 * 4U)
#define FPGA_META_OFFSET_PCI_FREQ (4 * 4U)
#define FPGA_META_OFFSET_SRC_LO   (5 * 4U)
#define FPGA_META_OFFSET_SRC_HI   (6 * 4U)

/**
 * @brief Get build date
 *
 * @param base_addr     Pointer to FPGA meta data IOMEM registers
 * @return u32          Build date in BCD format
 */
static inline u32 FPGA_META_build_date(void __iomem *base_addr)
{
    return readl(base_addr + FPGA_META_OFFSET_DATE);
}

/**
 * @brief Get build time
 *
 * @param base_addr     Pointer to FPGA meta data IOMEM registers
 * @return u32          Build time in BCD format
 */
static inline u32 FPGA_META_build_time(void __iomem *base_addr)
{
    return readl(base_addr + FPGA_META_OFFSET_TIME);
}

/**
 * @brief Get no. of channels
 *
 * @param base_addr     Pointer to FPGA meta data IOMEM registers
 * @return u8           No. of channels
 */
static inline u8 FPGA_META_num_channels(void __iomem *base_addr)
{
    return (readl(base_addr + FPGA_META_OFFSET_CH_REV) & 0xFF000000) >> 24;
}

/**
 * @brief Get major revision no.
 *
 * @param base_addr     Pointer to FPGA meta data IOMEM registers
 * @return u8           Major revision no.
 */
static inline u8 FPGA_META_major_rev(void __iomem *base_addr)
{
    return (readl(base_addr + FPGA_META_OFFSET_CH_REV) & 0x00FF0000) >> 16;
}

/**
 * @brief Get minor revision no.
 *
 * @param base_addr     Pointer to FPGA meta data IOMEM registers
 * @return u8           Minor revision no.
 */
static inline u8 FPGA_META_minor_rev(void __iomem *base_addr)
{
    return (readl(base_addr + FPGA_META_OFFSET_CH_REV) & 0x000000FF);
}

/**
 * @brief Get CAN frequency
 *
 * @param base_addr     Pointer to FPGA meta data IOMEM registers
 * @return u32          CAN frequency
 */
static inline u32 FPGA_META_can_freq(void __iomem *base_addr)
{
    return readl(base_addr + FPGA_META_OFFSET_CAN_FREQ);
}

/**
 * @brief Get bus frequency
 *
 * @param base_addr     Pointer to FPGA meta data IOMEM registers
 * @return u32          Bus frequency
 */
static inline u32 FPGA_META_pci_freq(void __iomem *base_addr)
{
    return readl(base_addr + FPGA_META_OFFSET_PCI_FREQ);
}

/**
 * @brief Get per user sequence id
 *
 * @param base_addr     Pointer to FPGA meta data IOMEM registers
 * @return u32          Per user sequence id
 */
static inline u16 FPGA_META_user_seq_id(void __iomem *base_addr)
{
    return (readl(base_addr + FPGA_META_OFFSET_SRC_LO) & 0x0000FFFE) >> 1;
}

/**
 * @brief Check if build is clean
 *
 * @param base_addr     Pointer to FPGA meta data IOMEM registers
 * @return true         If FPGA IP was built from clean sources
 * @return false        If FPGA IP wasn't built from clean sources
 */
static inline bool FPGA_META_is_clean_build(void __iomem *base_addr)
{
    return ((readl(base_addr + FPGA_META_OFFSET_SRC_LO) & 0x00000001) == 0);
}

/**
 * @brief Get source id
 *
 * @param base_addr     Pointer to FPGA meta data IOMEM registers
 * @return u64          Source id
 */
static inline u64 FPGA_META_source_id(void __iomem *base_addr)
{
    return (((u64)readl(base_addr + FPGA_META_OFFSET_SRC_HI) << 16) +
            ((readl(base_addr + FPGA_META_OFFSET_SRC_LO) & 0xFFFF0000) >> 16));
}

#endif // __FPGA_META_H__
