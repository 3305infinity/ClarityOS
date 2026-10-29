import asyncio
import os
import sys

# Add current path to sys.path so we can import moonwalk packages
sys.path.append(os.path.abspath(os.path.dirname(__file__)))
from providers import GeminiProvider

async def main():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        with open('.env', 'r') as f:
            for line in f:
                if line.startswith('GEMINI_API_KEY='):
                    api_key = line.strip().split('=', 1)[1]
    
    print("Initializing Pro model...")
    provider = GeminiProvider(api_key=api_key, model="gemini-3.1-pro-preview-customtools")
    
    # Needs a mock _client since providers.py expects it? Wait, GeminiProvider initializes it itself inside generate() maybe?
    # No, it's initialized in ModelRouter. Let's look at GeminiProvider init.
    print("Is available:", await provider.is_available())
    
    print("Testing generate_content...")
    try:
        response = await asyncio.wait_for(
            provider.generate(
                messages=[{"role": "user", "parts": [{"text": "Hello, this is a test. Respond with exactly the word OK."}]}],
                system_prompt="You are a test agent.",
                tools=[]
            ),
            timeout=30.0
        )
        if response.error:
            print(f"Error: {response.error}")
        else:
            print(f"Success: {response.text}")
    except asyncio.TimeoutError:
        print("Model generated a TimeoutError (hung for over 30s)!")

if __name__ == "__main__":
    asyncio.run(main())
