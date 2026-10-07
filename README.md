# Agente de Recusa — Atacado

Automação que monitora uma caixa de e-mail corporativa, identifica **notificações de não-entrega de transportadoras** (recusa, retenção fiscal ou extravio) usando IA e, para cada Nota Fiscal do Atacado afetada, abre o chamado de ponta a ponta: registra na planilha e no BigQuery, cria o Boletim de Devolução na WiseReturn e avisa a equipe de logística.

---

## O problema que resolve

As transportadoras (Braspress, Movvi, Solução, Comboio, entre outras) enviam diariamente comunicados informando que caixas de produtos — identificadas por Nota Fiscal — não puderam ser entregues a lojistas multimarca. Esses e-mails chegam em formatos variados: mensagens automáticas padronizadas ou texto livre redigido pelo operador.

Antes, alguém precisava ler cada e-mail, interpretar o motivo, conferir se a NF era do Atacado, registrar em planilha e abrir o Boletim de Devolução manualmente. O agente faz esse caminho inteiro sozinho.

---

## Fluxo

```
┌──────────────┐  novo e-mail   ┌─────────────────────┐
│  Caixa M365  │ ─────────────► │  Microsoft Graph    │
│ (monitorada) │                │ change notification │
└──────────────┘                └──────────┬──────────┘
                                           │ POST /graph-webhook
                                           ▼
                            ┌──────────────────────────────┐
                            │      Agente (FastAPI)        │
                            │  1. filtros de entrada       │
                            │  2. idempotência por thread  │
                            │  3. análise com Gemini       │
                            │  4. valida NF (BigQuery)     │
                            └──────────────┬───────────────┘
                                           │  por NF identificada
            ┌──────────────┬───────────────┼───────────────┬──────────────────┐
            ▼              ▼               ▼               ▼                  ▼
     ┌────────────┐ ┌─────────────┐ ┌────────────┐ ┌──────────────┐ ┌──────────────────┐
     │  Sheets    │ │  BigQuery   │ │ BigQuery   │ │  WiseReturn  │ │ E-mail p/         │
     │ (chamados) │ │ (chamados)  │ │ (série NF) │ │ (cria o BD)  │ │ logística (reply) │
     └────────────┘ └─────────────┘ └────────────┘ └──────────────┘ └──────────────────┘
```

1. **Recebimento** — o Graph dispara uma *change notification* para `POST /graph-webhook`. O agente responde `202` imediatamente e processa em segundo plano, para não estourar o timeout de ~3s do Graph.
2. **Filtros de entrada** — descarta e-mails do remetente ignorado e processa apenas os endereçados (To ou Cc) ao endereço-alvo configurado.
3. **Idempotência por thread** — cada conversa é analisada uma única vez; reenvios e continuações da mesma thread são pulados, evitando custo de IA e chamado duplicado.
4. **Análise com IA** — o Gemini classifica o e-mail devolvendo `is_recusa`, transportadora, Nota(s) Fiscal(is), motivo livre, sub-motivo padronizado, status e confiança, considerando o histórico da thread quando disponível.
5. **Validação da NF** — cada NF é conferida no faturamento do Atacado (BigQuery) pelo par número e série; NFs de outros emissores são descartadas.
6. **Registro** — o chamado é gravado no Google Sheets e na tabela de chamados do BigQuery, com deduplicação por NF.
7. **Boletim de Devolução** — a NF e sua série são enviadas à API da WiseReturn, que busca CNPJ, representante, transportadora e itens no ERP e cria o BD com status `PENDENTE ANALISTA`.
8. **Notificação** — a logística recebe um e-mail-resumo respondendo à thread original, com as NFs registradas e o desfecho de cada BD.

Cada execução registra os custos de Gemini (tokens), BigQuery (bytes processados) e infraestrutura na tabela de custos, em BRL.

### Ordem das etapas 6 a 8

