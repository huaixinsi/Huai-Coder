import httpx
from .config import get_settings

async def complete(prompt: str) -> str:
    settings = get_settings()
    if not (settings.llm_base_url and settings.llm_api_key and settings.llm_model):
        return f"Received: {prompt}\n\nTry /list ., /read README.md, or /grep FastAPI backend"
    endpoint = settings.llm_base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {settings.llm_api_key}"}
    payload = {"model": settings.llm_model, "messages": [{"role": "system", "content": "你是项目代码分析助手。只根据提供的项目上下文回答，不要输出 XML、DSML、tool_calls 或伪造的工具调用；如果上下文中没有足够信息，明确说明缺失内容。不要输出任何密钥、密码、Token 或凭证值。"}, {"role": "user", "content": prompt}], "stream": False}
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(endpoint, headers=headers, json=payload)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]
