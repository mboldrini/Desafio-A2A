#!/usr/bin/env python3
"""Servidor MCP - Central de Salas Hill Valley Tech.

Expoe 3 tools (listar_salas, consultar_disponibilidade, reservar_sala)
e 1 resource (politica://uso) via Streamable HTTP na porta 7301.

Uso:
    REQUEST_STATE_SECRET=<64-char-hex> python servidor-mcp/server.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import uvicorn
from mcp.server import ServerRequestContext
from mcp.server.mcpserver import MCPServer, Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.request_state import CallNext, HandlerResult, RequestStateSecurity
from mcp.shared.exceptions import MCPError
from mcp.types import (
    MISSING_REQUIRED_CLIENT_CAPABILITY,
    ElicitRequest,
    ElicitRequestFormParams,
    InputRequiredResult,
)
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Dados
# ---------------------------------------------------------------------------
DADOS_DIR = Path(__file__).parent.parent / "dados"
SALAS: list[dict] = json.loads((DADOS_DIR / "salas.json").read_text(encoding="utf-8"))
POLITICA_TEXT: str = (DADOS_DIR / "politica-de-uso.md").read_text(encoding="utf-8")
POLITICA_VERSION: str = POLITICA_TEXT.split("\n")[0].replace("versao:", "").strip()

# Reservas em memoria (inicializadas com o arquivo)
_reservas: list[dict] = json.loads((DADOS_DIR / "reservas.json").read_text(encoding="utf-8"))
_reserva_counter: list[int] = [len(_reservas)]

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
MCP_PORT = int(os.environ.get("MCP_PORT", "7301"))
SECRET = os.environ.get("REQUEST_STATE_SECRET", "")
if not SECRET:
    print("ERRO: REQUEST_STATE_SECRET nao definido.", file=sys.stderr)
    sys.exit(1)
if len(bytes.fromhex(SECRET) if all(c in "0123456789abcdefABCDEF" for c in SECRET) else b"x" * len(SECRET)) < 32:
    pass  # SDK valida internamente

SP_TZ = timezone(timedelta(hours=-3))
CHAVE = "__main__:escolha_de_sala"


# ---------------------------------------------------------------------------
# Modelos Pydantic
# ---------------------------------------------------------------------------
class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflitoOut]


class ReservaOut(BaseModel):
    reserva: Optional[str] = None
    reservado: bool = True
    sala: Optional[str] = None
    inicio: Optional[str] = None
    fim: Optional[str] = None
    responsavel: Optional[str] = None
    politica: Optional[str] = None
    motivo: Optional[str] = None


# ---------------------------------------------------------------------------
# Logica de negocio
# ---------------------------------------------------------------------------
def _parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _get_sala(sala_id: str) -> dict:
    sala = next((s for s in SALAS if s["id"] == sala_id), None)
    if sala is None:
        raise ToolError(f"Sala inexistente: {sala_id}")
    return sala


def _validate_policy(inicio: str, fim: str) -> None:
    from datetime import time as dtime

    dt_inicio = _parse_dt(inicio)
    dt_fim = _parse_dt(fim)

    if dt_fim <= dt_inicio:
        raise ToolError("Intervalo invalido: fim deve ser posterior a inicio")

    inicio_sp = dt_inicio.astimezone(SP_TZ)
    fim_sp = dt_fim.astimezone(SP_TZ)

    WINDOW_START = dtime(8, 0)
    WINDOW_END = dtime(20, 0)

    if not (WINDOW_START <= inicio_sp.time() <= WINDOW_END and WINDOW_START <= fim_sp.time() <= WINDOW_END):
        raise ToolError("Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00")

    if dt_fim - dt_inicio > timedelta(hours=2):
        raise ToolError("Duracao acima do limite: a politica permite no maximo 2 horas")


def _get_conflicts(sala_id: str, inicio: str, fim: str) -> list[dict]:
    dt_inicio = _parse_dt(inicio)
    dt_fim = _parse_dt(fim)
    result = []
    for r in _reservas:
        if r["sala"] != sala_id:
            continue
        r_inicio = _parse_dt(r["inicio"])
        r_fim = _parse_dt(r["fim"])
        # Overlap: intervals intercept when NOT (r ends before start OR r starts after end)
        if not (r_fim <= dt_inicio or r_inicio >= dt_fim):
            result.append(r)
    return result


def _find_alternatives(sala_pedida: dict, inicio: str, fim: str) -> list[str]:
    cap_min = sala_pedida["capacidade"]
    alternatives = []
    for s in SALAS:
        if s["id"] == sala_pedida["id"]:
            continue
        if s["capacidade"] < cap_min:
            continue
        if not _get_conflicts(s["id"], inicio, fim):
            alternatives.append(s)
    # Ordenar: capacidade ASC, id ASC em empate
    alternatives.sort(key=lambda s: (s["capacidade"], s["id"]))
    return [s["id"] for s in alternatives[:3]]


def _criar_reserva(sala: str, inicio: str, fim: str, responsavel: str) -> ReservaOut:
    _reserva_counter[0] += 1
    res_id = f"res-{_reserva_counter[0]:04d}"
    _reservas.append({"id": res_id, "sala": sala, "inicio": inicio, "fim": fim, "responsavel": responsavel})
    return ReservaOut(
        reserva=res_id,
        reservado=True,
        sala=sala,
        inicio=inicio,
        fim=fim,
        responsavel=responsavel,
        politica=POLITICA_VERSION,
        motivo=None,
    )


# ---------------------------------------------------------------------------
# Middleware de logging
# ---------------------------------------------------------------------------
async def _log_middleware(ctx: ServerRequestContext, call_next: CallNext) -> HandlerResult:
    tp = (ctx.meta or {}).get("traceparent", "-")
    print(f"MCP method={ctx.method} id={ctx.request_id} traceparent={tp}", file=sys.stderr, flush=True)
    return await call_next(ctx)


# ---------------------------------------------------------------------------
# MCPServer
# ---------------------------------------------------------------------------
mcp = MCPServer(
    "central-de-salas",
    version="1.0.0",
    request_state_security=RequestStateSecurity(
        keys=[SECRET],
        ttl=1800.0,
        bind_principal=None,
    ),
    middleware=[_log_middleware],
)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
@mcp.tool(description="Lista todas as salas com capacidade e recursos.")
def listar_salas() -> ListaDeSalas:
    return ListaDeSalas(salas=[SalaOut(**s) for s in SALAS])


@mcp.tool(description="Diz se uma sala esta livre no intervalo, e quais reservas conflitam.")
def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
    sala_obj = _get_sala(sala)
    _validate_policy(inicio, fim)
    conflicts = _get_conflicts(sala_obj["id"], inicio, fim)
    return Disponibilidade(
        sala=sala,
        livre=len(conflicts) == 0,
        conflitos=[
            ConflitoOut(id=c["id"], inicio=c["inicio"], fim=c["fim"], responsavel=c["responsavel"])
            for c in conflicts
        ],
    )


@mcp.tool(description="Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.")
async def reservar_sala(sala: str, inicio: str, fim: str, responsavel: str, ctx: Context) -> ReservaOut:
    # ------------------------------------------------------------------
    # Caminho de retry: inputResponses presente
    # ------------------------------------------------------------------
    if ctx.input_responses is not None:
        # Pegar estado selado (SDK ja decriptou)
        state_raw = ctx.request_state or "{}"
        try:
            state = json.loads(state_raw)
        except json.JSONDecodeError:
            raise MCPError(code=-32602, message="requestState invalido")

        # Usar valores selados (ignorar argumentos do retry, que podem ter sido adulterados)
        sala_sealed = state.get("sala", sala)
        inicio_sealed = state.get("inicio", inicio)
        fim_sealed = state.get("fim", fim)
        responsavel_sealed = state.get("responsavel", responsavel)
        alternativas = state.get("alternativas", [])
        chave = state.get("chave", CHAVE)

        response = ctx.input_responses.get(chave)
        if response is None:
            raise ToolError("Resposta para elicitation nao encontrada")

        if response.action in ("decline", "cancel"):
            return ReservaOut(reservado=False, motivo="recusado")

        # action == "accept"
        sala_escolhida = (response.content or {}).get("sala", "")
        if sala_escolhida not in alternativas:
            raise ToolError(f"Sala escolhida invalida: {sala_escolhida}")

        return _criar_reserva(sala_escolhida, inicio_sealed, fim_sealed, responsavel_sealed)

    # ------------------------------------------------------------------
    # Caminho inicial
    # ------------------------------------------------------------------
    sala_obj = _get_sala(sala)
    _validate_policy(inicio, fim)

    conflicts = _get_conflicts(sala_obj["id"], inicio, fim)

    if not conflicts:
        # Sala livre: reservar diretamente
        return _criar_reserva(sala, inicio, fim, responsavel)

    # Sala ocupada: verificar capability de elicitation
    caps = ctx.client_capabilities
    has_form = (
        caps is not None
        and caps.elicitation is not None
        and caps.elicitation.form is not None
    )
    if not has_form:
        raise MCPError(
            code=MISSING_REQUIRED_CLIENT_CAPABILITY,
            message=f"Client did not declare the form elicitation capability required by resolver '{CHAVE}'",
            data={"requiredCapabilities": {"elicitation": {"form": {}}}},
        )

    # Calcular alternativas
    alternativas = _find_alternatives(sala_obj, inicio, fim)
    if not alternativas:
        raise ToolError("Sem alternativas disponiveis no intervalo")

    # Montar schema da elicitation
    if len(alternativas) == 1:
        sala_schema = {"type": "string", "const": alternativas[0]}
    else:
        sala_schema = {"type": "string", "enum": alternativas}

    schema: dict = {
        "type": "object",
        "properties": {
            "sala": {
                **sala_schema,
                "title": "Sala",
                "description": "Sala alternativa escolhida",
            }
        },
        "required": ["sala"],
    }

    # Estado selado (plaintext - SDK vai criptografar)
    state_data = json.dumps(
        {
            "sala": sala,
            "inicio": inicio,
            "fim": fim,
            "responsavel": responsavel,
            "alternativas": alternativas,
            "chave": CHAVE,
        }
    )

    return InputRequiredResult(  # type: ignore[return-value]
        input_requests={
            CHAVE: ElicitRequest(
                params=ElicitRequestFormParams(
                    mode="form",
                    message="A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa.",
                    requested_schema=schema,
                )
            )
        },
        request_state=state_data,
    )


# ---------------------------------------------------------------------------
# Resource
# ---------------------------------------------------------------------------
@mcp.resource("politica://uso", mime_type="text/markdown", description="Politica de uso das salas.")
def politica_uso() -> str:
    return POLITICA_TEXT


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print(f"Servidor MCP iniciando na porta {MCP_PORT}...", file=sys.stderr, flush=True)
    app = mcp.streamable_http_app(streamable_http_path="/mcp", json_response=True)
    uvicorn.run(app, host="0.0.0.0", port=MCP_PORT, log_level="warning")
