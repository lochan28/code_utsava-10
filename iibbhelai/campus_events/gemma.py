"""Tiny wrapper around Gemma 4 (via the Google AI Studio API).

Put GEMINI_API_KEY=... in a .env file next to this file. The key is never printed.
"""
import json
import os
import re

from dotenv import load_dotenv

load_dotenv()

# ---- Tunable values --------------------------------------------------------
MODEL = "gemma-4-26b-a4b-it"   # the Gemma 4 model we use for everything
TEMPERATURE = 0.2              # lower = more predictable JSON, higher = more creative

_client = None


def client():
    """Create the API client once (lazy, so the app starts even without a key)."""
    global _client
    if _client is None:
        from google import genai
        key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not key:
            raise RuntimeError("No GEMINI_API_KEY found. Add it to .env")
        _client = genai.Client(api_key=key)
    return _client


def ask(parts, tools=None):
    """Send text (and optionally images) to Gemma, return the reply text."""
    from google.genai import types
    config = types.GenerateContentConfig(temperature=TEMPERATURE, tools=tools)
    resp = client().models.generate_content(model=MODEL, contents=parts, config=config)
    return resp.text or ""


def parse_json(text):
    """Pull the first {...} block out of a model reply. Raises ValueError if none."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("Gemma did not return JSON:\n" + text[:300])
    return json.loads(match.group(0))
