from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple


def _env_bool(name: str, default: bool = False) -> bool:
    v = str(os.getenv(name, '1' if default else '0')).strip().lower()
    return v in {'1', 'true', 'yes', 'on'}


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None:
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None:
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _json_truncate(obj: Any, max_chars: int) -> str:
    try:
        # Compact JSON drastically reduces prompt tokens and improves latency on CPU.
        s = json.dumps(obj, ensure_ascii=False, separators=(',', ':'))
    except Exception:
        try:
            s = json.dumps(obj, ensure_ascii=False, separators=(',', ':'))
        except Exception:
            s = str(obj)
    if max_chars and len(s) > max_chars:
        return s[: max(0, int(max_chars) - 3)] + '...'
    return s


def _http_json(
    method: str,
    url: str,
    payload: Optional[Dict[str, Any]] = None,
    *,
    headers: Optional[Dict[str, str]] = None,
    timeout_s: float = 30.0,
) -> Tuple[int, Dict[str, Any]]:
    body = None
    req_headers = {
        'Accept': 'application/json',
    }
    if headers:
        req_headers.update({str(k): str(v) for k, v in headers.items()})

    if payload is not None:
        body = json.dumps(payload).encode('utf-8')
        req_headers['Content-Type'] = 'application/json'

    req = urllib.request.Request(url=url, data=body, headers=req_headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=float(timeout_s)) as resp:
            status = int(getattr(resp, 'status', 200) or 200)
            raw = resp.read() or b''
            if not raw:
                return status, {}
            try:
                return status, json.loads(raw.decode('utf-8', errors='ignore'))
            except Exception:
                return status, {'raw': raw.decode('utf-8', errors='ignore')}
    except urllib.error.HTTPError as e:
        try:
            raw = e.read() or b''
            msg = raw.decode('utf-8', errors='ignore')
        except Exception:
            msg = str(e)
        return int(getattr(e, 'code', 500) or 500), {'error': msg}
    except Exception as e:
        return 0, {'error': str(e)}


