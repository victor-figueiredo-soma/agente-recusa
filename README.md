# Agente de Recusa — Atacado

Automação que monitora uma caixa de e-mail corporativa, identifica **notificações de não-entrega de transportadoras** (recusa, retenção fiscal ou extravio) usando IA e, para cada Nota Fiscal do Atacado afetada, abre um chamado — registrando em planilha e BigQuery e notificando a equipe de logística por e-mail, tudo sem intervenção manual.

---

## Objetivo

As transportadoras (Braspress, Movvi, Solução, Comboio, etc.) enviam diariamente comunicados informando que caixas de produtos — identificadas por Nota Fiscal — não puderam ser entregues a lojistas multimarca. Esses e-mails chegam em formatos variados (mensagens automáticas padronizadas ou texto livre redigido pelo operador) e precisavam ser lidos, classificados e registrados manualmente pela equipe de atacado.

Este agente automatiza esse fluxo de ponta a ponta:

- **Escuta** a caixa de entrada em tempo real (webhook do Microsoft Graph).
- **Classifica** cada e-mail com o Gemini, distinguindo recusa real de assuntos administrativos, entregas concluídas, volumes trocados, etc.
- **Extrai** a(s) Nota(s) Fiscal(is), transportadora, motivo e sub-motivo padronizado.
- **Valida** se a NF pertence ao Atacado (consulta ao BigQuery), descartando as de Varejo.
- **Registra** o chamado na planilha do Google Sheets e na tabela de chamados do BigQuery, com deduplicação por NF e por thread.
- **Notifica** a equipe de logística por e-mail (resposta na própria thread) resumindo os chamados criados.
- **Contabiliza** o custo de cada execução (Gemini + BigQuery + infraestrutura) para acompanhamento financeiro.

---

## Como funciona

```
┌──────────────┐   1. novo e-mail    ┌────────────────────┐
│  Caixa M365  │ ──────────────────► │  Microsoft Graph    │
│ (monitorada) │                     │  (change notification)
└──────────────┘                     └─────────┬──────────┘
                                                │ 2. POST /graph-webhook
                                                ▼
                                     ┌────────────────────────┐
                                     │   Agente (FastAPI)      │
                                     │                         │
   ┌─────────────────────────────────┤  a. filtros de entrada  │
   │                                 │  b. idempotência (thread)│
   │                                 │  c. análise Gemini       │
   │                                 │  d. valida NF (BigQuery) │
   │                                 └───────────┬─────────────┘
   │                                             │
   ▼ e. grava chamado                            ▼ f. notifica
┌────────────┐   ┌───────────────┐      ┌────────────────────┐
│  Sheets    │   │  BigQuery      │      │ E-mail p/ logística │
│ (chamados) │   │ (chamados +    │      │ (reply na thread)   │
│            │   │  custos)       │      └────────────────────┘
└────────────┘   └───────────────┘
```

1. Um e-mail chega na caixa monitorada.
2. O Microsoft Graph dispara uma *change notification* para `POST /graph-webhook`.
3. O agente responde `202` imediatamente e processa a mensagem em segundo plano:
   - **Filtros de entrada** — descarta e-mails do remetente ignorado e processa apenas os destinados ao endereço-alvo.
   - **Idempotência** — cada thread é analisada uma única vez; reenvios e continuações da mesma conversa são pulados.
   - **Análise com IA** — o Gemini classifica o e-mail (`is_recusa`, transportadora, NF, motivo, sub-motivo, status, confiança), considerando o histórico da thread quando disponível.
   - **Validação de NF** — cada NF é conferida no BigQuery; NFs que não são do Atacado são descartadas.
   - **Registro** — o chamado é gravado no Google Sheets e na tabela de chamados do BigQuery (deduplicação por NF).
   - **Notificação** — a equipe de logística recebe um e-mail-resumo respondendo à thread original.

A cada execução, os custos de Gemini, BigQuery e infraestrutura (Railway) são registrados na tabela de custos.

---

## Componentes

