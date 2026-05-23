
import os
import sys
import time
import tempfile
import zipfile
from flask import Flask

# Add current dir to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app import app, _iter_log_folders, _find_log_file

def test_download():
    # Find a part file
    part_file = None
    for folder in _iter_log_folders():
        if not os.path.exists(folder): continue
        for f in os.listdir(folder):
            if '_part0000.mf4' in f:
                part_file = f
                break
        if part_file: break
    
    if not part_file:
        print("No part file found to test.")
        return

    print(f"Testing download for: {part_file}")
    
    with app.test_request_context(f'/api/logs/{part_file}'):
        # Call the logic of download_log manually to debug (the route function)
        # We need to import the function or use app.view_functions
        view_func = app.view_functions['download_log']
        
        start = time.time()
        try:
            response = view_func(part_file)
            print(f"Response status: {response.status}")
            if response.status_code == 200:
                print("Download successful (simulated)")
                # Check if it's a stream or file
                if hasattr(response, 'direct_passthrough'):
                     print(f"Direct passthrough: {response.direct_passthrough}")
        except Exception as e:
            print(f"Error during download: {e}")
            import traceback
            traceback.print_exc()
        
        end = time.time()
        print(f"Duration: {end - start:.2f} seconds")

if __name__ == "__main__":
    test_download()
