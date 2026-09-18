#!/usr/bin/env python3
"""Agente A2A - Central de Salas Hill Valley Tech.

Implementa A2A v1.0 na porta 7300, chamando o servidor MCP internamente.

Uso:
    REQUEST_STATE_SECRET=<hex> python agente/agent.py
"""

from __future__ import annotations

import json
import os
import secrets
import sys

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

MCP_URL = os.environ.get("MCP_URL", "http://localhost:7301/mcp")
A2A_PORT = int(os.environ.get("A2A_PORT", "7300"))
A2A_HOST = os.environ.get("A2A_HOST", "127.0.0.1")
A2A_ENDPOINT = f"http://{A2A_HOST}:{A2A_PORT}/a2a"

MCP_VERSION = "2026-07-28"
MCP_CLIENT_INFO = {"name": "agente-central-de-salas", "version": "1.0.0"}
MCP_CAPS = {"elicitation": {"form": {}}}
CHAVE_MCP = "__main__:escolha_de_sala"

# In-memory state — both dicts keyed by task_id
tasks: dict[str, dict] = {}
paused: dict[str, dict] = {}  # task_id → {request_state, chave, alternativas, args}

TERMINAL = {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _mid() -> str:
    return f"msg-{secrets.token_hex(6)}"


def _tid() -> str:
    return f"task-{secrets.token_hex(6)}"


def _cid() -> str:
    return f"ctx-{secrets.token_hex(6)}"


def _aid() -> str:
    return f"art-{secrets.token_hex(6)}"


def _agent_msg(text: str, task_id: str, ctx_id: str) -> dict:
    return {
        "messageId": _mid(),
        "role": "ROLE_AGENT",
        "parts": [{"text": text}],
        "taskId": task_id,
        "contextId": ctx_id,
    }


def _user_msg(msg_id: str, text: str, task_id: str | None = None) -> dict:
    m: dict = {"messageId": msg_id, "role": "ROLE_USER", "parts": [{"text": text}]}
    if task_id:
        m["taskId"] = task_id
    return m


def _artifact(data: dict) -> dict:
    fields = ["reserva", "sala", "inicio", "fim", "responsavel", "politica"]
    filtered = {k: v for k, v in data.items() if k in fields and v is not None}
    return {"artifactId": _aid(), "name": "reserva", "parts": [{"text": json.dumps(filtered, ensure_ascii=False)}]}


def _text_of(result: dict) -> str:
    return " ".join(p.get("text", "") for p in result.get("content", []))


def _alts_from_result(result: dict) -> list[str]:
    input_requests = result.get("inputRequests") or {}
    chave = next(iter(input_requests), CHAVE_MCP)
    params = (input_requests.get(chave) or {}).get("params") or {}
    schema = params.get("requestedSchema") or {}
    properties = (schema.get("properties") or {})
    sala_schema = properties.get("sala") or {}
    enum = sala_schema.get("enum")
    if enum:
        return enum
    if "const" in sala_schema:
        return [sala_schema["const"]]
    return []


# ---------------------------------------------------------------------------
# MCP client
# ---------------------------------------------------------------------------
async def _mcp(params: dict, traceparent: str | None = None) -> dict:
    meta: dict = {
        "io.modelcontextprotocol/protocolVersion": MCP_VERSION,
        "io.modelcontextprotocol/clientInfo": MCP_CLIENT_INFO,
        "io.modelcontextprotocol/clientCapabilities": MCP_CAPS,
    }
    if traceparent:
        meta["traceparent"] = traceparent

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": MCP_VERSION,
        "Mcp-Method": "tools/call",
        "Mcp-Name": "reservar_sala",
    }
    body = {
        "jsonrpc": "2.0",
        "id": secrets.token_hex(6),
        "method": "tools/call",
        "params": {**params, "_meta": meta},
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(MCP_URL, json=body, headers=headers)
        return resp.json()


# ---------------------------------------------------------------------------
# Message parsing
# ---------------------------------------------------------------------------
def _parse_reservar(text: str) -> dict | None:
    import re
    kvs = {m.group(1): m.group(2) for m in re.finditer(r"(\w+)=(\S+)", text)}
    if all(k in kvs for k in ("sala", "inicio", "fim", "responsavel")):
        return kvs
    return None


def _parse_escolha(text: str) -> str | None:
    t = text.strip()
    if t.startswith("escolha="):
        return t[len("escolha="):]
    return None


# ---------------------------------------------------------------------------
# SendMessage handler
# ---------------------------------------------------------------------------
async def _handle_send(params: dict, traceparent: str | None) -> dict:
    message = params.get("message") or {}
    msg_id = message.get("messageId") or _mid()
    task_id_in = message.get("taskId")
    parts = message.get("parts") or []
    text = " ".join(p.get("text", "") for p in parts).strip()

    # ---- Continuation ----
    if task_id_in:
        task = tasks.get(task_id_in)
        if task is None:
            return {"error": {"code": -32602, "message": f"Task nao encontrada: {task_id_in}"}}
        state = (task.get("status") or {}).get("state", "")
        if state in TERMINAL:
            return {"error": {"code": -32600, "message": f"Task ja encerrada ({state})"}}

        # Add user message to history first
        task["history"].append(_user_msg(msg_id, text, task_id=task_id_in))

        if state != "TASK_STATE_INPUT_REQUIRED":
            return {"error": {"code": -32600, "message": "Task nao aguarda input"}}

        ps = paused.get(task_id_in)
        if ps is None:
            return {"error": {"code": -32600, "message": "Estado pausado perdido"}}

        escolha = _parse_escolha(text)
        if escolha is None:
            # Unparseable — re-ask
            return {"result": {"task": task}}

        alternativas = ps["alternativas"]

        if escolha == "recusar":
            action = "decline"
        elif escolha in alternativas:
            action = "accept"
        else:
            # Invalid choice — keep paused
            return {"result": {"task": task}}

        # Build MCP retry call
        retry_params: dict = {
            "name": "reservar_sala",
            "arguments": ps["args"],
            "requestState": ps["request_state"],
            "inputResponses": {
                CHAVE_MCP: {"action": action}
                if action == "decline"
                else {"action": action, "content": {"sala": escolha}}
            },
        }
        del paused[task_id_in]
        resp = await _mcp(retry_params, traceparent)

        result = resp.get("result") or {}
        rpc_error = resp.get("error")

        task_id = task["id"]
        ctx_id = task["contextId"]

        if rpc_error or result.get("isError"):
            err_msg = rpc_error.get("message", "") if rpc_error else _text_of(result)
            am = _agent_msg(err_msg, task_id, ctx_id)
            task["status"] = {"state": "TASK_STATE_FAILED", "message": am}
            task["history"].append(am)
            return {"result": {"task": task}}

        structured = result.get("structuredContent") or {}
        if action == "decline" or not structured.get("reservado", True):
            am = _agent_msg("Reserva cancelada.", task_id, ctx_id)
            task["status"] = {"state": "TASK_STATE_CANCELED", "message": am}
            task["history"].append(am)
            return {"result": {"task": task}}

        res_id = structured.get("reserva", "")
        sala = structured.get("sala", "")
        am = _agent_msg(f"Reserva {res_id} confirmada na {sala}.", task_id, ctx_id)
        task["artifacts"] = [_artifact(structured)]
        task["status"] = {"state": "TASK_STATE_COMPLETED", "message": am}
        task["history"].append(am)
        return {"result": {"task": task}}

    # ---- New task ----
    task_id = _tid()
    ctx_id = _cid()
    task: dict = {
        "id": task_id,
        "contextId": ctx_id,
        "status": {"state": "TASK_STATE_WORKING"},
        "history": [],
        "artifacts": [],
    }
    tasks[task_id] = task
    task["history"].append(_user_msg(msg_id, text))

    # Parse command
    if not text.startswith("reservar") or (args := _parse_reservar(text)) is None:
        am = _agent_msg("Formato invalido. Use: reservar sala=X inicio=Y fim=Z responsavel=W", task_id, ctx_id)
        task["status"] = {"state": "TASK_STATE_FAILED", "message": am}
        task["history"].append(am)
        return {"result": {"task": task}}

    mcp_args = {"sala": args["sala"], "inicio": args["inicio"], "fim": args["fim"], "responsavel": args["responsavel"]}
    resp = await _mcp({"name": "reservar_sala", "arguments": mcp_args}, traceparent)

    result = resp.get("result") or {}
    rpc_error = resp.get("error")

    # Tool error
    if result.get("isError"):
        am = _agent_msg(_text_of(result), task_id, ctx_id)
        task["status"] = {"state": "TASK_STATE_FAILED", "message": am}
        task["history"].append(am)
        return {"result": {"task": task}}

    # RPC-level error
    if rpc_error:
        am = _agent_msg(rpc_error.get("message", "Erro MCP"), task_id, ctx_id)
        task["status"] = {"state": "TASK_STATE_FAILED", "message": am}
        task["history"].append(am)
        return {"result": {"task": task}}

    # input_required → pause
    if result.get("resultType") == "input_required":
        request_state = result.get("requestState")
        input_requests = result.get("inputRequests") or {}
        chave = next(iter(input_requests), CHAVE_MCP)
        alternativas = _alts_from_result(result)

        agent_text = "alternativas: " + ", ".join(alternativas)
        am = _agent_msg(agent_text, task_id, ctx_id)
        task["status"] = {"state": "TASK_STATE_INPUT_REQUIRED", "message": am}
        task["history"].append(am)

        paused[task_id] = {
            "request_state": request_state,
            "chave": chave,
            "alternativas": alternativas,
            "args": mcp_args,
        }
        return {"result": {"task": task}}

    # Successful direct reservation
    structured = result.get("structuredContent") or {}
    res_id = structured.get("reserva", "")
    sala = structured.get("sala", "")
    am = _agent_msg(f"Reserva {res_id} confirmada na {sala}.", task_id, ctx_id)
    task["artifacts"] = [_artifact(structured)]
    task["status"] = {"state": "TASK_STATE_COMPLETED", "message": am}
    task["history"].append(am)
    return {"result": {"task": task}}


# ---------------------------------------------------------------------------
# Agent Card
# ---------------------------------------------------------------------------
AGENT_CARD = {
    "name": "Central de Salas",
    "description": "Reserva salas de reuniao da Hill Valley Tech.",
    "provider": {"organization": "Hill Valley Tech", "url": "https://hillvalley.example"},
    "version": "1.0.0",
    "supportedInterfaces": [
        {"url": A2A_ENDPOINT, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
    ],
    "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
    "defaultInputModes": ["text/plain"],
    "defaultOutputModes": ["text/plain"],
    "skills": [
        {
            "id": "reservar-sala",
            "name": "Reservar sala",
            "description": "Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
            "tags": ["salas", "agenda"],
            "inputModes": ["text/plain"],
            "outputModes": ["text/plain"],
            "examples": [
                "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
            ],
        }
    ],
}


# ---------------------------------------------------------------------------
# Starlette routes
# ---------------------------------------------------------------------------
async def agent_card(request: Request) -> JSONResponse:
    return JSONResponse(AGENT_CARD)


async def a2a(request: Request) -> JSONResponse:
    traceparent = request.headers.get("traceparent")
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})

    rpc_id = body.get("id")
    method = body.get("method")
    params = body.get("params") or {}

    if method == "SendMessage":
        result = await _handle_send(params, traceparent)
    elif method == "GetTask":
        tid = params.get("id")
        t = tasks.get(tid)
        result = {"result": {"task": t}} if t else {"error": {"code": -32602, "message": f"Task nao encontrada: {tid}"}}
    else:
        result = {"error": {"code": -32601, "message": f"Metodo desconhecido: {method}"}}

    return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, **result})


app = Starlette(
    routes=[
        Route("/.well-known/agent-card.json", agent_card),
        Route("/a2a", a2a, methods=["POST"]),
    ]
)

if __name__ == "__main__":
    print(f"Agente A2A iniciando na porta {A2A_PORT}...", file=sys.stderr, flush=True)
    uvicorn.run(app, host="0.0.0.0", port=A2A_PORT, log_level="warning")
