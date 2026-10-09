<p align="center">
  <a href="https://github.com/ThinkerWen/temp-mail">
    <img src="docs/assets/logo.svg" width="128" height="128" alt="Temp Mail logo" />
  </a>
</p>

<h1 align="center">Temp Mail</h1>

<p align="center"><a href="README.md">简体中文</a> · <strong>English</strong></p>

<p align="center"><strong>Add a little privacy to your inbox.</strong></p>

<p align="center">A self-hosted temporary email service that brings multiple providers together through a web interface and a unified API.</p>

<p align="center">
  <a href="https://github.com/ThinkerWen/temp-mail/actions/workflows/workflow.yml"><img src="https://github.com/ThinkerWen/temp-mail/actions/workflows/workflow.yml/badge.svg" alt="Docker image publishing" /></a>
  <a href="https://hub.docker.com/r/designerwang/temp-mail"><img src="https://img.shields.io/docker/pulls/designerwang/temp-mail?logo=docker&label=Docker%20Hub&color=2496ED" alt="Docker Hub pulls" /></a>
  <a href="https://github.com/ThinkerWen/temp-mail/tags"><img src="https://img.shields.io/github/v/tag/ThinkerWen/temp-mail?label=version&color=f97316" alt="Version tag" /></a>
</p>

---

## Features

- **Unified inbox**: Create temporary mailboxes, copy addresses, and view messages and operation history.
- **Dashboard**: Track mailbox statistics, incoming email trends, and recent activity.
- **Online configuration**: Manage providers and system settings with hot reload support.
- **Clean interface**: Dark mode, Chinese and English, and a responsive layout.
- **Preserved history**: Expired mailboxes stop sending and receiving; existing messages remain available until manually cleared.

## Supported Providers

| Provider | Website | Capabilities | Integration Guide |
| --- | --- | --- | --- |
| [temp-mail.org](https://temp-mail.org/) | [temp-mail.org](https://temp-mail.org/) | Create mailboxes, receive emails | [Integration guide](docs/providers/temp-mail-org.md) |
| [TempMail.lol](https://tempmail.lol/) | [tempmail.lol](https://tempmail.lol/) | Create mailboxes, receive emails | [Integration guide](docs/providers/tempmail-lol.md) |

Sending emails and downloading attachments are not currently supported.

## Docker Deployment

For a fresh deployment, run from the project root:

```bash
mkdir -p temp-mail
cd temp-mail
curl -fsSL https://raw.githubusercontent.com/ThinkerWen/temp-mail/main/compose.yaml -o compose.yaml
mkdir -p config data
docker pull designerwang/temp-mail:latest
docker compose run --rm --user "$(id -u):$(id -g)" init
docker compose up -d
```

Open `http://127.0.0.1:8000/` and sign in with the `app.api_token` from `config/config.yaml`.

## Running Locally

Requires Python 3.12+, uv, Node.js 22.13+, and pnpm 11.

```bash
uv sync --dev
uv run python scripts/init_config.py
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend build
uv run python run.py
```

## Documentation

The linked guides are currently in Chinese.

| Guide | Contents |
| --- | --- |
| [Deployment](docs/deployment.md) | Docker, persistent storage, logs, and backups |
| [Configuration](docs/configuration.md) | YAML settings, provider configuration, and hot reload |
| [Usage and Frontend Development](docs/frontend.md) | Page features, local development, and builds |
| [API Usage](docs/api.md) | Authentication, mailbox creation, receiving emails, and endpoint reference |
| [Architecture](docs/design.md) | Data models, task scheduling, and adding providers |

See [config.example.yaml](config.example.yaml) for the configuration template and [test_main.http](test_main.http) for request examples. Once the service is running, visit `/docs` for interactive API documentation.

For Windows, remote access, migrating existing configurations, and backups, see the [deployment guide](docs/deployment.md). If you have an existing database, skip initialization and keep your original configuration and encryption key.

## Development Checks

```bash
uv run pytest
pnpm --dir frontend build
pnpm --dir frontend exec playwright install chromium
pnpm --dir frontend test
```

The backend uses FastAPI, SQLite, and Loguru; the frontend uses React, TypeScript, and HeroUI. Tests use local mock providers and do not create real mailboxes.
