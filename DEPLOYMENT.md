# Deployment Guide: Docker & Docker Compose

This guide explains how to deploy the Excel Automation System using Docker and Docker Compose.

---

## 1. Quick Start with Docker Compose

### Prerequisites
- Docker Engine installed
- Docker Compose installed (usually included with Docker Desktop on macOS/Windows, or `docker-compose-plugin` on Linux)

### Steps

1. **Configure Environment Variables (Recommended)**
   Copy the example environment file and create a `.env` file:
   ```bash
   cp .env.example .env
   ```
   Generate a secure secret key for `EXCEL_AUTOMATION_SECRET`:
   ```bash
   python3 -c 'import secrets; print(secrets.token_hex(32))'
   ```
   Paste the generated string into your `.env` file.

   To turn on the website assistant (the **Ask a question** panel), also set `ANTHROPIC_API_KEY` in `.env`. Without it the panel stays hidden. `ASSISTANT_MODEL` and `ASSISTANT_EFFORT` are optional; see the Website assistant section of the README.

2. **Build and Start the Container**
   ```bash
   docker compose up -d --build
   ```

3. **Verify the Container is Running & Healthy**
   ```bash
   docker compose ps
   ```
   You should see `excel_automation_app` with status `Up (healthy)`.

4. **Create the Sign-in Accounts**
   Nobody can use the dashboard until an account exists. Create one per person (you will be asked for a password of at least 8 characters):
   ```bash
   docker compose exec excel-automation flask --app app users add alice --name "Alice Rao"
   ```
   Manage accounts later with `users list`, `users password <username>` and `users remove <username>` in the same way. Accounts are kept in `./config/users.db` on the host, so they survive rebuilds.

5. **Access the Web Dashboard**
   Open your browser and navigate to:
   ```
   http://localhost:5050
   ```
   *(Or `http://<your-server-ip>:5050` if deployed on a remote server/VM)* and sign in.

---

## 2. Persistent Storage & Host Directory Mapping

All important data is persisted on your host machine via volume mounts:

| Host Directory | Container Path | Purpose |
| :--- | :--- | :--- |
| `./config/` | `/app/config` | Configuration files (`config.json`, `review_config.json`, `review_schema.json`), sign-in accounts (`users.db`), and the website assistant's rules and knowledge base (`assistant/`) |
| `./input/` | `/app/input` | Drop folder for monthly Excel files and `input/invoices/` for PDFs |
| `./processed/` | `/app/processed` | Successfully processed files |
| `./errors/` | `/app/errors` | Files that failed validation |
| `./backups/` | `/app/backups` | Automatic backups of `Master.xlsx` before each update |
| `./reports/` | `/app/reports` | CSV error reports and JSON processing summaries |
| `./logs/` | `/app/logs` | Application logs |
| `./output/` | `/app/output` | Invoice review SQLite store and extracted documents |
| `./data/` | `/app/data` | Continuous synchronization of `Master.xlsx` |

> **Note on `Master.xlsx`**: You can upload `Master.xlsx` directly from the web dashboard or place it into `./data/Master.xlsx` on the host. The application automatically syncs any updates to `./data/Master.xlsx` and creates timestamped backups in `./backups/`.

---

## 3. Container Management Commands

- **View Live Logs:**
  ```bash
  docker compose logs -f
  ```

- **Restart the Service:**
  ```bash
  docker compose restart
  ```

- **Stop the Service:**
  ```bash
  docker compose down
  ```

- **Rebuild After Code Changes:**
  ```bash
  docker compose up -d --build
  ```

- **Check Health Status Directly:**
  ```bash
  curl -f http://localhost:5050/healthz
  ```

---

## 4. Running with Standalone Docker (Without Compose)

If you prefer building and running with pure Docker CLI:

```bash
# Build the image
docker build -t excel-automation:latest .

# Run the container with persistent volumes
docker run -d \
  --name excel_automation \
  --restart unless-stopped \
  -p 5050:5050 \
  -e EXCEL_AUTOMATION_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')" \
  -v "$(pwd)/config:/app/config" \
  -v "$(pwd)/input:/app/input" \
  -v "$(pwd)/processed:/app/processed" \
  -v "$(pwd)/errors:/app/errors" \
  -v "$(pwd)/backups:/app/backups" \
  -v "$(pwd)/reports:/app/reports" \
  -v "$(pwd)/logs:/app/logs" \
  -v "$(pwd)/output:/app/output" \
  -v "$(pwd)/data:/app/data" \
  excel-automation:latest
```

---

## 5. Production Considerations

1. **Gunicorn Thread Model:**
   The application uses 1 worker with multiple threads (`GUNICORN_WORKERS=1`, `GUNICORN_THREADS=4`). This is intentional to ensure the internal in-process locking mechanism (`_workbook_lock`) serializes write operations to `Master.xlsx` while handling multiple concurrent read requests.
2. **Reverse Proxy (Nginx / Cloudflare):**
   When exposing this application to the public internet or corporate network:
   - Put Nginx or Caddy in front to provide SSL/TLS (`https://`).
   - Configure Nginx client body size: `client_max_body_size 50M;` (matching the Flask 50MB limit).

