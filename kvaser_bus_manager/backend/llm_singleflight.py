import threading

# Global single-flight lock for all LLM inference requests.
# Kept in a standalone module to avoid circular imports between Flask app and helpers.
LLM_SINGLEFLIGHT_LOCK = threading.Lock()
