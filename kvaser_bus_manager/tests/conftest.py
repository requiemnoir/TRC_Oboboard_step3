import os
import sys


# Allow running pytest from inside the `kvaser_bus_manager/` folder.
# The package root is the parent directory of `kvaser_bus_manager/`.
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
