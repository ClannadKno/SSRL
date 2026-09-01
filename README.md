# SSRL-ESP

SSRL-ESP (Socially Shared Regulation of Learning Experimental Study Platform) is a research platform for collaborative learning experiments. It includes a Flask backend, a collaborative editor, teacher and student interfaces, questionnaires, intervention workflows, and browser load tests.

This public snapshot has been sanitized. Runtime databases, participant data, login keys, uploads, logs, credentials, and machine-specific files are not included.

## Install

Install Python 3.11 or newer, [uv](https://docs.astral.sh/uv/), Node.js 20 or newer, and npm. Then run the following commands from the repository root:

```bash
uv sync --frozen

cd frontend/collaborative-editor
npm ci
npm run build
cd ../..
```

## Run

Start Flask, the collaboration WebSocket service, and the Huey worker:

```bash
SERA_LLM_ENABLED=0 ./start_collab.sh
```

To build the frontend during startup, use `./start_collab.sh --build`. To start only Flask through Waitress, use `./start_flask_waitress.sh`.

The startup scripts use the environment created by `uv sync`. Local databases, uploads, queues, and generated secret files are excluded by `.gitignore`.

## Test

```bash
uv run --frozen --no-sync pytest
```

Load tests are documented separately in `load-test/README.md`.

Before publishing a fork, add an open-source license, confirm that all included research materials and assets may be redistributed, and run a final secret scan.
