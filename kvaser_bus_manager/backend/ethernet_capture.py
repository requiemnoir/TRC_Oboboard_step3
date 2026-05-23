import threading
import time
import os

# scapy is heavy to import; load it lazily when capture starts.
sniff = None
wrpcap = None
Ether = None
IP = None
IPv6 = None
TCP = None
UDP = None
Dot1Q = None


def _ensure_scapy() -> None:
    global sniff, wrpcap, Ether, IP, IPv6, TCP, UDP, Dot1Q
    if sniff is not None:
        return
    try:
        from scapy.all import sniff as _sniff, wrpcap as _wrpcap, Ether as _Ether, IP as _IP, IPv6 as _IPv6, TCP as _TCP, UDP as _UDP, Dot1Q as _Dot1Q
        sniff = _sniff
        wrpcap = _wrpcap
        Ether = _Ether
        IP = _IP
        IPv6 = _IPv6
        TCP = _TCP
        UDP = _UDP
        Dot1Q = _Dot1Q
    except Exception:
        sniff = None
        wrpcap = None
        Ether = None
        IP = None
        IPv6 = None
        TCP = None
        UDP = None
        Dot1Q = None
from someip_parser import parse_someip

# Default mirror data port.  Overridden at runtime by the gateway_mirror
# config (dest_port) so that the capture filter stays aligned with what the
# gateway is actually told to send to.
DEFAULT_MIRROR_PORT = 30490

