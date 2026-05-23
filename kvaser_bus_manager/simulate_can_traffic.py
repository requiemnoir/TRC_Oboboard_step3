import time
import threading
import random
try:
    try:
        import canlib.canlib as canlib
    except (ImportError, OSError, SystemExit):
        canlib = None
except ImportError:
    print("Error: canlib not found. Cannot simulate on real interfaces.")
    exit(1)

def simulate_channel(ch_idx):
    try:
        # Open channel for exclusive access or shared? 
        # canOPEN_ACCEPT_VIRTUAL allows opening virtual channels if they exist
        ch = canlib.openChannel(ch_idx, canlib.canOPEN_ACCEPT_VIRTUAL)
        
        # Try to set Self-Reception (Loopback) to allow simulation without physical bus
        try:
            # canDRIVER_SELFRECEPTION = 8 (usually)
            # We use the constant if available, else 8
            mode = getattr(canlib, 'canDRIVER_SELFRECEPTION', 8)
            ch.setBusOutputControl(mode)
            print(f"Channel {ch_idx} set to Self-Reception Mode")
        except:
            print(f"Channel {ch_idx} could not set Self-Reception, using Normal")
            ch.setBusOutputControl(canlib.canDRIVER_NORMAL)

        ch.setBusParams(canlib.canBITRATE_500K)
        ch.busOn()
        print(f"Simulation started on Channel {ch_idx}")
    except Exception as e:
        print(f"Failed to open Channel {ch_idx}: {e}")
        return

    counter = 0
    try:
        while True:
            counter = (counter + 1) % 255
            
            # Define ID and Data based on channel
            if ch_idx == 0: # Engine
                msg_id = 0x100
                data = [0x01, counter, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]
                interval = 0.01 # 10ms
            elif ch_idx == 1: # Body
                msg_id = 0x200
                data = [0x02, 0x00, counter, 0x00, 0x00, 0x00, 0x00, 0x00]
                interval = 0.1 # 100ms
            elif ch_idx == 2: # Chassis
                msg_id = 0x300
                data = [0x03, 0x00, 0x00, counter, 0x00, 0x00, 0x00, 0x00]
                interval = 0.02 # 20ms
            elif ch_idx == 3: # ADAS
                msg_id = 0x400
                data = [0x04, 0x00, 0x00, 0x00, counter, 0x00, 0x00, 0x00]
                interval = 0.05 # 50ms
            else:
                msg_id = 0x500 + ch_idx
                data = [0xFF] * 8
                interval = 1.0

            try:
                # Check if buffer is full before writing? 
                # Kvaser driver handles buffering, but if we write too fast in a loop without consuming, it fills up.
                # Since we are simulating, we can just catch the overflow and sleep a bit more.
                ch.write(msg_id, data)
            except Exception as e:
                if "Transmit buffer overflow" in str(e):
                    time.sleep(0.1) # Back off
                else:
                    print(f"Write error on Ch {ch_idx}: {e}")
            
            time.sleep(interval)
            
    except KeyboardInterrupt:
        pass
    finally:
        ch.busOff()
        ch.close()

if __name__ == "__main__":
    print("Starting CAN Traffic Simulation on Real Interfaces...")
    
    threads = []
    # Try to open up to 4 channels
    num_channels = canlib.getNumberOfChannels()
    print(f"Found {num_channels} channels.")
    
    for i in range(min(num_channels, 4)):
        t = threading.Thread(target=simulate_channel, args=(i,))
        t.daemon = True
        t.start()
        threads.append(t)
        
    if not threads:
        print("No channels found to simulate on.")
    else:
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("Stopping simulation...")
