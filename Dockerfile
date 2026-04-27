# ============================================================
# Quilgo Automation Suite — Docker Image
# Python 3.12 + Node.js 20 + Playwright Chromium + Streamlit
# ============================================================

FROM --platform=linux/amd64 python:3.12-slim

# --- System packages ---
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# --- Install Node.js 20 (LTS) ---
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# --- Working directory ---
WORKDIR /app

# --- Python dependencies first (layer cache) ---
COPY requirements.txt .
RUN pip install --upgrade pip && pip install --default-timeout=1000 --retries 5 -r requirements.txt

# --- Node.js dependencies ---
COPY package.json package-lock.json* ./
RUN npm install

# --- Playwright: install Chromium + its OS dependencies inside the container ---
COPY playwright.config.js ./
RUN npx playwright install chromium --with-deps

# --- Copy the rest of the project ---
COPY . .

# --- Create runtime directories so volume mounts work cleanly ---
RUN mkdir -p Quilgo downloads

# --- Streamlit runs on port 8501 ---
EXPOSE 8501

# --- Streamlit config: disable the "welcome" prompt and file watcher ---
ENV STREAMLIT_SERVER_HEADLESS=true
ENV STREAMLIT_SERVER_PORT=8501
ENV STREAMLIT_SERVER_ENABLE_CORS=false
ENV STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

CMD ["streamlit", "run", "streamlit_app.py", "--server.address=0.0.0.0"]