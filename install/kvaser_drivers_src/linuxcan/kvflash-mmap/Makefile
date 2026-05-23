#
#             Copyright 2023 by Kvaser AB, Molndal, Sweden
#                         http://www.kvaser.com
#
#  This software is dual licensed under the following two licenses:
#  BSD-new and GPLv2. You may use either one. See the included
#  COPYING file for details.
#
#  License: BSD-new
#  ==============================================================================
#  Redistribution and use in source and binary forms, with or without
#  modification, are permitted provided that the following conditions are met:
#      * Redistributions of source code must retain the above copyright
#        notice, this list of conditions and the following disclaimer.
#      * Redistributions in binary form must reproduce the above copyright
#        notice, this list of conditions and the following disclaimer in the
#        documentation and/or other materials provided with the distribution.
#      * Neither the name of the <organization> nor the
#        names of its contributors may be used to endorse or promote products
#        derived from this software without specific prior written permission.
#
#  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
#  AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
#  IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
#  ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
#  LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
#  CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
#  SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR
#  BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER
#  IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
#  ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
#  POSSIBILITY OF SUCH DAMAGE.
#
#
#  License: GPLv2
#  ==============================================================================
#  This program is free software; you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation; either version 2 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software
#  Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA
#
#
#  IMPORTANT NOTICE:
#  ==============================================================================
#  This source code is made available for free, as an open license, by Kvaser AB,
#  for use with its applications. Kvaser AB does not accept any liability
#  whatsoever for any third party patent or other immaterial property rights
#  violations that may result from any usage of this source code, regardless of
#  the combination of source code and various applications that it can be used
#  in, or with.
#
#  -----------------------------------------------------------------------------
#

UTIL_NAME = kv_flash_prog_mmap

LINUXCAN_ROOT ?= ..

CC ?= gcc
CFLAGS = -Wall -Wextra -Werror -O2 -I.
CFLAGS += -DLINUX
CFLAGS += -I../kvflash/common
CFLAGS += -I$(LINUXCAN_ROOT)/include
CFLAGS += -I$(LINUXCAN_ROOT)/pciefd/hw
CFLAGS += -I$(LINUXCAN_ROOT)/pciefd/util
CFLAGS += -I$(LINUXCAN_ROOT)/pciefd/drivers/altera/inc
CFLAGS += -I$(LINUXCAN_ROOT)/pciefd/drivers/altera/inc/sys
CFLAGS += -I$(LINUXCAN_ROOT)/pciefd/drivers/altera/HAL/inc
CFLAGS += -I$(LINUXCAN_ROOT)/pciefd/drivers/kvaser
CFLAGS += -I$(LINUXCAN_ROOT)/pciefd/drivers/kvaser/hydra_flash
CFLAGS += -I$(LINUXCAN_ROOT)/pciefd/drivers/kvaser/spi/sf2_spi
CFLAGS += -I$(LINUXCAN_ROOT)/pciefd/drivers/kvaser/spi_flash
CFLAGS += -I$(LINUXCAN_ROOT)/pciefd/drivers/xilinx/spi/src
CFLAGS += -I$(LINUXCAN_ROOT)/pciefd/drivers/xilinx/common/src

LDLIBS = -lpci

KVFLASH_OBJS = kv_flash_prog.c can_fwu.o can_fwu_hydra.o crc32.o
KVFLASH_MMAP_OBJS = kvaser_pciefd.o $(addprefix ../kvflash/common/, $(KVFLASH_OBJS)) \
                    kvaser_pciefd_altera.o \
                    kvaser_pciefd_sf2.o \
                    kvaser_pciefd_xilinx.o \
                    $(LINUXCAN_ROOT)/common/util.o \
                    $(LINUXCAN_ROOT)/pciefd/drivers/altera/HAL/src/altera_avalon_epcs_flash_controller.o \
                    $(LINUXCAN_ROOT)/pciefd/drivers/altera/HAL/src/altera_avalon_spi.o \
                    $(LINUXCAN_ROOT)/pciefd/drivers/altera/HAL/src/epcs_commands.o \
                    $(LINUXCAN_ROOT)/pciefd/drivers/kvaser/spi_flash/spi_flash.o \
                    $(LINUXCAN_ROOT)/pciefd/drivers/kvaser/spi_flash/spi_flash_sf2.o \
                    $(LINUXCAN_ROOT)/pciefd/drivers/kvaser/spi_flash/spi_flash_altera.o \
                    $(LINUXCAN_ROOT)/pciefd/drivers/kvaser/spi_flash/spi_flash_xilinx.o \
                    $(LINUXCAN_ROOT)/pciefd/drivers/kvaser/hydra_flash/hydra_flash.o \
                    $(LINUXCAN_ROOT)/pciefd/drivers/kvaser/spi/sf2_spi/sf2_spi.o \
                    $(LINUXCAN_ROOT)/pciefd/drivers/xilinx/spi/src/xspi.o \
                    $(LINUXCAN_ROOT)/pciefd/drivers/xilinx/spi/src/xspi_options.o

ifeq ($(KV_DEBUG_ON),1)
  KV_XTRA_CFLAGS_DEBUG= -D_DEBUG=1 -DDEBUG=1
  IS_DEBUG=Debug: $(KV_XTRA_CFLAGS_DEBUG)
  CFLAGS += $(KV_XTRA_CFLAGS_DEBUG) -g
else
  KV_XTRA_CFLAGS_DEBUG= -D_DEBUG=0 -DDEBUG=0
  CFLAGS += $(KV_XTRA_CFLAGS_DEBUG)
endif

.PHONY: clean clean_linuxcan clean_linuxcan_o

# do not clean and build in parallell
.NOTPARALLEL:

all: clean_linuxcan $(UTIL_NAME) clean_linuxcan_o

$(UTIL_NAME): $(KVFLASH_MMAP_OBJS)
	$(CC) -o $@ $^ $(CFLAGS) $(LDLIBS)
	@echo --------------------------------------------------------------------
	@echo "Built Kvaser $(UTIL_NAME)"
	@echo --------------------------------------------------------------------

clean:
	rm -rf $(UTIL_NAME) *.o *~ ../kvflash/*.o

clean_linuxcan:
	# Remove build output built by linuxcan Makefile
	find $(LINUXCAN_ROOT) -type f \( -name "*.o" -o -name "*.so" \) -delete

clean_linuxcan_o:
	# Remove object files built by this Makefile
	find $(LINUXCAN_ROOT) -type f -name "*.o" -delete

