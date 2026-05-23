try:
    import canlib.canlib as canlib
except (ImportError, OSError, SystemExit):
    # Mock for development without drivers
    class MockCanLib:
        canOPEN_ACCEPT_VIRTUAL = 1
        canNoMsg = Exception
        canError = Exception
        def openChannel(self, ch, flags):
            class Channel:
                def busOn(self): pass
                def busOff(self): pass
                def close(self): pass
                def read(self, timeout): raise Exception("No Msg")
            return Channel()
    canlib = MockCanLib()

import time

# Note: FlexRay support in Python via Kvaser requires specific hardware and often the 'kvadblib' or specific setup.
# This handler assumes the hardware is in a mode where we can read raw frames or uses a simplified approach.
# Standard canlib supports FlexRay on specific channels if configured.

class FlexRayHandler:
    def __init__(self, channel_number):
        self.channel_number = channel_number
        self.ch = None
        self.is_open = False

    def open(self):
        try:
            # FlexRay often requires specific flags or a different open call depending on the Kvaser device generation.
            # We use standard openChannel but expect the user to have configured the device for FlexRay if needed.
            self.ch = canlib.openChannel(self.channel_number, canlib.canOPEN_ACCEPT_VIRTUAL)
            
            # FlexRay configuration is complex and usually requires a database (FIBEX) to configure the controller.
            # For this "Universal" handler, we assume the interface is already configured or we just listen.
            self.ch.busOn()
            self.is_open = True
            print(f"FlexRay Channel {self.channel_number} Opened.")
            return True
        except canlib.canError as e:
            print(f"Error opening FlexRay channel {self.channel_number}: {e}")
            return False

    def close(self):
        if self.ch:
            try:
                self.ch.busOff()
                self.ch.close()
            except Exception as e:
                print(f"Error closing FlexRay channel: {e}")
        self.is_open = False
        self.ch = None

    def read(self):
        if not self.is_open:
            return None
        try:
            msg = self.ch.read(timeout=10)
            timestamp = getattr(msg, 'time', None)
            if timestamp is None:
                timestamp = getattr(msg, 'timestamp', None)
            if timestamp is None:
                timestamp = getattr(msg, 'timeStamp', None)
            if timestamp is None:
                timestamp = time.time() * 1000
            return {
                "id": msg.id,
                "data": list(msg.data),
                "dlc": msg.dlc,
                "flags": msg.flags,
                "timestamp": timestamp,
                "type": "FLEXRAY"
            }
        except canlib.canNoMsg:
            return None
        except canlib.canError as e:
            return None
