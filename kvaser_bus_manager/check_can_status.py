import sys
import time
import os

# Use the same import style as the backend (Kvaser Python canlib).
try:
    import canlib.canlib as canlib
except (ImportError, OSError) as e:
    print(f"canlib not available ({e}). Are Kvaser drivers + python-canlib installed in this Python env?")
    sys.exit(1)

def check_channels():
    num = canlib.getNumberOfChannels()
    print(f"Found {num} channels")

    # Optional: bring the bus ON to get meaningful status on some devices.
    # Disabled by default to avoid surprising behavior.
    do_bus_on = str(os.getenv('CHECK_CAN_BUSON', '0')).strip().lower() in {'1', 'true', 'yes', 'on'}
    bitrate_env = str(os.getenv('CHECK_CAN_BITRATE', '500000')).strip()

    bitrate = None
    if do_bus_on:
        # Map common bitrates to Kvaser constants when available.
        br_map = {
            '1000000': getattr(canlib, 'canBITRATE_1M', None),
            '500000': getattr(canlib, 'canBITRATE_500K', None),
            '250000': getattr(canlib, 'canBITRATE_250K', None),
            '125000': getattr(canlib, 'canBITRATE_125K', None),
        }
        bitrate = br_map.get(bitrate_env)
        if bitrate is None:
            try:
                bitrate = int(bitrate_env)
            except Exception:
                bitrate = getattr(canlib, 'canBITRATE_500K', None)

    for i in range(num):
        try:
            # Open channel to check real status.
            # NOTE: canOPEN_ACCEPT_VIRTUAL may fail on some setups; fall back to flags=0.
            try:
                flags = int(getattr(canlib, 'canOPEN_ACCEPT_VIRTUAL', 0) or 0)
                ch = canlib.openChannel(i, flags)
            except Exception:
                ch = canlib.openChannel(i, 0)
            data = canlib.ChannelData(i)
            print(f"Channel {i}: {data.channel_name}")

            if do_bus_on and bitrate is not None:
                try:
                    ch.setBusOutputControl(getattr(canlib, 'canDRIVER_NORMAL', 4))
                    ch.setBusParams(bitrate)
                    ch.busOn()
                    time.sleep(0.05)
                except Exception as e:
                    print(f"  Warning: failed to busOn/set bitrate: {e}")
            
            # Check bus status
            flags = ch.readStatus()
            print(f"  Status Flags: {flags}")
            
            if flags & canlib.canSTAT_BUS_OFF:
                print("  -> BUS OFF")
            if flags & canlib.canSTAT_ERROR_PASSIVE:
                print("  -> ERROR PASSIVE")
            if flags & canlib.canSTAT_ERROR_WARNING:
                print("  -> ERROR WARNING")
            if flags & canlib.canSTAT_ERROR_ACTIVE:
                print("  -> ERROR ACTIVE")
                
            # Check TX Error Counter
            try:
                if hasattr(ch, 'read_error_counters'):
                    ec = ch.read_error_counters()
                    # ErrorCounters(tx=..., rx=..., overrun=...)
                    print(f"  Error Counters: {ec}")
                elif hasattr(ch, 'readErrorCounters'):
                    tx_err, rx_err, _ = ch.readErrorCounters()
                    print(f"  TX Errors: {tx_err}, RX Errors: {rx_err}")
            except Exception:
                pass

            try:
                if do_bus_on:
                    ch.busOff()
            except Exception:
                pass
            ch.close()
        except Exception as e:
            print(f"  Error checking channel {i}: {e}")

if __name__ == "__main__":
    check_channels()
