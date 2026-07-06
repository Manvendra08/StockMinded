import logging, sys
logging.basicConfig(stream=sys.stdout, level=logging.WARNING)
from data.ai_scraper import _get_ai_config, _create_curl_cffi_llm_session

config = _get_ai_config()
session, backend = _create_curl_cffi_llm_session()
url = 'https://opencode.ai/zen/v1/chat/completions'
headers = {
    'Authorization': f'Bearer {config["opencode_api_key"]}',
    'Content-Type': 'application/json',
}
payload = {
    'model': 'big-pickle',
    'messages': [{'role': 'user', 'content': 'What is 2+2? Reply with just the number.'}],
    'max_tokens': 50,
}
resp = session.post(url, headers=headers, json=payload, timeout=20)
print(f'HTTP {resp.status_code}')
data = resp.json()
print('Content:', repr(data['choices'][0]['message']['content']))
print('Finish reason:', data['choices'][0]['finish_reason'])
usage = data.get('usage', {})
print('Usage:', {k: usage[k] for k in ('prompt_tokens','completion_tokens','total_tokens') if k in usage})
if 'completion_tokens_details' in usage:
    print('Reasoning tokens:', usage['completion_tokens_details'].get('reasoning_tokens'))
