"""Local-first LLM + embedding client.

Default provider is Ollama on localhost (no API cost, no data leaves the machine).
`FILMGRAPH_LLM=none` disables generation entirely: every flow still answers from
graph queries, the LLM only adds NLU fallback, triple extraction and narration.
An OpenAI-compatible endpoint (Groq, Together, vLLM, LM Studio...) can be selected
with FILMGRAPH_LLM=openai + FILMGRAPH_LLM_BASE_URL/KEY; it is never used unless set.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from functools import lru_cache

from . import settings


def _model(name: str) -> str:
    """Provider, hosts and model names come from the settings page (defaults: the env vars above)."""
    return settings.get('models.' + name)


class LLMUnavailable(RuntimeError):
    pass


def _post(url: str, payload: dict, timeout: float, headers: dict | None = None) -> dict:
    request = urllib.request.Request(url, json.dumps(payload).encode(),
                                     {'Content-Type': 'application/json', **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except OSError as exc:  # URLError, timeouts, connection refused
        raise LLMUnavailable(f'{url}: {exc}') from exc


@lru_cache(maxsize=1)
def available() -> bool:
    provider = _model('llm_provider')
    if provider == 'none':
        return False
    if provider == 'openai':
        return bool(_model('llm_base_url'))
    try:
        with urllib.request.urlopen(_model('ollama_host') + '/api/tags', timeout=2) as r:
            return any(m['name'] == _model('chat_model') for m in json.loads(r.read()).get('models', []))
    except OSError:
        return False


def chat(messages: list[dict], schema: dict | None = None, temperature: float = 0,
         max_tokens: int = 512, timeout: float = 120) -> tuple[str | dict, dict]:
    """Return (content, usage). With `schema`, output is constrained JSON and parsed."""
    if not available():
        raise LLMUnavailable('No LLM provider configured or reachable')
    started = time.perf_counter()
    provider, chat_model = _model('llm_provider'), _model('chat_model')
    if provider == 'openai':
        payload = {'model': chat_model, 'messages': messages, 'temperature': temperature, 'max_tokens': max_tokens}
        if schema:
            payload['response_format'] = {'type': 'json_schema', 'json_schema': {'name': 'out', 'schema': schema}}
        data = _post(_model('llm_base_url').rstrip('/') + '/chat/completions', payload, timeout,
                     {'Authorization': 'Bearer ' + os.getenv('FILMGRAPH_LLM_KEY', '')})
        text = data['choices'][0]['message']['content']
        usage = {'prompt_tokens': data.get('usage', {}).get('prompt_tokens'),
                 'completion_tokens': data.get('usage', {}).get('completion_tokens')}
    else:
        payload = {'model': chat_model, 'messages': messages, 'stream': False, 'think': False,
                   'options': {'temperature': temperature, 'num_predict': max_tokens}}
        if schema:
            payload['format'] = schema
        data = _post(_model('ollama_host') + '/api/chat', payload, timeout)
        text = data['message']['content']
        usage = {'prompt_tokens': data.get('prompt_eval_count'), 'completion_tokens': data.get('eval_count')}
    usage.update(model=chat_model, provider=provider, ms=round((time.perf_counter() - started) * 1000))
    if schema:
        try:
            return json.loads(text), usage
        except json.JSONDecodeError as exc:
            raise LLMUnavailable('Model returned invalid JSON') from exc
    return text.strip(), usage


def embed(texts: list[str], timeout: float = 300) -> list[list[float]]:
    """Batch embeddings from the local Ollama embedding model (L2-normalised by Ollama)."""
    data = _post(_model('ollama_host') + '/api/embed', {'model': _model('embed_model'), 'input': texts}, timeout)
    return data['embeddings']
