import os
import asyncio
from dotenv import load_dotenv

load_dotenv()
from google import genai
from google.genai import types

async def test_providers_way():
    client = genai.Client()
    
    # Simulate EXACTLY what is built in agent.py
    messages = [
        {"role": "user", "parts": ["hello world, what time is it?"]},
        {"role": "model", "parts": [types.Part.from_function_call(name="run_shell", args={"command": "date"})]},
        {"role": "user", "parts": [{
            "function_response": {
                "name": "run_shell",
                "response": {"result": "Sun Mar  1 12:00:00 PST 2026"}
            }
        }]}
    ]

    contents = [dict(m) for m in messages]

    print("Requesting...")
    try:
        stream = await client.aio.models.generate_content_stream(
            model="gemini-3-flash-preview",
            contents=contents,
            config=types.GenerateContentConfig(
                tools=[types.Tool(function_declarations=[
                    types.FunctionDeclaration(name="run_shell", description="x", parameters={"type":"object"})
                ])]
            )
        )
        print("Stream instantiated successfully!")
        stream_iter = stream.__aiter__()
        chunk = await asyncio.wait_for(stream_iter.__anext__(), timeout=5.0)
        print("Chunk:", chunk.text)
    except Exception as e:
        print(f"Exception exactly: {type(e)} - {str(e)}")

asyncio.run(test_providers_way())
