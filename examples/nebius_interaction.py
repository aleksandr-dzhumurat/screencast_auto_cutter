
# Running from a subdirectory: make the project root importable
# (docs/refactoring_plan.md) so `agents`, `common`, `ffmpeg_skill` and
# `scripts` resolve no matter where the command is invoked from.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import json
import os
import warnings

from dotenv import load_dotenv
from langsmith import traceable
from langsmith.wrappers import wrap_openai
from openai import OpenAI

# Must run before langsmith reads LANGSMITH_* vars (tracing is enabled via LANGSMITH_TRACING=true)
load_dotenv()

if not os.getenv("LANGSMITH_PROJECT"):
    raise RuntimeError("LANGSMITH_PROJECT is not set or empty; set it in .env")

# Nebius returns `metadata` as a list, while the OpenAI SDK model types it as `str`;
# LangSmith's serialization of the response triggers a harmless pydantic warning.
warnings.filterwarnings(
    "ignore",
    message=r"Pydantic serializer warnings",
    category=UserWarning,
    module=r"pydantic\.main",
)

client = wrap_openai(
    OpenAI(
        base_url=os.getenv("NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1/"),
        api_key=os.environ["NEBIUS_API_KEY"],
    )
)

MODEL = os.getenv("NEBIUS_MODEL", "zai-org/GLM-5.3-Flash")

tools = [{
    "type": "function",
    "function": {
        "name": "get_order",
        "description": "Look up an order by ID. Use when the customer mentions an order.",
        "parameters": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "Order number, e.g. 4512"},
            },
            "required": ["order_id"],
        },
    },
}]

ORDERS = {
    "4512": {"order_id": "4512", "status": "shipped", "eta": "2026-10-10", "items": ["Wireless mouse"]},
    "4513": {"order_id": "4513", "status": "processing", "eta": None, "items": ["USB-C cable", "Keyboard"]},
}


@traceable(run_type="tool", name="get_order")
def get_order(order_id: str) -> dict:
    return ORDERS.get(order_id, {"error": f"Order {order_id} not found"})


TOOL_IMPLS = {"get_order": get_order}


@traceable(run_type="chain", name="run_agent")
def run_agent(question: str) -> str:
    messages = [
        {"role": "system", "content": "You are a helpful customer support agent."},
        {"role": "user", "content": question},
    ]
    while True:
        response = client.chat.completions.create(model=MODEL, messages=messages, tools=tools)
        message = response.choices[0].message
        if not message.tool_calls:
            return message.content

        messages.append(message)
        for call in message.tool_calls:
            args = json.loads(call.function.arguments)
            result = TOOL_IMPLS[call.function.name](**args)
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": json.dumps(result),
            })


def main() -> None:
    test_questions = [
        "Hi! Where is my order 4512 and when will it arrive?",  # tool call, found
        "What items are in order 4513?",  # tool call, found
        "Can you check order 9999?",  # tool call, not found
        "What are your support hours?",  # no tool call
    ]
    for question in test_questions:
        print(f"Q: {question}\nA: {run_agent(question)}\n")


if __name__ == "__main__":
    main()
