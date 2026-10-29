import asyncio
import os
import sys
import time

sys.path.append(os.path.abspath('.'))

from providers import GeminiProvider
from google.genai import types

# Mock tools
mock_tools = [
    {
        "name": "click_element",
        "description": "Click an element on the screen.",
        "parameters": {"type": "OBJECT", "properties": {"element_name": {"type": "STRING"}}, "required": ["element_name"]}
    },
    {
        "name": "type_text",
        "description": "Type text into the focused field.",
        "parameters": {"type": "OBJECT", "properties": {"text": {"type": "STRING"}}, "required": ["text"]}
    },
    {
        "name": "read_screen",
        "description": "Read the current screen context.",
        "parameters": {"type": "OBJECT", "properties": {}}
    },
    {
        "name": "send_response",
        "description": "Send the final response to the user when the task is complete.",
        "parameters": {"type": "OBJECT", "properties": {"message": {"type": "STRING"}}, "required": ["message"]}
    }
]

async def execute_mock_tool(name, args):
    if name == "read_screen":
        return "Login Screen. Elements: [Username Input], [Password Input], [Login Button]"
    elif name == "click_element":
        return f"Clicked {args.get('element_name')}"
    elif name == "type_text":
        return f"Typed '{args.get('text')}'"
    elif name == "send_response":
        return f"RESPONSE:{args.get('message')}"
    return "Success"

async def run_monolithic_benchmark(provider, task_prompt):
    print(f"\n--- Running Monolithic Benchmark ({provider.name}) ---")
    start_time = time.time()
    
    messages = [{"role": "user", "parts": [{"text": task_prompt}]}]
    iterations = 0
    llm_times = []
    
    while iterations < 10:
        iterations += 1
        t0 = time.time()
        
        response = await provider.generate(
            messages=messages,
            system_prompt="You are an autonomous agent. Use tools to complete the user's task. Always use read_screen first to understand the UI.",
            tools=mock_tools
        )
        
        t_llm = time.time() - t0
        llm_times.append(t_llm)
        print(f"  Iter {iterations} LLM Time: {t_llm:.2f}s | Tool calls: {len(response.tool_calls)}")
        
        if response.error:
            print(f"  Error: {response.error}")
            break
            
        if not response.has_tool_calls:
            print(f"  Model stopped using tools. Output: {response.text}")
            break
            
        # Append mock model response
        if response.raw_model_parts:
            messages.append({"role": "model", "parts": response.raw_model_parts})
            
        # Execute tools
        tool_responses = []
        finished = False
        for tc in response.tool_calls:
            res = await execute_mock_tool(tc.name, tc.args)
            if res.startswith("RESPONSE:"):
                finished = True
                continue
            tool_responses.append({
                "function_response": {
                    "name": tc.name,
                    "response": {"result": res}
                }
            })
            
        if finished:
            print(f"  Task Completed!")
            break
            
        if tool_responses:
            messages.append({"role": "user", "parts": tool_responses})
            
    total_time = time.time() - start_time
    print(f"Monolithic Total Time: {total_time:.2f}s (Avg LLM: {sum(llm_times)/len(llm_times):.2f}s)")
    return total_time

async def run_dual_agent_benchmark(strategist, worker, task_prompt):
    print(f"\n--- Running Dual-Agent Benchmark (Strategist: {strategist.name}, Worker: {worker.name}) ---")
    start_time = time.time()
    
    # 1. Plan Phase
    print("  Phase 1: Strategist Planning...")
    t0 = time.time()
    # Provide the initial screen state mocked out to the planner
    screen_state = "Login Screen. Elements: [Username Input], [Password Input], [Login Button]"
    plan_prompt = f"Task: {task_prompt}\nScreen State: {screen_state}\nProvide a strict 4-step execution plan."
    
    plan_response = await strategist.generate(
        messages=[{"role": "user", "parts": [{"text": plan_prompt}]}],
        system_prompt="You are a Strategist. Write a step-by-step execution plan. Do NOT use tools. Output text only.",
        tools=[]
    )
    t_plan = time.time() - t0
    print(f"  Strategist Time: {t_plan:.2f}s")
    plan = plan_response.text
    print(f"  Plan generated:\n{plan}\n")
    
    # 2. Execution Phase
    print("  Phase 2: Worker Executing Plan...")
    messages = [{"role": "user", "parts": [{"text": f"Execute this plan step-by-step:\n{plan}"}]}]
    iterations = 0
    worker_times = []
    
    while iterations < 10:
        iterations += 1
        t0 = time.time()
        
        response = await worker.generate(
            messages=messages,
            system_prompt="You are a fast executing worker. Execute the next step in the plan using tools.",
            tools=mock_tools
        )
        
        t_llm = time.time() - t0
        worker_times.append(t_llm)
        print(f"  Worker Iter {iterations} Time: {t_llm:.2f}s | Tool calls: {len(response.tool_calls)}")
        
        if response.error or not response.has_tool_calls:
            break
            
        if response.raw_model_parts:
            messages.append({"role": "model", "parts": response.raw_model_parts})
            
        tool_responses = []
        finished = False
        for tc in response.tool_calls:
            res = await execute_mock_tool(tc.name, tc.args)
            if res.startswith("RESPONSE:"):
                finished = True
                continue
            tool_responses.append({"function_response": {"name": tc.name, "response": {"result": res}}})
            
        if finished:
            print(f"  Task Completed!")
            break
            
        if tool_responses:
            messages.append({"role": "user", "parts": tool_responses})

    total_time = time.time() - start_time
    print(f"Dual-Agent Total Time: {total_time:.2f}s (Plan: {t_plan:.2f}s, Avg Worker: {sum(worker_times)/len(worker_times):.2f}s)")
    return total_time

async def main():
    api_key = os.environ.get('GEMINI_API_KEY')
    if not api_key:
        with open('.env', 'r') as f:
            for line in f:
                if line.startswith('GEMINI_API_KEY='):
                    api_key = line.strip().split('=', 1)[1]
    
    # Init providers: We'll benchmark Pro vs Flash
    pro_provider = GeminiProvider(api_key=api_key, model='gemini-2.5-pro')
    # Use 3.1 Pro if you really want to test its latency, but it might hang:
    # pro_provider = GeminiProvider(api_key=api_key, model='gemini-3.1-pro-preview-customtools')
    flash_provider = GeminiProvider(api_key=api_key, model='gemini-3-flash-preview')
    
    task_prompt = "Login to the portal using username 'admin' and password 'secret'."
    
    # Test 1: Monolithic Pro (Current Setup)
    # await run_monolithic_benchmark(pro_provider, task_prompt)
    
    # Test 2: Dual Agent (Strategist Pro -> Worker Flash)
    # await run_dual_agent_benchmark(pro_provider, flash_provider, task_prompt)

    # Let's run them!
    print("Starting Benchmark Suite...")
    try:
        await run_monolithic_benchmark(pro_provider, task_prompt)
        await run_dual_agent_benchmark(pro_provider, flash_provider, task_prompt)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"Error during benchmark: {e}")

if __name__ == "__main__":
    asyncio.run(main())