| Módulo | Responsabilidade |
| --- | --- |
| [main.py](main.py) | App FastAPI: webhook do Graph, ciclo de vida da *subscription* (criação, renovação e watchdog de auto-recuperação) e orquestração do processamento. |
| [agents/graph_client.py](agents/graph_client.py) | Integração com o Microsoft Graph: autenticação (MSAL), leitura de mensagens e threads, gestão de *subscriptions* e envio de respostas. |
| [agents/email_analyzer.py](agents/email_analyzer.py) | Análise do e-mail com o Gemini — limpeza do HTML, prompt especializado por transportadora e parsing do resultado. |
| [agents/sheet_writer.py](agents/sheet_writer.py) | Gravação dos chamados no Google Sheets, com verificação de reiteração (mesma/outra thread). |
| [agents/bq_client.py](agents/bq_client.py) | BigQuery: validação de NF do Atacado, gravação de chamados (dedup por NF), idempotência por thread e registro de custos. |
| [models/schemas.py](models/schemas.py) | Modelos Pydantic e as regras de negócio (sub-motivos padronizados, normalização de status). |
| [utils/](utils/) | Logger, cálculo de custos (`pricing.py`) e política de *retry* transitório (`retry.py`). |

---

## Estrutura do projeto

```
agente-recusa/
├── main.py                # entrypoint FastAPI + webhook + ciclo da subscription
├── agents/                # integrações externas (Graph, Gemini, Sheets, BigQuery)
├── models/                # schemas Pydantic e regras de negócio
├── utils/                 # logger, pricing, retry
├── tests/                 # testes
├── requirements.txt
├── Dockerfile
├── railway.toml           # config de deploy (Railway)
└── .env.example           # modelo das variáveis de ambiente
```

---

## Configuração

Toda a configuração é feita por **variáveis de ambiente**, definidas no painel do **Railway** (Service → Variables). O arquivo [.env.example](.env.example) serve como referência das variáveis necessárias e do que cada uma representa (credenciais do Gemini, Google Sheets, BigQuery e Microsoft Graph, além dos parâmetros de custo).

As variáveis que determinam **para onde a automação aponta** — as que você provavelmente vai ajustar a cada ambiente — são:

| Variável | O que define |
| --- | --- |
| `SPREADSHEET_ID` | ID da planilha do Google Sheets onde os chamados são registrados (parte da URL da planilha). |
| `MAILBOX_USER_ID` | E-mail/ID da caixa monitorada — é nela que a *subscription* fica de prontidão aguardando novos e-mails para disparar o webhook. |
| `WEBHOOK_BASE_URL` | URL pública do host do agente, usada para registrar a *subscription* no Graph. |
| `NOTIFICATION_EMAIL` | E-mail que recebe o aviso após a criação do chamado (resposta na mesma thread). |
| `MAILBOX_TARGET_EMAIL` | Só processa e-mails cujo campo "Para:" contenha este endereço. |
| `FILTER_IGNORE_FROM` | E-mails deste remetente são descartados sem avaliação. |

> As demais variáveis (chaves de API, credenciais de *service account* e parâmetros de precificação) seguem documentadas em [.env.example](.env.example).

---

## Execução

### Local

```bash
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8080
```

O agente sobe na porta `8080`. Para que o Graph consiga registrar a *subscription* e entregar as notificações, `WEBHOOK_BASE_URL` precisa apontar para uma URL pública que alcance a aplicação (em desenvolvimento, use um túnel como o ngrok).

### Docker

```bash
docker build -t agente-recusa .
docker run -p 8080:8080 --env-file .env agente-recusa
```

### Deploy (Railway)

O deploy usa o [Dockerfile](Dockerfile) e o [railway.toml](railway.toml), com *health check* em `/health`. Basta configurar as variáveis de ambiente no painel do Railway.

---

## Endpoints

| Método | Rota | Descrição |
| --- | --- | --- |
| `GET` | `/health` | Health check; retorna o status e o ID da *subscription* ativa. |
| `GET` / `POST` | `/graph-webhook` | Endpoint do webhook: responde ao `validationToken` do Graph e recebe as *change notifications*. |
| `POST` | `/subscriptions/renew` | Renovação manual da *subscription* (protegida por header `X-API-Key`). |

---

## Robustez e observabilidade

- **Subscription auto-gerenciada** — criada no *startup*, renovada a cada 24h e monitorada por um *watchdog* horário que a recria caso tenha expirado ou sumido, sobrevivendo a *restarts* sem intervenção.
- **Idempotência em dois níveis** — por thread (cada conversa é vista uma vez) e por NF (uma NF gera no máximo um chamado), evitando chamados e custos duplicados.
- **Retry transitório** — chamadas a Gemini, Graph, Sheets e BigQuery reexecutam automaticamente em falhas temporárias (429/5xx/timeout) com *backoff* exponencial.
- **Rastreio de custos** — cada execução registra o consumo de Gemini (tokens), BigQuery (bytes processados) e infraestrutura na tabela de custos, em BRL.
```