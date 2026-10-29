import os
import asyncio
from dotenv import load_dotenv

load_dotenv()
from google import genai
from google.genai import types

async def test_func():
    client = genai.Client()
    
    raw_parts = [
        types.Part.from_function_call(name="write_file", args={"path": "foo"})
    ]

    # Exactly what agent.py builds:
    tool_response_parts = [{
        "function_response": {
            "name": "write_file",
            "response": {"result": "success"}
        }
    }]

    messages = [
        {"role": "user", "parts": ["hello"]},
        {"role": "model", "parts": raw_parts},
        {"role": "user", "parts": tool_response_parts}
    ]

    contents = [dict(m) for m in messages]

    print("Requesting stream...")
    try:
        stream = await client.aio.models.generate_content_stream(
            model="gemini-3-flash-preview",
            contents=contents,
            config=types.GenerateContentConfig(
                tools=[types.Tool(function_declarations=[
                    types.FunctionDeclaration(name="write_file", description="x", parameters={"type":"object"})
                ])]
            )
        )
        print("Stream instantiated successfully!")
        
        async def iterate_w_timeout():
            stream_iter = stream.__aiter__()
            while True:
                chunk = await asyncio.wait_for(stream_iter.__anext__(), timeout=5.0)
                yield chunk

        async for chunk in iterate_w_timeout():
            print("Ch:", chunk.text)
    except Exception as e:
        print(f"Exception exactly: {type(e)} - {str(e)}")

asyncio.run(test_func())
