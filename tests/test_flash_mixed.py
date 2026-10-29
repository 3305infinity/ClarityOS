import asyncio
import os
import sys

sys.path.append(os.path.abspath('.'))

from providers import GeminiProvider
from google.genai import types

async def main():
    api_key = os.environ.get('GEMINI_API_KEY')
    if not api_key:
        with open('.env', 'r') as f:
            for line in f:
                if line.startswith('GEMINI_API_KEY='):
                    api_key = line.strip().split('=', 1)[1]
    
    provider = GeminiProvider(api_key=api_key, model='gemini-3-flash-preview')
    
    # Simulate turn 1: model calls add
    messages = [
        {'role': 'user', 'parts': [{'text': 'What is 5+3?'}]},
        {'role': 'model', 'parts': [{'function_call': {'name': 'add', 'args': {'a': 5, 'b': 3}}}]},
        # Simulate turn 2: our mixed parts payload
        {'role': 'user', 'parts': [
            {'function_response': {'name': 'add', 'response': {'result': 8}}},
            {'text': '[Active Window: Terminal]'} # the minimal context from perception.py
        ]}
    ]
    
    print('Testing generate_content with mixed functionResponse and text...')
    try:
        response = await asyncio.wait_for(
            provider.generate(
                messages=messages,
                system_prompt='You are a calculator.',
                tools=[{
                    'name': 'add',
                    'description': 'Add',
                    'parameters': {
                        'type': 'OBJECT',
                        'properties': {'a': {'type': 'NUMBER'}, 'b': {'type': 'NUMBER'}}
                    }
                }],
            ),
            timeout=10.0
        )
        print(f'Success! Response: {response.text}')
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f'Crash Error: {type(e).__name__} - {e}')

asyncio.run(main())
