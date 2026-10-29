import asyncio
import os
import sys
import time

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
    
    provider = GeminiProvider(api_key=api_key, model='gemini-2.5-pro')
    
    start = time.time()
    try:
        response_stream = await provider._client.aio.models.generate_content_stream(
            model=provider._model,
            contents='You are a calculator. Tell me a 50 word story about an apple, then call add(1,1), then tell me a 50 word story about a banana, then call add(2,2).',
            config=types.GenerateContentConfig(
                tools=[types.Tool(function_declarations=[
                    types.FunctionDeclaration(
                        name='add',
                        description='Add two numbers',
                        parameters={
                            'type': 'OBJECT',
                            'properties': {'a': {'type': 'NUMBER'}, 'b': {'type': 'NUMBER'}},
                            'required': ['a', 'b']
                        }
                    )
                ])]
            )
        )
        async for chunk in response_stream:
            if chunk.candidates and chunk.candidates[0].content and chunk.candidates[0].content.parts:
                for part in chunk.candidates[0].content.parts:
                    if part.function_call:
                        print(f'[{time.time()-start:.2f}s] Function {part.function_call.name}')
                    if part.text:
                        print(f'[{time.time()-start:.2f}s] Text Chunk')
    except Exception as e:
        print(f'Error: {e}')

asyncio.run(main())
