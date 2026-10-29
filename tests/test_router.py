import asyncio
import os
from model_router import ModelRouter

async def main():
    r = ModelRouter()
    r._initialized = True # Skip dotenv so it uses existing env vars if running in context
    
    # Manually load the API key from .env 
    env_path = os.path.join(os.path.dirname(__file__), '.env')
    try:
        with open(env_path, 'r') as f:
            for line in f:
                if line.startswith('GEMINI_API_KEY='):
                    os.environ['GEMINI_API_KEY'] = line.strip().split('=', 1)[1]
                if line.startswith('GEMINI_ROUTING_MODEL='):
                    os.environ['GEMINI_ROUTING_MODEL'] = line.strip().split('=', 1)[1]
                if line.startswith('GEMINI_FAST_MODEL='):
                    os.environ['GEMINI_FAST_MODEL'] = line.strip().split('=', 1)[1]
                if line.startswith('GEMINI_POWERFUL_MODEL='):
                    os.environ['GEMINI_POWERFUL_MODEL'] = line.strip().split('=', 1)[1]
    except FileNotFoundError:
        print("No .env found!")
        return
                
    await r.initialize()
    
    # Test 1: Simple
    try:
        res1 = await r.route('what time is it?')
        print(f'Test 1 (Simple) -> {res1.tier}')
    except Exception as e:
        print(f"Test 1 Error: {e}")
    
    # Test 2: Complex
    try:
        res2 = await r.route('can you read the code visible on my screen and write a test for the active python file?')
        print(f'Test 2 (Complex) -> {res2.tier}')
    except Exception as e:
        print(f"Test 2 Error: {e}")

if __name__ == "__main__":
    asyncio.run(main())
