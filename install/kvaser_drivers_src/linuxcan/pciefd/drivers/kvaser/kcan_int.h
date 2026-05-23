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
 * Kvaser KCAN interrupt controller interface
 */

#ifndef __KCAN_INT_H__
#define __KCAN_INT_H__
#include <linux/io.h>

// IO memory register offsets
#define KCAN_INT_REG_RAW    0x0000
#define KCAN_INT_REG_MASK   0x0004
#define KCAN_INT_REG_MASKED 0x0008

// Interrupt source bit map
#define KCAN_INT_IRQ_SPI0  BIT(0) // SPI flash
#define KCAN_INT_IRQ_RX0   BIT(4) // Receive buffer
#define KCAN_INT_IRQ_TX(n) BIT((n) + 16) // n < max no. of channels!

/**
 * @brief Get unfiltered interrupt sources
 *
 * @param int_base  Pointer to base address of KCAN interrupt IO memory registers
 * @return u32      A 32-bit value where each bit represents an interrupt source
 */
static inline u32 KCAN_INT_get_raw_irq(const void __iomem *int_base)
{
    return readl(int_base + KCAN_INT_REG_RAW);
}

/**
 * @brief Get interrupt enable mask
 *
 * @param int_base  Pointer to base address of KCAN interrupt IO memory registers
 * @return u32      A 32-bit value where each bit represents an interrupt source
 */
static inline u32 KCAN_INT_get_enable_mask(const void __iomem *int_base)
{
    return readl(int_base + KCAN_INT_REG_MASK);
}

/**
 * @brief Get interrupt sources filtered by the enable mask
 *
 * @param int_base  Pointer to base address of KCAN interrupt IO memory registers
 * @return u32      A 32-bit value where each bit represents an interrupt source
 */
static inline u32 KCAN_INT_get_irq(const void __iomem *int_base)
{
    return readl(int_base + KCAN_INT_REG_MASKED);
}

/**
 * @brief Set interrupt enable mask
 *
 * @param int_base  Pointer to base address of KCAN interrupt IO memory registers
 * @param mask      A 32-bit value where each bit represents an interrupt source
 */
static inline void KCAN_INT_set_enable_mask(void __iomem *int_base, u32 mask)
{
    writel(mask, int_base + KCAN_INT_REG_MASK);
}

/**
 * @brief Enable interrupt sources according to a bit pattern.
 *
 * A bit set (1) will enable the corresponding interrupt source
 * while a bit cleared (0) will leave it in it's current state.
 *
 * @param int_base  Pointer to base address of KCAN interrupt IO memory registers
 * @param mask      A 32-bit value where each bit represents an interrupt source
 */
static inline void KCAN_INT_set_enable_mask_bits(void __iomem *int_base, u32 bits)
{
    u32 mask = readl(int_base + KCAN_INT_REG_MASK);
    mask |= bits;
    writel(mask, int_base + KCAN_INT_REG_MASK);
}

/**
 * @brief Disable interrupt sources according to a bit pattern.
 *
 * A bit set (1) will disable the corresponding interrupt source
 * while a bit cleared (0) will leave it in it's current state.
 *
 * @param int_base  Pointer to base address of KCAN interrupt IO memory registers
 * @param mask      A 32-bit value where each bit represents an interrupt source
 */
static inline void KCAN_INT_clear_enable_mask_bits(void __iomem *int_base, u32 bits)
{
    u32 mask = readl(int_base + KCAN_INT_REG_MASK);
    mask &= ~bits;
    writel(mask, int_base + KCAN_INT_REG_MASK);
}

#endif // __KCAN_INT_H__
