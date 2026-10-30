FROM python:3.12-slim

WORKDIR /app

# Install git (needed to pip-install pyquotex from GitHub)
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

# Install Python dependencies (pyquotex git URL is already inside requirements.txt)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY . .

# Dashboard port
EXPOSE 5000

# Environment variables — set these in Railway:
#   QUOTEX_EMAIL       your Quotex account email
#   QUOTEX_PASSWORD    your Quotex account password
#   DATA_DIR           directory for config.json on a PERSISTENT volume, e.g.
#                      /data. Without it settings live in /app, which is
#                      rebuilt on every deploy — so anything saved in the
#                      Settings screen is lost on the next redeploy.
#                      (QUOTEX_CONFIG_PATH sets the full file path instead.)
# Telegram credentials are stored in quotex_bot_session.json (persist via volume)

CMD ["python", "main.py"]
