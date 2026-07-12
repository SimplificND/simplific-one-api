# Simplific ONE API

Motor oficial de WhatsApp para campanhas 1:1, templates, inbox e automações.

## Stack

- Frontend: React + Vite
- Backend: FastAPI
- Deploy: Docker Compose / Coolify

## Variáveis

No Coolify, configure estas variáveis no serviço:

```bash
APP_NAME="Simplific ONE API"
PUBLIC_BASE_URL="https://api.negociodeproposito.com.br"
META_GRAPH_VERSION="v23.0"
META_APP_ID=""
META_APP_SECRET=""
META_WABA_ID=""
META_PHONE_NUMBER_ID=""
META_ACCESS_TOKEN=""
META_VERIFY_TOKEN="simplific-one-api-webhook"
STORAGE_DIR="/app/storage"
```

Webhook Meta:

```text
https://api.negociodeproposito.com.br/api/meta/webhook
```

Token de verificação:

```text
simplific-one-api-webhook
```

## Primeira versão funcional

- Dashboard operacional.
- Tela de conexão com App ID, App Secret, WABA ID, Phone Number ID e Access Token.
- Cadastro de contatos com tags e listas.
- Importação de leads por CSV com criação de lista, tags padrão e campos personalizados.
- Upload de mídia local com URL pública para uso em fluxos.
- Sincronização de modelos/templates da Meta pelo WABA ID.
- Inbox alimentada pelo webhook oficial.
- Tela de envios com nome, lista, template, fluxo de resposta, listas de exclusão, envio imediato e agendamento.
- Construção de fluxos com mensagem, áudio, imagem, vídeo, arquivo, tags, listas e atraso inteligente.
- Automação por mensagem recebida, botão clicado, texto exato, texto contém ou qualquer mensagem.
- Sequência com delay por item.
- Envio real pela Cloud API quando `META_ACCESS_TOKEN` e `META_PHONE_NUMBER_ID` estiverem configurados.
- Modo mock quando as credenciais Meta ainda não estiverem configuradas.

## Desenvolvimento

```bash
docker compose up --build
```

Frontend: http://localhost:3000
Backend: http://localhost:8000/api/health

## Testes

```bash
cd backend
pip install -r requirements-dev.txt
pytest -v
```

Os testes usam dados sintéticos (não tocam produção) e, quando necessário,
sobem o próprio processo `uvicorn` contra um `STORAGE_DIR` temporário para
simular cenários reais de concorrência e reinício.
