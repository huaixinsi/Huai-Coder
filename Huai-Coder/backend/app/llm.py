import httpx
from .config import get_settings

async def complete(prompt: str) -> str:
    settings = get_settings()
    if not (settings.llm_base_url and settings.llm_api_key and settings.llm_model):
        return f"Received: {prompt}\n\nTry /list ., /read README.md, or /grep FastAPI backend"
    endpoint = settings.llm_base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {settings.llm_api_key}"}
    payload = {"model": settings.llm_model, "messages": [{"role": "user", "content": prompt}], "stream": False}
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(endpoint, headers=headers, json=payload)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]