A criação do BD é deliberadamente a **última** etapa. É o único efeito colateral visível fora do time — um analista passa a ter trabalho na fila. Fazê-la antes do registro interno arriscaria um BD sem rastro nosso caso a gravação falhasse. Nesta ordem, o pior caso é "registro interno existe, BD não", que é detectável no log e no e-mail, e corrigível com um único reenvio (a API deduplica por NF).

### Número e série da NF

Uma NF só é identificada pelo par **número e série**: a mesma numeração se repete em séries de emissores diferentes do grupo. Por isso o agente nunca valida uma NF só pelo número.

- **Quando o e-mail traz a série** — a Braspress informa `1528101/72` — o par exato precisa existir no faturamento do Atacado. Uma NF de outro emissor com o mesmo número é descartada.
- **Quando o e-mail não traz a série**, o número é aceito em qualquer série, desde que a nota tenha sido emitida há no máximo **180 dias**. Um número que só casa com uma nota mais antiga é tratado como colisão de numeração e descartado.

A série do documento validado é a que vai para a WiseReturn, que exige o campo. A validação acontece em `bq_client.validar_nf_atacado`, numa única consulta. Se o BigQuery estiver indisponível, o chamado é registrado normalmente, mas o BD não é criado — sem validação não há como garantir que é a nota certa —, e o e-mail de resumo informa o motivo.

---

## Componentes

| Módulo | Responsabilidade |
| --- | --- |
| [main.py](main.py) | App FastAPI: webhook do Graph, ciclo de vida da *subscription* (criação, renovação e watchdog) e orquestração do processamento. |
| [agents/graph_client.py](agents/graph_client.py) | Microsoft Graph: autenticação (MSAL), leitura de mensagens e threads, gestão de *subscriptions* e envio de respostas. |
| [agents/email_analyzer.py](agents/email_analyzer.py) | Análise com o Gemini — limpeza do HTML, prompt especializado por transportadora e parsing do resultado. |
| [agents/sheet_writer.py](agents/sheet_writer.py) | Gravação dos chamados no Google Sheets, com detecção de reiteração (mesma ou outra thread). |
| [agents/bq_client.py](agents/bq_client.py) | BigQuery: validação da NF por número e série, gravação de chamados, idempotência por thread e registro de custos. |
| [agents/wisereturn_client.py](agents/wisereturn_client.py) | API WiseReturn: criação do BD e classificação do desfecho (criado, já existente, erro de negócio, autenticação ou rede). |
| [models/schemas.py](models/schemas.py) | Modelos Pydantic e regras de negócio (sub-motivos padronizados, normalização de status). |
| [utils/](utils/) | Logger com alerta por e-mail, cálculo de custos (`pricing.py`) e política de *retry* (`retry.py`). |

---

## Estrutura

```
agente-recusa/
├── main.py                 # entrypoint FastAPI + webhook + ciclo da subscription
├── agents/                 # integrações externas (Graph, Gemini, Sheets, BigQuery, WiseReturn)
├── models/                 # schemas Pydantic e regras de negócio
├── utils/                  # logger, pricing, retry
├── tests/                  # testes (pytest)
├── requirements.txt        # dependências de runtime
├── requirements-dev.txt    # + pytest, para rodar os testes
├── Dockerfile
├── railway.toml            # config de deploy (healthcheck em /health)
└── .env.example            # modelo das variáveis de ambiente
```

---

## Configuração

Toda a configuração vem de **variáveis de ambiente**, definidas no painel do **Railway** (Service → Variables). O [.env.example](.env.example) documenta todas elas.

As que determinam para onde a automação aponta:

