FROM python:3.11-slim

ARG APP_USER=app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./requirements.txt

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        git \
        libffi-dev \
        libnss3 \
        libasound2 \
        libc6 \
        libglib2.0-0 \
        libssl-dev \
        libstdc++6 \
        libx11-6 \
        libxcursor1 \
        libxext6 \
        libxrender1 \
        libxtst6 \
        tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --shell /bin/bash "${APP_USER}"

COPY . /app
RUN chown -R "${APP_USER}:${APP_USER}" /app

USER ${APP_USER}

ENV STREAMLIT_BROWSER_GATHER_USAGE_STATS="false"

RUN mkdir -p ~/.streamlit && \
    cat <<'EOF' > ~/.streamlit/config.toml
[theme]
base = "dark"
primaryColor = "#30D158"
font = "sans serif"

[server]
headless = true
enableCORS = false
enableXsrfProtection = false
EOF

EXPOSE 8501

CMD ["streamlit", "run", "streamlit_app.py", "--server.port=8501", "--server.address=0.0.0.0"]
