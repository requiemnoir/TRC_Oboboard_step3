import os
import time

try:
    import numpy as np
except Exception:
    np = None

try:
    from asammdf import MDF, Signal
except Exception:
    MDF = None
    Signal = None

class EthernetMF4Logger:
    def __init__(self, log_dir=None):
        if np is None or MDF is None or Signal is None:
            raise ImportError("MF4 Ethernet logging richiede 'numpy' e 'asammdf'")

        # Default to the project-level logs folder (../logs) rather than cwd-relative "logs".
        if log_dir is None:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            log_dir = os.path.abspath(os.path.join(base_dir, '..', 'logs'))
        elif not os.path.isabs(str(log_dir)):
            base_dir = os.path.dirname(os.path.abspath(__file__))
            log_dir = os.path.abspath(os.path.join(base_dir, '..', str(log_dir)))

        self.log_dir = str(log_dir)
        if not os.path.exists(self.log_dir):
            os.makedirs(self.log_dir, exist_ok=True)
        self.mdf = MDF()
        self.start_time = time.time()
        self.data_buffer = {
            "raw": {"t": [], "src_ip": [], "dst_ip": [], "proto": [], "len": []},
            "doip": {"t": [], "ecu_id": [], "sid": [], "did": [], "resp": []},
            "someip": {"t": [], "srv_id": [], "met_id": [], "msg_type": [], "len": []},
            "xcp": {"t": [], "name": [], "val": [], "unit": []}
        }
        self.max_buffer = 1000

    def log_raw_eth(self, timestamp, src_ip, dst_ip, proto, length):
        self.data_buffer["raw"]["t"].append(timestamp)
        self.data_buffer["raw"]["src_ip"].append(src_ip)
        self.data_buffer["raw"]["dst_ip"].append(dst_ip)
        self.data_buffer["raw"]["proto"].append(proto)
        self.data_buffer["raw"]["len"].append(length)
        self._check_flush()

    def log_doip(self, timestamp, ecu_id, sid, did, resp_code):
        self.data_buffer["doip"]["t"].append(timestamp)
        self.data_buffer["doip"]["ecu_id"].append(ecu_id)
        self.data_buffer["doip"]["sid"].append(sid)
        self.data_buffer["doip"]["did"].append(did)
        self.data_buffer["doip"]["resp"].append(resp_code)
        self._check_flush()

    def log_someip(self, timestamp, srv_id, met_id, msg_type, length):
        self.data_buffer["someip"]["t"].append(timestamp)
        self.data_buffer["someip"]["srv_id"].append(srv_id)
        self.data_buffer["someip"]["met_id"].append(met_id)
        self.data_buffer["someip"]["msg_type"].append(msg_type)
        self.data_buffer["someip"]["len"].append(length)
        self._check_flush()

    def log_xcp(self, timestamp, name, value, unit):
        # XCP is tricky because "name" and "unit" are strings, MDF signals usually numeric.
        # We might need separate signals for each XCP measurement name if we want to plot them.
        # For now, we'll log them as string signals or generic events.
        # Better approach: Create a signal for each unique 'name' encountered?
        # For simplicity in this "generic" logger, we might just log value if name is constant, 
        # but here we might receive mixed signals.
        # Let's store them in a generic list for now, or assume the caller handles signal separation.
        # To satisfy the requirement "Associare canali XCP a gruppi MF4", we should probably 
        # have a dynamic signal creation.
        # For this implementation, I will log them as text events or simple signals if possible.
        # Let's stick to a simple structure: XCP_Value, XCP_Name (String).
        self.data_buffer["xcp"]["t"].append(timestamp)
        self.data_buffer["xcp"]["name"].append(name)
        self.data_buffer["xcp"]["val"].append(value)
        self.data_buffer["xcp"]["unit"].append(unit)
        self._check_flush()

    def _check_flush(self):
        # In a real high-perf scenario, we would flush to disk or append to MDF object periodically.
        # Here we just keep in memory until stop for simplicity, or append to MDF.
        pass

    def save(self, base_path: str | None = None):
        # Create Signals
        sigs = []
        
        # RAW
        if self.data_buffer["raw"]["t"]:
            t = np.array(self.data_buffer["raw"]["t"])
            sigs.append(Signal(np.array(self.data_buffer["raw"]["len"]), t, name="ETH_Length", unit="bytes"))
            sigs.append(Signal(np.array(self.data_buffer["raw"]["proto"]), t, name="ETH_Proto"))
            # IP addresses are strings, MDF supports string signals (byte arrays)
            # Converting IPs to string arrays
            sigs.append(Signal(np.array(self.data_buffer["raw"]["src_ip"], dtype='S15'), t, name="ETH_SrcIP", encoding='utf-8'))
            sigs.append(Signal(np.array(self.data_buffer["raw"]["dst_ip"], dtype='S15'), t, name="ETH_DstIP", encoding='utf-8'))

        # DOIP
        if self.data_buffer["doip"]["t"]:
            t = np.array(self.data_buffer["doip"]["t"])
            sigs.append(Signal(np.array(self.data_buffer["doip"]["ecu_id"]), t, name="DOIP_ECU_ID"))
            sigs.append(Signal(np.array(self.data_buffer["doip"]["sid"]), t, name="DOIP_SID"))
            sigs.append(Signal(np.array(self.data_buffer["doip"]["did"]), t, name="DOIP_DID"))
            sigs.append(Signal(np.array(self.data_buffer["doip"]["resp"]), t, name="DOIP_RespCode"))

        # SOMEIP
        if self.data_buffer["someip"]["t"]:
            t = np.array(self.data_buffer["someip"]["t"])
            sigs.append(Signal(np.array(self.data_buffer["someip"]["srv_id"]), t, name="SOMEIP_ServiceID"))
            sigs.append(Signal(np.array(self.data_buffer["someip"]["met_id"]), t, name="SOMEIP_MethodID"))
            sigs.append(Signal(np.array(self.data_buffer["someip"]["msg_type"]), t, name="SOMEIP_MsgType"))
            sigs.append(Signal(np.array(self.data_buffer["someip"]["len"]), t, name="SOMEIP_Length"))

        # XCP
        if self.data_buffer["xcp"]["t"]:
            # Group by signal name
            from collections import defaultdict
            grouped = defaultdict(lambda: {"t": [], "v": []})
            for i, name in enumerate(self.data_buffer["xcp"]["name"]):
                grouped[name]["t"].append(self.data_buffer["xcp"]["t"][i])
                grouped[name]["v"].append(self.data_buffer["xcp"]["val"][i])
            
            for name, data in grouped.items():
                clean_name = str(name or '').strip()
                if not clean_name:
                    continue
                prefixed = clean_name if clean_name.startswith('XCP:') else f"XCP:1.{clean_name}"
                values = np.array(data["v"])
                timestamps = np.array(data["t"])
                sigs.append(Signal(values, timestamps, name=prefixed))
                sigs.append(Signal(values, timestamps, name=clean_name))

        self.mdf.append(sigs)

        filepath = None
        if base_path:
            try:
                # `base_path` is expected to be a full path without extension, e.g. .../logs/session_YYYYmmdd_HHMMSS
                base_path = str(base_path)
                filepath = f"{base_path}.eth.mf4"
            except Exception:
                filepath = None

        if not filepath:
            filename = f"eth_log_{int(time.time())}.mf4"
            filepath = os.path.join(self.log_dir, filename)

        self.mdf.save(filepath)
        return filepath