| Variável | O que define |
| --- | --- |
| `MAILBOX_USER_ID` | Caixa monitorada — é nela que a *subscription* aguarda novos e-mails. |
| `MAILBOX_TARGET_EMAIL` | Só processa e-mails cujo To/Cc contenha este endereço. |
| `FILTER_IGNORE_FROM` | E-mails deste remetente são descartados sem avaliação. |
| `WEBHOOK_BASE_URL` | URL pública **HTTPS** do agente, usada para registrar a *subscription*. Precisa incluir o esquema `https://`. |
| `NOTIFICATION_EMAIL` | Recebe o resumo dos chamados (reply na thread original). |
| `ALERT_EMAIL` | Recebe alerta automático a cada log de nível ERROR. |
| `SPREADSHEET_ID` | Planilha do Google Sheets onde os chamados são registrados. |
| `WISERETURN_API_KEY` | Chave do header `X-External-Key`. **Sem ela a criação de BD é desativada**, e o restante do fluxo segue normal. |

Credenciais (`GEMINI_API_KEY`, `GOOGLE_CREDENTIALS_JSON`, `BQ_CREDENTIALS_JSON`, `AZURE_*`), parâmetros de precificação e `WISERETURN_API_URL` estão documentados no [.env.example](.env.example).

---

## Execução

### Local

```bash
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8080
```

O agente sobe na porta `8080` (ou em `$PORT`, se definida). Para o Graph registrar a *subscription* e entregar notificações, `WEBHOOK_BASE_URL` precisa apontar para uma URL pública que alcance a aplicação — em desenvolvimento, use um túnel como o ngrok.

### Docker

```bash
docker build -t agente-recusa .
docker run -p 8080:8080 --env-file .env agente-recusa
```

### Deploy (Railway)

Usa o [Dockerfile](Dockerfile) e o [railway.toml](railway.toml), com *health check* em `/health`. O Railway injeta `PORT` automaticamente — não defina essa variável manualmente.

### Testes

```bash
pip install -r requirements-dev.txt
pytest -q
```

---

## Endpoints

| Método | Rota | Descrição |
| --- | --- | --- |
| `GET` | `/health` | Status, ID da *subscription* ativa e se a integração WiseReturn está ligada. |
| `GET` / `POST` | `/graph-webhook` | Responde ao `validationToken` do Graph e recebe as *change notifications*. |
| `POST` | `/subscriptions/renew` | Renovação manual da *subscription* (header `X-API-Key`). |

`/health` é o primeiro lugar a olhar quando algo parece errado:

```json
{"status": "ok", "subscription_id": "f6bdcab2-...", "wisereturn": "on"}
```

`subscription_id` nulo significa que o agente **não está recebendo e-mails**.

---

## Robustez e observabilidade

- **Subscription auto-gerenciada** — criada no *startup*, renovada a cada 24h e monitorada por um *watchdog* horário que a recria caso tenha expirado ou sumido. Sobrevive a restarts sem intervenção.
- **Idempotência em dois níveis** — por thread (cada conversa é vista uma vez) e por NF (uma NF gera no máximo um chamado), evitando chamados e custos duplicados. A criação do BD também é idempotente: a WiseReturn deduplica por NF e devolve o número do BD já existente.
- **Retry transitório** — chamadas a Gemini, Graph, Sheets, BigQuery e WiseReturn reexecutam automaticamente em falhas temporárias (429, 5xx, timeout) com *backoff* exponencial. Erros determinísticos de negócio não são retentados.
- **Falhas isoladas** — um problema na criação do BD nunca interrompe o processamento da NF: o chamado já está gravado, e o desfecho aparece no log e no e-mail de resumo.
- **Alerta por e-mail** — qualquer log de nível ERROR dispara e-mail para `ALERT_EMAIL`, com *throttle* para evitar repetição. Falhas que afetam apenas uma NF ficam em WARNING e são reportadas de forma agregada no resumo à logística.
- **Rastreio de custos** — cada execução registra o consumo de Gemini (tokens), BigQuery (bytes) e infraestrutura na tabela de custos, em BRL.
- **Instâncias isoladas** — a limpeza de *subscriptions* obsoletas filtra por `notificationUrl`, então cada instância só administra as próprias. Duas instâncias sob o mesmo App Registration do Azure (por exemplo produção e staging, ou agentes para caixas diferentes) coexistem sem se interferir.
