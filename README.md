# A Ponte: um agente A2A com MCP por dentro

*Onde termina a profundidade e começa o alcance*

**Projeto:** MBA Engenharia de Software com IA, curso de MCP e A2A

## Como rodar

### Pré-requisitos

Python 3.10+ com os pacotes instalados:

```bash
pip install "mcp==2.2.0" "starlette==1.6.0" uvicorn httpx
```

### Gerar o segredo

```bash
export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
```

> Nunca fixe o valor no código. A variável precisa ter pelo menos 32 bytes de aleatoriedade.

### Terminal 1 — Servidor MCP (porta 7301)

```bash
REQUEST_STATE_SECRET=$REQUEST_STATE_SECRET python servidor-mcp/server.py
```

### Terminal 2 — Agente A2A (porta 7300)

```bash
REQUEST_STATE_SECRET=$REQUEST_STATE_SECRET python agente/agent.py
```

### Validador

```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

---

## Onde a ponte acontece

**Entrada do `input_required` → pausa da Task**
(`agente/agent.py`, função `_handle_send`, caminho de nova Task):

```python
if result.get("resultType") == "input_required":
    request_state = result.get("requestState")
    ...
    task["status"] = {"state": "TASK_STATE_INPUT_REQUIRED", "message": am}
    paused[task_id] = {"request_state": request_state, ...}
    return {"result": {"task": task}}
```

Quando o servidor MCP responde `resultType=input_required`, o agente não tenta responder a elicitation por conta própria: ele guarda o `requestState` cifrado na memória associado ao `task_id` e devolve a Task em `TASK_STATE_INPUT_REQUIRED` com a linha `alternativas: ...` para o cliente A2A.

**Retomada com `requestState` → retry MCP**
(`agente/agent.py`, função `_handle_send`, caminho de continuação):

```python
retry_params = {
    "name": "reservar_sala",
    "arguments": ps["args"],
    "requestState": ps["request_state"],   # ecoado sem modificação
    "inputResponses": {CHAVE_MCP: {"action": action, ...}},
}
resp = await _mcp(retry_params, traceparent)
```

Quando o `SendMessage` de continuação chega com `taskId`, o agente recupera o `requestState` guardado e o reenvia ao servidor MCP em um novo `tools/call` com id JSON-RPC diferente. O servidor decripta o estado, valida a integridade via AES-256-GCM e conclui a reserva.

---

## Decisões técnicas

### Proteção do `requestState`

Usa o `RequestStateSecurity` do SDK Python com AES-256-GCM (chave derivada de `REQUEST_STATE_SECRET` via HKDF). O SDK sela o estado na saída do servidor e o dessela na entrada do retry, antes que o handler da tool seja chamado. O agente nunca abre nem interpreta o `requestState`: apenas guarda e ecoa.

- **TTL:** 1800 s (30 minutos). Configurável via SDK.
- **Rotação:** `RequestStateSecurity` aceita lista de chaves; a primeira é usada para selar, todas são tentadas para desselas (zero-downtime rotation).

### Estado das Tasks

`tasks: dict[str, dict]` e `paused: dict[str, dict]` — dicionários em memória, sem persistência. Cada Task tem `id`, `contextId`, `status`, `history` e `artifacts`. O estado pausado é indexado por `task_id` e contém apenas o `requestState` opaco, a chave de elicitation, as alternativas validadas e os argumentos originais selados — nunca é exposto em nenhuma resposta A2A (verificação 34 do validador).

### Propagação de `traceparent`

O agente lê o header `traceparent` do request A2A e o injeta no campo `_meta` de todos os `tools/call` emitidos para aquela Task. O trace-id é preservado; o span-id pode ser novo.

### Sem LLM, sem sessão

O parsing é por regex (`key=value`). A escolha de sala é validada contra a lista de alternativas armazenada no `paused` state. Toda informação necessária para retomar uma Task está no `requestState` cifrado que o servidor emitiu — o agente não guarda nada além do blob opaco.

---

## Saída do validador

```
trace-id desta execucao: 11c18a1cba134a76456a9413d94f1c7a
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```
