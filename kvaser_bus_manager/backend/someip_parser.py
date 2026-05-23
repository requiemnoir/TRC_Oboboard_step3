import struct

class SomeIpHeader:
    def __init__(self, service_id, method_id, length, client_id, session_id, proto_ver, iface_ver, msg_type, ret_code):
        self.service_id = service_id
        self.method_id = method_id
        self.length = length
        self.client_id = client_id
        self.session_id = session_id
        self.proto_ver = proto_ver
        self.iface_ver = iface_ver
        self.msg_type = msg_type
        self.ret_code = ret_code

    def __repr__(self):
        return f"SOMEIP(Srv={self.service_id:04x}, Met={self.method_id:04x}, Type={self.msg_type:02x})"

def parse_someip(payload):
    # SOME/IP Header is 16 bytes
    if len(payload) < 16:
        return None
    
    try:
        # Struct format: ! I I I B B B B
        # Fields are:
        # Message ID (Service ID 16b + Method ID 16b) = 32b -> I
        # Length = 32b -> I
        # Request ID (Client ID 16b + Session ID 16b) = 32b -> I
        # Protocol Version = 8b -> B
        # Interface Version = 8b -> B
        # Message Type = 8b -> B
        # Return Code = 8b -> B
        
        msg_id, length, req_id, proto_ver, iface_ver, msg_type, ret_code = struct.unpack('!IIIBBB B', payload[:16])
        
        service_id = (msg_id >> 16) & 0xFFFF
        method_id = msg_id & 0xFFFF
        client_id = (req_id >> 16) & 0xFFFF
        session_id = req_id & 0xFFFF
        
        return SomeIpHeader(service_id, method_id, length, client_id, session_id, proto_ver, iface_ver, msg_type, ret_code)
    except Exception:
        return None
