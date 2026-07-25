"""
Multi-provider LLM sandbox.

Set in .env:
  TEST_PROVIDER=groq        # uses GROQ_API_KEY + GROQ_TEST_MODEL (or default chain)
  TEST_PROVIDER=gemini      # uses GEMINI_API_KEY
  TEST_PROVIDER=deepseek    # uses DEEPSEEK_API_KEY + DEEPSEEK_MODEL (default: deepseek-chat)
  TEST_PROVIDER=openai      # uses OPENAI_API_KEY + OPENAI_MODEL (default: gpt-4o-mini)

Leave TEST_PROVIDER blank or unset to use the default Groq production chain.
"""

import os


async def triage(text: str) -> str:
    provider = os.getenv("TEST_PROVIDER", "").strip().lower()

    if provider == "gemini":
        from .llm_gemini import triage as _t
        return await _t(text)

    if provider == "deepseek":
        return await _triage_openai_compat(
            text,
            base_url="https://api.deepseek.com",
            api_key=os.getenv("DEEPSEEK_API_KEY", ""),
            model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
            provider_name="DeepSeek",
        )

    if provider == "openai":
        return await _triage_openai_compat(
            text,
            base_url="https://api.openai.com/v1",
            api_key=os.getenv("OPENAI_API_KEY", ""),
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            provider_name="OpenAI",
        )

    if provider == "nvidia":
        return await _triage_openai_compat(
            text,
            base_url="https://integrate.api.nvidia.com/v1",
            api_key=os.getenv("NVIDIA_API_KEY", ""),
            model=os.getenv("NVIDIA_MODEL", "deepseek-ai/deepseek-v4-pro"),
            provider_name="NVIDIA",
        )

    # Default: Groq (production chain)
    from .llm import triage as _t
    return await _t(text)


async def _triage_openai_compat(text: str, base_url: str, api_key: str, model: str, provider_name: str) -> str:
    """Generic handler for any OpenAI-compatible API (DeepSeek, OpenAI, NVIDIA NIM, etc.)"""
    from openai import AsyncOpenAI
    from .llm import SYSTEM_PROMPT

    text = text.strip()[:500]
    try:
        client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        extra = {"extra_body": {"chat_template_kwargs": {"thinking": False}}} if provider_name == "NVIDIA" else {}
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            max_tokens=200,
            temperature=0.8,
            top_p=0.95,
            **extra,
        )
        result = resp.choices[0].message.content.strip()
        print(f"[LLM] provider={provider_name} model={model} intent={result!r:.60}")
        return result
    except Exception as e:
        print(f"[LLM ERROR] {provider_name}/{model}: {e}")
        # Fall back to Groq production chain
        from .llm import triage as _t
        return await _t(text)
