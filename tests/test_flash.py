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
    
    print('Initializing Flash model...')
    provider = GeminiProvider(api_key=api_key, model='gemini-3-flash-preview')
    
    print('Testing generate_content with tools...')
    try:
        response = await asyncio.wait_for(
            provider.generate(
                messages=[{'role': 'user', 'parts': [{'text': 'What is the sum of 5 and 3?'}]}],
                system_prompt='You are a calculator.',
                tools=[{
                    'name': 'add',
                    'description': 'Add two numbers',
                    'parameters': {
                        'type': 'OBJECT',
                        'properties': {
                            'a': {'type': 'NUMBER'},
                            'b': {'type': 'NUMBER'}
                        },
                        'required': ['a', 'b']
                    }
                }],
            ),
            timeout=30.0
        )
        if response.error:
            print(f'Error: {response.error}')
        else:
            print(f'Success! Tool calls: {response.has_tool_calls}')
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f'Crash Error: {e}')

asyncio.run(main())