class EthernetCapture:
    def __init__(self, interface, logger, on_packet=None, pcap_file="capture.pcap", mirror_callback=None, mirror_port=None, bpf_filter=None):
        self.interface = interface
        self.logger = logger
        self.on_packet = on_packet
        self.pcap_file = pcap_file
        self.mirror_callback = mirror_callback
        self.mirror_port = int(mirror_port) if mirror_port else DEFAULT_MIRROR_PORT
        self.bpf_filter = str(bpf_filter).strip() if bpf_filter else ''
        self.running = False
        self.thread = None
        self.packets = []
        self.stats = {"pps": 0, "mbps": 0, "errors": 0, "count": 0}
        self._last_stats_time = time.time()
        self._byte_count = 0
        self._mirror_rx_count = 0
        self._mirror_vag_start_hint = 4
        self._mirror_recent = {}
        self._mirror_recent_lock = threading.Lock()
        try:
            dedupe_window_ms = float(os.getenv('KBSM_MIRROR_DEDUPE_WINDOW_MS', '2.0') or 2.0)
        except Exception:
            dedupe_window_ms = 2.0
        self._mirror_dedupe_window_s = max(0.0, min(dedupe_window_ms / 1000.0, 0.050))

    # ------------------------------------------------------------------
    #  Mirror payload decoders
    # ------------------------------------------------------------------

    def _emit_mirror_frame(self, arb_id, data, frame_type="CAN", channel_id=99, flags=0):
        """Helper to emit a single parsed mirror CAN/FlexRay/LIN frame."""
        payload = bytes(data or b'')
        dedupe_window = getattr(self, '_mirror_dedupe_window_s', 0.0)
        if dedupe_window > 0.0:
            now = time.monotonic()
            key = (
                str(frame_type or '').upper(),
                int(channel_id),
                int(arb_id) & 0x1FFFFFFF,
                int(flags or 0) & 0xFF,
                payload,
            )
            recent = getattr(self, '_mirror_recent', None)
            recent_lock = getattr(self, '_mirror_recent_lock', None)
            if recent is None:
                recent = {}
                self._mirror_recent = recent
            if recent_lock is None:
                import threading
                recent_lock = threading.Lock()
                self._mirror_recent_lock = recent_lock
            with recent_lock:
                last_seen = recent.get(key)
                if last_seen is not None and (now - float(last_seen)) <= dedupe_window:
                    return
                recent[key] = now
                if len(recent) > 1024:
                    prune_age = max(dedupe_window * 4.0, 0.010)
                    self._mirror_recent = {
                        k: ts for k, ts in recent.items()
                        if (now - float(ts)) <= prune_age
                    }
        if self.mirror_callback:
            callback_kwargs = {
                'channel_id': channel_id,
                'arb_id': arb_id,
                'data': payload,
                'flags': int(flags or 0),
                'frame_type': frame_type,
                'capture_origin': 'mirror',
            }
            try:
                self.mirror_callback(**callback_kwargs)
            except TypeError as exc:
                if 'capture_origin' not in str(exc):
                    raise
                callback_kwargs.pop('capture_origin', None)
                self.mirror_callback(**callback_kwargs)
            self._mirror_rx_count += 1
            if self._mirror_rx_count <= 5 or self._mirror_rx_count % 100 == 0:
                print(f"[MIRROR] #{self._mirror_rx_count} {frame_type} 0x{arb_id:03X} [{len(data)}] {data.hex()}", flush=True)

    def _unpack_mirror_payload(self, payload):
        """Decode mirror UDP/TCP payload.  Supports multiple formats:

        0. **VAG SOME/IP-wrapped Bus Mirroring** (MLBevo gateway):
           SOME/IP header (16B): ServiceID=0x02FD, MethodID=0xF302
           Then VAG proprietary payload: [PktLen:2][Flags:2] + N × frame entries
           Each CAN frame entry:
             [TimestampOffset:2][BusChannel:2][NetworkType:1][Pad:2][CAN_ID:2][DataLen:2][Data:N]
           NetworkType: 1=CAN, 0=status/bus-state block

        1. **AUTOSAR Bus Mirroring** (ISO 23150 / AUTOSAR SWS_BM):
           Header: [StatusByte:1][TimeStamp:4][SeqCounter:2]
           Followed by N × [NetworkType:1][NetworkID:1][FrameID:4][PayloadLen:2][Payload:N]

        2. **Iron Bird / Simulation** (legacy DOOD protocol):
           Repeating: [Magic:2 = 0xD00D][ArbID:4][DLC:1][Data:8]  = 15 bytes

        3. **Raw CAN-in-UDP** (simple):
           Repeating: [ArbID:4][DLC:1][Data:0..8]

        The function auto-detects the format from the payload header.
        """
        import struct

        if not payload or len(payload) < 7:
            return

        # --- Format 0: VAG SOME/IP-wrapped mirror (Service 0x02FD / Method 0xF302) ---
        if len(payload) >= 20:
            someip_srv = struct.unpack('!H', payload[0:2])[0]
            someip_met = struct.unpack('!H', payload[2:4])[0]
            if someip_srv == 0x02FD and someip_met == 0xF302:
                inner = payload[16:]  # strip 16-byte SOME/IP header
                # IMPORTANT: if it's our mirror SOME/IP service, do not fall through
                # to AUTOSAR/raw decoders (they would misinterpret SOME/IP bytes as CAN).
                self._try_unpack_vag_mirror(inner)
                return

        # --- Format 1: Iron Bird (magic 0xD00D) ---
        if len(payload) >= 15:
            maybe_magic = struct.unpack('!H', payload[0:2])[0]
            if maybe_magic == 0xD00D:
                self._unpack_iron_bird(payload)
                return

        # --- Format 2: AUTOSAR Bus Mirroring (ISO 23150) ---
        # StatusByte(1) + Timestamp(4) + SeqCounter(2) = 7-byte header
        # Then network frames: NetworkType(1) + NetworkID(1) + FrameID(4) + PayloadLen(2) + data
        # We detect this by checking if byte 0 looks like a valid status byte (0x00-0x0F)
        # and the rest can be parsed as frames.
        status_byte = payload[0]
        if status_byte <= 0x0F and len(payload) >= 15:
            if self._try_unpack_autosar_mirror(payload):
                return

        # --- Format 3: Raw CAN-in-UDP (simple 5+ byte blocks) ---
        # [ArbID:4][DLC:1][data:DLC]
        if len(payload) >= 5:
            self._try_unpack_raw_can(payload)

    def _unpack_iron_bird(self, payload):
        """Iron Bird / Simulation protocol: [0xD00D:2][ArbID:4][DLC:1][Data:8] = 15B blocks."""
        import struct
        block_size = 15
        offset = 0
        while offset + block_size <= len(payload):
            magic, arb_id, dlc = struct.unpack('!HIB', payload[offset:offset + 7])
            if magic != 0xD00D:
                break
            data_bytes = payload[offset + 7:offset + 7 + 8]
            real_data = data_bytes[:min(dlc, 8)]
            self._emit_mirror_frame(arb_id, real_data, "CAN", channel_id=99)
            offset += block_size

    def _try_unpack_vag_mirror(self, payload):
        """Best-effort decoder for VAG proprietary mirror payload (inside SOME/IP 0x02FD/0xF302).

        The MLBevo gateway uses more than one record layout in the same family of captures.
        We therefore resync byte-by-byte and try both known layouts at each offset:

        - Layout A: ts:2, bus:1, ntype:1, reserved:2, id_or_slotcycle:2, len:2, data
        - Layout B: ts:2, bus:2, ntype:1, pad:1, id_or_slot:2, len:2, data

        Supported output types here:
        - CAN (classic)
        - FlexRay (slot id + cycle for downstream FIBEX variant selection)
        """
        import struct

        if not payload or len(payload) < 16:
            return False

        # Known MLBevo IDs (subset) used only as a scoring signal (not a hard requirement).
        known_ids = {
            0x0FD, 0x0A8, 0x0A7, 0x0116, 0x0086, 0x007C, 0x0108, 0x00B5, 0x0040,
            0x030B, 0x0030, 0x023C, 0x03C0, 0x0103, 0x00AD, 0x00B3, 0x0121, 0x03D5,
        }

        def _try_at(off: int):
            """Try to parse one VAG mirror record at a given offset.

            Returns (consumed_bytes, frame_type, bus_ch, frame_id, data, flags)
            or None.
            """
            if off + 10 > len(payload):
                return None

            candidates = []

            # Layout A: ts:2, bus:1, ntype:1, reserved:2, id_or_slotcycle:2, len:2
            bus_a = payload[off + 2]
            ntype_a = payload[off + 3]
            field_a = struct.unpack('!H', payload[off + 6:off + 8])[0]
            len_a = struct.unpack('!H', payload[off + 8:off + 10])[0]
            if bus_a <= 15 and 0 < len_a <= 254:
                end_a = off + 10 + len_a
                if end_a <= len(payload):
                    data_a = payload[off + 10:end_a]
                    if ntype_a == 1 and field_a <= 0x7FF and len_a <= 8:
                        candidates.append((10 + len_a, 'CAN', bus_a, field_a, data_a, 0))
                    elif ntype_a == 0:
                        slot_id = (field_a >> 8) & 0xFF
                        cycle = field_a & 0xFF
                        if slot_id != 0 and 0 <= cycle <= 63 and len_a >= 8:
                            candidates.append((10 + len_a, 'FlexRay', bus_a, slot_id, data_a, cycle))

            # Layout B: ts:2, bus:2, ntype:1, pad:1, id_or_slot:2, len:2
            # Only try Layout B when Layout A didn't produce a match, to avoid
            # false FlexRay/CAN cross-interpretations from shifted ntype fields.
            if not candidates:
                bus_b = struct.unpack('!H', payload[off + 2:off + 4])[0] & 0xFF
                ntype_b = payload[off + 4]
                field_b = struct.unpack('!H', payload[off + 6:off + 8])[0]
                len_b = struct.unpack('!H', payload[off + 8:off + 10])[0]
                if bus_b <= 15 and 0 < len_b <= 254:
                    end_b = off + 10 + len_b
                    if end_b <= len(payload):
                        data_b = payload[off + 10:end_b]
                        if ntype_b == 1 and field_b <= 0x7FF and len_b <= 8:
                            candidates.append((10 + len_b, 'CAN', bus_b, field_b, data_b, 0))
                        elif ntype_b == 0:
                            slot_id = payload[off + 6]
                            cycle = payload[off + 7]
                            if slot_id != 0 and 0 <= cycle <= 63 and len_b >= 8:
                                candidates.append((10 + len_b, 'FlexRay', bus_b, slot_id, data_b, cycle))

            if not candidates:
                return None

            def _score(candidate):
                consumed, frame_type, _bus_ch, frame_id, frame_data, _flags = candidate
                if frame_type == 'FlexRay':
                    base = 1000
                    if len(frame_data) == 34:
                        base += 100
                    elif len(frame_data) in {12, 16, 20, 24, 32}:
                        base += 40
                    if 1 <= frame_id <= 255:
                        base += 20
                    return base + consumed
                base = 500
                if frame_id in known_ids:
                    base += 200
                if 0 <= frame_id <= 0x7FF:
                    base += 50
                return base + consumed

            candidates.sort(key=_score, reverse=True)
            return candidates[0]

        # Start scanning after a small packet header.  4 bytes is typical (len+flags).
        start_hint_raw = getattr(self, '_mirror_vag_start_hint', 4)
        start_hint = int(start_hint_raw) if isinstance(start_hint_raw, int) else 4
        start_offsets = []
        for off in [start_hint, 4, 3, 2, 0]:
            if off not in start_offsets:
                start_offsets.append(off)
        best = {
            "frames": [],
            "score": -1,
            "start": start_offsets[0] if start_offsets else 4,
        }

        for start in start_offsets:
            frames = []
            score = 0
            i = start
            # resync scan
            bad_run = 0
            while i + 10 <= len(payload):
                hit = _try_at(i)
                if hit is None:
                    i += 1
                    bad_run += 1
                    # Keep scanning the full payload even after long unknown gaps.
                    # Real mirror packets can embed large status/proprietary blocks
                    # between valid records; stopping early silently drops the valid
                    # FlexRay/CAN frames that follow later in the same SOME/IP packet.
                    continue
                bad_run = 0
                consumed, frame_type, bus_ch, frame_id, data, flags = hit
                if not data:
                    i += 1
                    continue
                frames.append((frame_type, bus_ch, frame_id, data, flags))
                if frame_type == "CAN":
                    arb_id = frame_id
                    if arb_id in known_ids:
                        score += 3
                    else:
                        score += 1
                elif frame_type == 'FlexRay':
                    score += 2
                i += consumed

            if len(frames) >= 1 and score > best["score"]:
                best = {"frames": frames, "score": score, "start": start}

        if not best["frames"]:
            return False

        try:
            self._mirror_vag_start_hint = int(best.get("start", start_hint))
        except Exception:
            pass

        for frame_type, bus_ch, frame_id, data, flags in best["frames"]:
            if frame_type == 'FlexRay':
                self._emit_mirror_frame(frame_id, data, 'FlexRay', channel_id=200 + bus_ch, flags=flags)
            else:
                self._emit_mirror_frame(frame_id & 0x1FFFFFFF, data, 'CAN', channel_id=100 + bus_ch)

        return True


    def _try_unpack_autosar_mirror(self, payload):
        """AUTOSAR Bus Mirroring format (SWS_BM / ISO 23150).

        Header (7 bytes):
          [StatusByte:1] [TimeStamp:4 (µs)] [SequenceCounter:2]
        Frame entries:
          [NetworkType:1] [NetworkID:1] [FrameID:4] [PayloadLen:2] [Payload:PayloadLen]
          NetworkType: 0x01=CAN, 0x02=CAN-FD, 0x03=LIN, 0x04=FlexRay, 0x05=Ethernet
        """
        import struct

        if len(payload) < 7:
            return False

        # Parse header
        # status_byte = payload[0]
        # timestamp_us = struct.unpack('!I', payload[1:5])[0]
        # seq_counter = struct.unpack('!H', payload[5:7])[0]

        offset = 7
        parsed_any = False

        while offset + 8 <= len(payload):  # minimum frame entry: 1+1+4+2 = 8
            net_type = payload[offset]
            net_id = payload[offset + 1]
            frame_id = struct.unpack('!I', payload[offset + 2:offset + 6])[0]
            pld_len = struct.unpack('!H', payload[offset + 6:offset + 8])[0]
            offset += 8

            if pld_len > 4095 or offset + pld_len > len(payload):
                break  # sanity check

            frame_data = payload[offset:offset + pld_len]
            offset += pld_len

            if net_type in (0x01, 0x02):  # CAN / CAN-FD
                arb_id = frame_id & 0x1FFFFFFF
                self._emit_mirror_frame(arb_id, frame_data, "CAN" if net_type == 0x01 else "CAN-FD", channel_id=100 + net_id)
                parsed_any = True
            elif net_type == 0x04:  # FlexRay
                self._emit_mirror_frame(frame_id, frame_data, "FlexRay", channel_id=200 + net_id)
                parsed_any = True
            elif net_type == 0x03:  # LIN
                self._emit_mirror_frame(frame_id, frame_data, "LIN", channel_id=150 + net_id)
                parsed_any = True
            # type 0x05 (Ethernet) — skip for now

        return parsed_any

    def _try_unpack_raw_can(self, payload):
        """Simple raw CAN-in-UDP: [ArbID:4][DLC:1][Data:0..8] repeating."""
        import struct
        offset = 0
        while offset + 5 <= len(payload):
            arb_id = struct.unpack('!I', payload[offset:offset + 4])[0]
            dlc = payload[offset + 4]
            if dlc > 64 or arb_id > 0x1FFFFFFF:
                break  # not valid CAN
            actual = min(dlc, len(payload) - offset - 5)
            if actual <= 0 and dlc > 0:
                break
            data = payload[offset + 5:offset + 5 + actual]
            self._emit_mirror_frame(arb_id & 0x1FFFFFFF, data, "CAN", channel_id=99)
            offset += 5 + max(actual, dlc)

    def _unpack_doip_mirror(self, tcp_payload):
        """Extract mirror CAN frames embedded inside DoIP diagnostic messages.

        The VAG gateway (MLBevo) may send mirror data as DoIP *diagnostic message*
        payloads (type 0x8001) from the gateway logical address (e.g. 0x4010) to the
        tester (0x0E00).  The UDS payload within these messages carries mirrored CAN
        frames using one of several sub-formats:

        1. AUTOSAR mirror format (same as UDP variant)
        2. Proprietary VAG format: [SourceBusID:1][ArbID:2or4][DLC:1][Data:N]
        3. Encapsulated RDBI response containing mirror snapshot

        We iterate over concatenated DoIP messages in the TCP stream.
        """
        import struct
        offset = 0

        while offset + 8 <= len(tcp_payload):
            # DoIP header: [Ver:1][InvVer:1][PayloadType:2][Length:4]
            if tcp_payload[offset] != 0x02 and tcp_payload[offset] != 0x03:
                offset += 1
                continue
            ver = tcp_payload[offset]
            inv_ver = tcp_payload[offset + 1]
            if (ver ^ inv_ver) != 0xFF:
                offset += 1
                continue
            ptype = struct.unpack('!H', tcp_payload[offset + 2:offset + 4])[0]
            plen = struct.unpack('!I', tcp_payload[offset + 4:offset + 8])[0]

            if plen > 65535 or offset + 8 + plen > len(tcp_payload):
                break

            doip_body = tcp_payload[offset + 8:offset + 8 + plen]
            offset += 8 + plen

            # We only care about diagnostic message (0x8001)
            if ptype != 0x8001 or len(doip_body) < 5:
                continue

            src_addr = struct.unpack('!H', doip_body[0:2])[0]
            # dst_addr = struct.unpack('!H', doip_body[2:4])[0]
            uds_data = doip_body[4:]

            # Skip normal UDS responses (TesterPresent, session, negative, etc.)
            if not uds_data or uds_data[0] in (0x7E, 0x50, 0x51, 0x67, 0x6E, 0x7F, 0x3E):
                continue

            # Source should be the gateway (0x40xx range)
            if (src_addr & 0xFF00) != 0x4000:
                continue

            # Attempt to parse UDS payload as mirror data
            # Check for AUTOSAR mirror format (StatusByte ≤ 0x0F, enough length)
            if len(uds_data) >= 15 and uds_data[0] <= 0x0F:
                if self._try_unpack_autosar_mirror(uds_data):
                    continue

            # VAG proprietary: [BusID:1][ArbID:4][DLC:1][Data:DLC]
            if len(uds_data) >= 6:
                bus_id = uds_data[0]
                if bus_id <= 0x20:  # reasonable bus ID
                    arb_id = struct.unpack('!I', uds_data[1:5])[0]
                    dlc = uds_data[5]
                    if arb_id <= 0x1FFFFFFF and dlc <= 64:
                        actual = min(dlc, len(uds_data) - 6)
                        data = uds_data[6:6 + actual]
                        self._emit_mirror_frame(arb_id, data, "CAN", channel_id=100 + bus_id)


    def start(self):
        self.running = True
        _ensure_scapy()
        if sniff is None:
            raise ImportError("scapy non installato (pip install scapy)")
        self.thread = threading.Thread(target=self._sniff_loop)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=2)
        # Save PCAP (only if pcap_file was configured)
        if self.pcap_file and self.packets and wrpcap is not None:
            try:
                parent = os.path.dirname(os.path.abspath(str(self.pcap_file)))
                if parent:
                    os.makedirs(parent, exist_ok=True)
            except Exception:
                pass
            wrpcap(self.pcap_file, self.packets)

    def _sniff_loop(self):
        # Scapy sniff is blocking, so we use a timeout or stop_filter
        # We increase timeout to 5s to reduce promiscuous mode toggling frequency.
        # Filter out own traffic (SSH port 22, Web port 5000/5001) to avoid flooding capture with management traffic.
        bpf_filter = self.bpf_filter or "not port 22"
        
        while self.running:
            try:
                # timeout=5 minimizes the "entered/left promiscuous mode" syslog spam
                sniff(iface=self.interface, filter=bpf_filter, prn=self._process_packet, store=0, timeout=1)
            except Exception as e:
                print(f"Sniff Error: {e}")
                self.stats["errors"] += 1
                time.sleep(1)

    def _process_packet(self, pkt):
        if not self.running:
            return

        # Mirror Traffic (UDP)
        if UDP in pkt and pkt[UDP].dport == self.mirror_port:
            if self.mirror_callback and pkt[UDP].payload:
                try:
                    payload = bytes(pkt[UDP].payload)
                    self._unpack_mirror_payload(payload)
                except Exception as e:
                    print(f"Mirror unpack error: {e}")
        elif TCP in pkt and pkt[TCP].dport == self.mirror_port:
            if self.mirror_callback and pkt[TCP].payload:
                try:
                    payload = bytes(pkt[TCP].payload)
                    self._unpack_mirror_payload(payload)
                except Exception as e:
                    print(f"Mirror TCP unpack error: {e}")

        # --- DoIP diagnostic mirror: the gateway may embed mirror frames
        #     inside normal DoIP diagnostic messages on port 13400. ---
        _doip_port = 13400
        if TCP in pkt and (pkt[TCP].sport == _doip_port or pkt[TCP].dport == _doip_port):
            if self.mirror_callback and pkt[TCP].payload:
                try:
                    tcp_payload = bytes(pkt[TCP].payload)
                    self._unpack_doip_mirror(tcp_payload)
                except Exception:
                    pass

        # Only accumulate raw packets when pcap_file is set (avoid memory leak).
        if self.pcap_file:
            self.packets.append(pkt)
        self.stats["count"] += 1
        self._byte_count += len(pkt)
        
        # Update Stats
        now = time.time()
        if now - self._last_stats_time >= 1.0:
            self.stats["pps"] = self.stats["count"] / (now - self._last_stats_time)
            self.stats["mbps"] = (self._byte_count * 8) / (1000000 * (now - self._last_stats_time))
            self.stats["count"] = 0
            self._byte_count = 0
            self._last_stats_time = now

        # Log to MF4
        src = "0.0.0.0"
        dst = "0.0.0.0"
        proto = 0
        
        if IP in pkt:
            src = pkt[IP].src
            dst = pkt[IP].dst
            proto = pkt[IP].proto
        elif IPv6 and IPv6 in pkt:
            src = pkt[IPv6].src
            dst = pkt[IPv6].dst
            proto = pkt[IPv6].nh  # Next Header in IPv6 is basically protocol

        self.logger.log_raw_eth(pkt.time, src, dst, proto, len(pkt))

        # Check SOME/IP
        someip_info = None
        if UDP in pkt or TCP in pkt:
            payload = bytes(pkt[UDP].payload) if UDP in pkt else bytes(pkt[TCP].payload)
            someip = parse_someip(payload)
            if someip:
                self.logger.log_someip(pkt.time, someip.service_id, someip.method_id, someip.msg_type, someip.length)
                someip_info = f"SOME/IP Srv:0x{someip.service_id:04X} Met:0x{someip.method_id:04X}"

        # Emit to UI
        if self.on_packet:
            summary = pkt.summary()
            if someip_info:
                summary = someip_info
            
            # Format for eth_packet handler
            payload_hex = bytes(pkt).hex()
            layers = []
            if Ether in pkt: layers.append("Ether")
            if Dot1Q and Dot1Q in pkt: layers.append("VLAN") # Detect VLANs
            if IP in pkt: layers.append("IP")
            if IPv6 and IPv6 in pkt: layers.append("IPv6")
            if TCP in pkt: layers.append("TCP")
            if UDP in pkt: layers.append("UDP")
            
            data = {
                "timestamp": pkt.time,
                "summary": summary,
                "layers": ", ".join(layers),
                "length": len(pkt),
                "payload_hex": payload_hex
            }
            self.on_packet(data)