class CopilotAgent:
    """Tiny client for local LLM providers.

    Default provider is Ollama on localhost.

    Env vars:
      - COPILOT_PROVIDER: ollama|openai (openai = OpenAI-compatible server)
      - COPILOT_BASE_URL: e.g. http://127.0.0.1:11434
      - COPILOT_MODEL: e.g. llama3.2:3b
      - COPILOT_TIMEOUT_S: request timeout
      - COPILOT_API_KEY: used only for openai provider
      - COPILOT_DEBUG: if true, includes raw provider replies in output
    """

    def __init__(
        self,
        *,
        provider: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout_s: Optional[float] = None,
    ):
        self.provider = (provider or os.getenv('COPILOT_PROVIDER') or 'ollama').strip().lower() or 'ollama'
        self.base_url = (base_url or os.getenv('COPILOT_BASE_URL') or 'http://127.0.0.1:11434').strip().rstrip('/')
        self.model = (model or os.getenv('COPILOT_MODEL') or 'llama3.2:3b').strip()
        self.timeout_s = float(timeout_s) if timeout_s is not None else _env_float('COPILOT_TIMEOUT_S', 30.0)
        self.debug = _env_bool('COPILOT_DEBUG', False)
        self.num_predict = _env_int('COPILOT_NUM_PREDICT', 256)
        # Some Ollama models (e.g. lb634-diag, qwen3) emit a separate <think>
        # block before the actual answer.  With a small num_predict budget the
        # whole budget is consumed by the reasoning and the visible content
        # comes back empty.  Disable thinking by default; opt-in via env.
        self.think = _env_bool('COPILOT_THINK', False)

    def status(self) -> Dict[str, Any]:
        started = time.time()
        if self.provider == 'ollama':
            st, data = _http_json('GET', f"{self.base_url}/api/version", None, timeout_s=min(self.timeout_s, 5.0))
            ok = st == 200 and isinstance(data, dict) and bool(data.get('version'))
            return {
                'ok': bool(ok),
                'provider': 'ollama',
                'base_url': self.base_url,
                'model': self.model,
                'version': data.get('version') if isinstance(data, dict) else None,
                'latency_ms': int((time.time() - started) * 1000),
                'error': None if ok else (data.get('error') if isinstance(data, dict) else 'unreachable'),
            }

        if self.provider == 'openai':
            headers = {}
            api_key = str(os.getenv('COPILOT_API_KEY') or '').strip()
            if api_key:
                headers['Authorization'] = f"Bearer {api_key}"
            st, data = _http_json('GET', f"{self.base_url}/v1/models", None, headers=headers, timeout_s=min(self.timeout_s, 5.0))
            ok = st == 200
            return {
                'ok': bool(ok),
                'provider': 'openai',
                'base_url': self.base_url,
                'model': self.model,
                'latency_ms': int((time.time() - started) * 1000),
                'error': None if ok else (data.get('error') if isinstance(data, dict) else 'unreachable'),
            }

        return {'ok': False, 'provider': self.provider, 'error': 'unknown provider'}

    def chat(
        self,
        *,
        system: str,
        user: str,
        context: Optional[Dict[str, Any]] = None,
        temperature: float = 0.2,
        max_context_chars: Optional[int] = None,
        model: Optional[str] = None,
        timeout_s: Optional[float] = None,
        num_predict: Optional[int] = None,
    ) -> Dict[str, Any]:
        sys_msg = str(system or '').strip()
        user_msg = str(user or '').strip()
        ctx = context if isinstance(context, dict) else {}

        max_chars = int(max_context_chars) if max_context_chars is not None else _env_int('COPILOT_MAX_CONTEXT_CHARS', 12000)
        ctx_str = _json_truncate(ctx, max_chars)

        use_model = str(model or self.model).strip() or self.model
        use_timeout = float(timeout_s) if timeout_s is not None else float(self.timeout_s)
        use_num_predict = int(num_predict) if num_predict is not None else int(self.num_predict)

        messages: List[Dict[str, str]] = [
            {'role': 'system', 'content': sys_msg},
            {'role': 'system', 'content': f"Snapshot (JSON, may be truncated):\n{ctx_str}"},
            {'role': 'user', 'content': user_msg},
        ]

        if self.provider == 'ollama':
            payload = {
                'model': use_model,
                'stream': False,
                'messages': messages,
                'think': bool(self.think),
                'options': {
                    'temperature': float(temperature),
                    # Keep responses bounded for CPU-only inference.
                    'num_predict': int(use_num_predict),
                },
            }
            st, data = _http_json('POST', f"{self.base_url}/api/chat", payload, timeout_s=use_timeout)
            if st != 200:
                return {
                    'ok': False,
                    'provider': 'ollama',
                    'error': (data.get('error') if isinstance(data, dict) else 'request failed'),
                }

            content = None
            thinking = None
            try:
                msg = data.get('message') if isinstance(data, dict) else None
                if isinstance(msg, dict):
                    content = msg.get('content')
                    thinking = msg.get('thinking')
            except Exception:
                content = None
                thinking = None

            # Defensive fallback: if the model returned only the reasoning
            # block (e.g. think=True with a tight num_predict budget), surface
            # the thinking text rather than an empty reply.
            final_content = str(content or '').strip()
            if not final_content and thinking:
                final_content = str(thinking).strip()

            out = {
                'ok': True,
                'provider': 'ollama',
                'model': use_model,
                'content': final_content,
            }
            if self.debug:
                out['raw'] = data
            return out

        if self.provider == 'openai':
            headers = {}
            api_key = str(os.getenv('COPILOT_API_KEY') or '').strip()
            if api_key:
                headers['Authorization'] = f"Bearer {api_key}"
            payload = {
                'model': self.model,
                'temperature': float(temperature),
                'messages': messages,
            }
            st, data = _http_json('POST', f"{self.base_url}/v1/chat/completions", payload, headers=headers, timeout_s=self.timeout_s)
            if st != 200:
                return {'ok': False, 'provider': 'openai', 'error': (data.get('error') if isinstance(data, dict) else 'request failed')}
            content = ''
            try:
                choices = data.get('choices') if isinstance(data, dict) else None
                if isinstance(choices, list) and choices:
                    msg = choices[0].get('message')
                    if isinstance(msg, dict):
                        content = str(msg.get('content') or '')
            except Exception:
                content = ''
            out = {'ok': True, 'provider': 'openai', 'model': self.model, 'content': content.strip()}
            if self.debug:
                out['raw'] = data
            return out

        return {'ok': False, 'provider': self.provider, 'error': 'unknown provider'}
