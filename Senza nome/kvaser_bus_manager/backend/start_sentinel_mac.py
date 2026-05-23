import sys
import os
from unittest.mock import MagicMock

# 1. Mock canlib to bypass libcanlib.so requirement on macOS
mock_canlib = MagicMock()
sys.modules['canlib'] = mock_canlib
sys.modules['canlib.canlib'] = mock_canlib

# 2. Add backend to sys.path
backend_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, backend_dir)

# 3. Patch Flask to force port 5002
import flask
original_run = flask.Flask.run

def patched_run(self, host=None, port=None, **options):
    print(f"[Wrapper] Forcing port 5002 (original was {port})")
    return original_run(self, host=host, port=5002, **options)

flask.Flask.run = patched_run

# 4. Import and run the app
if __name__ == '__main__':
    print("[Wrapper] Starting Sentinel with mocked canlib and forced port 5002...")
    import app
    app.main()
