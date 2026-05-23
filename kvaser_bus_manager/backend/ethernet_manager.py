from ethernet_capture import EthernetCapture
from doip_client import DoIPClient
from xcp_eth_client import XCPEthClient
from mf4_logger import EthernetMF4Logger
import os
import time
import threading
LIVE_TRAFFIC_ENABLED = str(os.getenv('KBSM_LIVE_TRAFFIC_ENABLE', '0')).strip().lower() in {'1', 'true', 'yes', 'on'}

class EthernetManager:
    def __init__(self, socketio=None, main_logger=None):
        self.capture = None
        self.doip = None
        self.xcp = None
        self.mf4_logger = None # Renamed from self.logger to avoid confusion
        self.main_logger = main_logger # Reference to BusLogger
        self.log_dir = getattr(main_logger, 'log_dir', None)
        self.config = {}
        self.socketio = socketio
        self._listeners = []
        # Optional callback invoked for every packet emitted.
        # Used by app.py to implement an Ethernet-triggered logger start.
        self.trigger_cb = None
        self._mirror_injection_cb = None
        self._mirror_port = None
        self._ui_emit_interval_s = max(0.0, float(os.getenv('KBSM_UI_ETH_EMIT_INTERVAL_S', '0.10') or 0.10))
        self._ui_emit_batch_max = max(1, int(os.getenv('KBSM_UI_ETH_EMIT_BATCH_MAX', '48') or 48))
        self._pending_ui_packets = []
        self._ui_emit_lock = threading.Lock()
        self._ui_emit_timer = None
        self._save_thread = None
        self._last_save_path = None
        self._last_save_error = None

    def _flush_ui_packets(self):
        if not LIVE_TRAFFIC_ENABLED:
            with self._ui_emit_lock:
                self._ui_emit_timer = None
                self._pending_ui_packets = []
            return
        batch = []
        with self._ui_emit_lock:
            self._ui_emit_timer = None
            if not self._pending_ui_packets:
                return
            batch = self._pending_ui_packets
            self._pending_ui_packets = []

        if not self.socketio:
            return

        try:
            if len(batch) == 1:
                self.socketio.emit('eth_packet', batch[0])
            else:
                self.socketio.emit('eth_packet_batch', batch)
        except Exception:
            pass

    def _schedule_ui_flush_locked(self):
        if self._ui_emit_timer is not None:
            return
        timer = threading.Timer(self._ui_emit_interval_s, self._flush_ui_packets)
        timer.daemon = True
        self._ui_emit_timer = timer
        timer.start()

    def _emit_ui_packet(self, data):
        if (not LIVE_TRAFFIC_ENABLED) or (not self.socketio):
            return
        if self._ui_emit_interval_s <= 0:
            try:
                self.socketio.emit('eth_packet', data)
            except Exception:
                pass
            return

        flush_now = False
        timer = None
        with self._ui_emit_lock:
            self._pending_ui_packets.append(data)
            if len(self._pending_ui_packets) >= self._ui_emit_batch_max:
                flush_now = True
                timer = self._ui_emit_timer
                self._ui_emit_timer = None
            else:
                self._schedule_ui_flush_locked()

        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass
        if flush_now:
            self._flush_ui_packets()

    def set_mirror_injection_callback(self, cb):
        """Set callback to inject parsed mirror frames into BusManager."""
        self._mirror_injection_cb = cb
        if self.capture:
            # If capture already running (unlikely if called early), hot patch it
            self.capture.mirror_callback = cb

    def set_mirror_port(self, port):
        """Update the mirror listening port.  Propagates to an active capture."""
        self._mirror_port = int(port) if port else None
        if self.capture:
            self.capture.mirror_port = self._mirror_port or 30490

    def add_listener(self, cb):
        if callable(cb):
            self._listeners.append(cb)

    def remove_listener(self, cb):
        if cb in self._listeners:
            self._listeners.remove(cb)

    def _build_capture_bpf_filter(self, config):
        override = str(os.getenv('KBSM_ETH_BPF_FILTER', '') or '').strip()
        if override:
            return override

        mirror_port = int(self._mirror_port or config.get('mirror_port') or 30490)
        if any(bool(config.get(key)) for key in ('pcap_enabled', 'someip_enabled', 'doip_enabled', 'xcp_enabled')):
            return ''

        return f'udp port {mirror_port} or tcp port 13400'

    def start_logging(self, formats):
        if 'mf4' in formats:
            if self.log_dir:
                self.mf4_logger = EthernetMF4Logger(log_dir=self.log_dir)
            else:
                self.mf4_logger = EthernetMF4Logger()
            print("Ethernet MF4 Logging started")

    def stop_logging(self):
        logger = self.mf4_logger
        self.mf4_logger = None
        if logger:
            base_path = None
            try:
                base_path = getattr(self.main_logger, 'base_name', None)
            except Exception:
                base_path = None

            def _save_worker(detached_logger, detached_base_path):
                try:
                    logfile = detached_logger.save(base_path=detached_base_path)
                    self._last_save_path = logfile
                    self._last_save_error = None
                    print(f"Ethernet MF4 Logging stopped: {logfile}")
                except Exception as e:
                    self._last_save_error = str(e)
                    print(f"Ethernet MF4 Logging save error: {e}")

            self._save_thread = threading.Thread(
                target=_save_worker,
                args=(logger, base_path),
                name='ethernet-mf4-save',
                daemon=True,
            )
            self._save_thread.start()
            return {'saving': True, 'base_path': base_path}
        return None

    # --- Proxy Methods for Logging ---
    def log_raw_eth(self, *args, **kwargs):
        if self.mf4_logger:
            self.mf4_logger.log_raw_eth(*args, **kwargs)

    def log_doip(self, *args, **kwargs):
        if self.mf4_logger:
            self.mf4_logger.log_doip(*args, **kwargs)

    def log_someip(self, *args, **kwargs):
        if self.mf4_logger:
            self.mf4_logger.log_someip(*args, **kwargs)

    def log_xcp(self, *args, **kwargs):
        if self.mf4_logger:
            self.mf4_logger.log_xcp(*args, **kwargs)
    # ---------------------------------

    def _emit_packet(self, data):
        # Trigger hook (best-effort)
        try:
            if callable(self.trigger_cb):
                self.trigger_cb(data)
        except Exception:
            pass

        # Notify listeners
        for cb in self._listeners:
            try:
                cb(data)
            except Exception:
                pass

        self._emit_ui_packet(data)
        
        # Log to main logger (CSV/TXT/JSON/MF4) only if explicitly enabled.
        # Ethernet packet streams can be extremely high-rate and will dwarf CAN logs.
        # Prefer PCAP and/or the dedicated EthernetMF4Logger instead.
        log_to_main = str(os.getenv('ETH_LOG_TO_MAIN', '0')).strip().lower() in {'1', 'true', 'yes', 'on'}
        if log_to_main and self.main_logger and self.main_logger.active:
            # Normalize to milliseconds (CAN uses ms epoch timestamps).
            ts = data.get('timestamp')
            try:
                ts_f = float(ts)
            except Exception:
                ts_f = time.time()
            # Heuristic: scapy uses seconds float; convert to ms.
            ts_ms = int(ts_f * 1000.0) if ts_f < 1e11 else int(ts_f)
            log_entry = {
                "timestamp": ts_ms,
                "channel": "ETH",
                "type": "ETH",
                "id": 0,
                "dlc": data['length'],
                "data": data['payload_hex'],
                "flags": 0,
                "decoded": {"name": data['summary']}
            }
            self.main_logger.log(log_entry)

    def start(self, config):
        self.config = config
        # self.logger = EthernetMF4Logger() # Removed: Managed by start_logging
        
        # Always start the capture thread (needed for mirror, DoIP in-band,
        # SOME/IP, and live traffic).  When pcap_enabled is False we simply
        # skip saving packets to a .pcap file.
        pcap_file = None
        if config.get("pcap_enabled"):
            pcap_file = "capture.pcap"
            try:
                base = getattr(self.main_logger, 'base_name', None)
                if base:
                    pcap_file = f"{base}.pcap"
                else:
                    ld = self.log_dir or getattr(self.main_logger, 'log_dir', None)
                    if ld:
                        pcap_file = os.path.join(str(ld), f"capture_{time.strftime('%Y%m%d_%H%M%S')}.pcap")
            except Exception:
                pcap_file = "capture.pcap"

        capture_bpf_filter = self._build_capture_bpf_filter(config)

        self.capture = EthernetCapture(
            interface=config["interface"],
            logger=self,
            on_packet=self._emit_packet,
            pcap_file=pcap_file,
            mirror_callback=self._mirror_injection_cb,
            mirror_port=self._mirror_port or config.get("mirror_port"),
            bpf_filter=capture_bpf_filter,
        )
        self.capture.start()
            
        if config.get("doip_enabled"):
            self.doip = DoIPClient(config["doip_ip"], self)
            self.doip.start()
            
        if config.get("xcp_enabled"):
            self.xcp = XCPEthClient(config["xcp_ip"], int(config["xcp_port"]), self)
            self.xcp.start()

    def stop(self):
        try:
            with self._ui_emit_lock:
                timer = self._ui_emit_timer
                self._ui_emit_timer = None
        except Exception:
            timer = None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass
        self._flush_ui_packets()
        if self.capture:
            self.capture.stop()
        if self.doip:
            self.doip.stop()
        if self.xcp:
            self.xcp.stop()
        # Logging is now independent, but if we stop Ethernet, we might want to ensure logging stops or just continues?
        # Usually, if we stop the source, logging just stops receiving data.
        # We don't force save here anymore, unless we want to enforce "Stop Ethernet = Stop Logging for Ethernet"
        # But user asked for consistency. CAN Stop doesn't stop logging. So Ethernet Stop shouldn't stop logging (it just stops data).
        return None

    def get_stats(self):
        if self.capture:
            return self.capture.stats
        return {"pps": 0, "mbps": 0, "errors": 0}

    def send_uds(self, sid, did, data, target_addr=None):
        if self.doip:
            self.doip.send_uds(sid, did, data, target_address=target_addr)
