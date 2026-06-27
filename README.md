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
- Cadastro de contatos com tags e listas.
- Inbox alimentada pelo webhook oficial.
- Envio manual de sequência: texto, template, imagem, vídeo, áudio ou documento.
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
