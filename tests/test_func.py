import os
import asyncio
from google import genai
from google.genai import types

async def test_func():
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
    
    messages = [
        {"role": "user", "parts": ["test tool"]},
        {"role": "model", "parts": [types.Part.from_function_call(name="test_tool", args={"foo": "bar"})]},
        {"role": "user", "parts": [{
            "function_response": {
                "name": "test_tool",
                "response": {"result": "success"}
            }
        }]}
    ]
    
    print("Testing dict-based function_response...")
    try:
        stream = await client.aio.models.generate_content_stream(
            model="gemini-3-flash-preview",
            contents=messages,
            config=types.GenerateContentConfig(
                tools=[types.Tool(function_declarations=[
                    types.FunctionDeclaration(name="test_tool", description="test", parameters={"type":"object"})
                ])]
            )
        )
        async for chunk in stream:
            print("Chunk:", chunk.text)
    except Exception as e:
        print("Dict error:", type(e), e)

    print("\nTesting Part.from_function_response...")
    messages[2]["parts"] = [types.Part.from_function_response(name="test_tool", response={"result": "success"})]
    try:
        stream = await client.aio.models.generate_content_stream(
            model="gemini-3-flash-preview",
            contents=messages,
            config=types.GenerateContentConfig()
        )
        async for chunk in stream:
            print("Chunk:", chunk.text)
    except Exception as e:
        print("Part error:", type(e), e)

asyncio.run(test_func())
