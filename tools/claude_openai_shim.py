"""OpenAI-compatible shim in front of the Claude Code CLI.

The GraphRAG runtime only speaks OpenAI's chat-completions API, and the pinned
settings.yaml reads its generation endpoint from GENERATION_API_BASE. This
serves that shape and fulfils each request by piping the rendered prompt into
`claude -p` on stdin, so nothing here needs a metered API key and there is no
command-line length limit to trip over on a large map prompt.

Run:  python claude_openai_shim.py            (listens on 127.0.0.1:8770)
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

CLAUDE = os.environ.get("CLAUDE_BIN", r"C:\Users\jluo6\.local\bin\claude.exe")
MODEL = os.environ.get("SHIM_MODEL", "claude-sonnet-5")
CALL_TIMEOUT_S = float(os.environ.get("SHIM_TIMEOUT_S", "600"))

app = FastAPI()


def render_prompt(messages) -> str:
    """Flatten OpenAI messages into one prompt.

    GraphRAG puts its instructions in the system message and the community
    context in the user message; concatenating in order preserves that.
    """
    parts = []
    for message in messages or []:
        content = message.get("content", "")
        if isinstance(content, list):  # OpenAI content-part form
            content = "".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict)
            )
        if not content:
            continue
        role = message.get("role", "user")
        if role == "assistant":
            parts.append("Previous assistant reply:\n" + content)
        else:
            parts.append(content)
    return "\n\n".join(parts)


def call_claude(prompt: str) -> str:
    completed = subprocess.run(
        [CLAUDE, "-p", "--model", MODEL],
        input=prompt.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=CALL_TIMEOUT_S,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()[-800:]
        raise RuntimeError(detail or f"claude exited {completed.returncode}")
    return completed.stdout.decode("utf-8", "replace").strip()


@app.get("/v1/models")
async def models():
    return {
        "object": "list",
        "data": [{"id": MODEL, "object": "model", "owned_by": "anthropic"}],
    }


def _sse(payload) -> str:
    return "data: " + json.dumps(payload) + "\n\n"


def stream_response(completion_id: str, model: str, text: str):
    """Emit an OpenAI chat.completion.chunk stream.

    `claude -p` hands back the whole answer at once, so this is one content
    chunk followed by the stop chunk rather than token-by-token. That is a
    valid SSE stream as far as any OpenAI client is concerned, and GraphRAG's
    global-search REDUCE step requests streaming even though its map calls do
    not — without this the reduce call receives a plain JSON body, fails to
    parse it, and the whole query exits non-zero after every map call has
    already succeeded.
    """
    created = int(time.time())
    head = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
    }
    yield _sse(head)
    body = dict(head)
    body["choices"] = [{"index": 0, "delta": {"content": text}, "finish_reason": None}]
    yield _sse(body)
    tail = dict(head)
    tail["choices"] = [{"index": 0, "delta": {}, "finish_reason": "stop"}]
    yield _sse(tail)
    yield "data: [DONE]\n\n"


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    prompt = render_prompt(body.get("messages"))
    if not prompt:
        return JSONResponse({"error": {"message": "no prompt"}}, status_code=400)

    try:
        # A thread per call: the CLI is a subprocess, so concurrency is real.
        text = await asyncio.to_thread(call_claude, prompt)
    except subprocess.TimeoutExpired:
        return JSONResponse(
            {"error": {"message": f"claude timed out after {CALL_TIMEOUT_S}s"}},
            status_code=504,
        )
    except RuntimeError as exc:
        return JSONResponse({"error": {"message": str(exc)}}, status_code=502)

    model = body.get("model") or MODEL
    if body.get("stream"):
        completion_id = "chatcmpl-" + uuid.uuid4().hex[:24]
        return StreamingResponse(
            stream_response(completion_id, model, text),
            media_type="text/event-stream",
        )

    # Rough token estimate: the caller only uses this for logging/metrics.
    prompt_tokens = max(1, len(prompt) // 4)
    completion_tokens = max(1, len(text) // 4)
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex[:24],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.get("model") or MODEL,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("SHIM_PORT", "8770")))
