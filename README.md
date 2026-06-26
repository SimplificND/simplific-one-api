# Simplific ONE API

Motor oficial de WhatsApp para campanhas 1:1, templates, inbox e automações.

## Stack

- Frontend: React + Vite
- Backend: FastAPI
- Deploy: Docker Compose / Coolify

## Variáveis

Copie `.env.example` para `.env` no ambiente de deploy e preencha as credenciais da Meta.

```bash
cp .env.example .env
```

## Desenvolvimento

```bash
docker compose up --build
```

Frontend: http://localhost:3000
Backend: http://localhost:8000/api/health

