FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt

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
    && pip install --no-cache-dir -r requirements.txt

COPY . /app

EXPOSE 8501

ENV STREAMLIT_BROWSER_GATHER_USAGE_STATS="false"

RUN mkdir -p ~/.streamlit && \
    printf "[server]\nheadless = true\nenableCORS = false\nenableXsrfProtection = false\n" > ~/.streamlit/config.toml

CMD ["streamlit", "run", "streamlit_app.py", "--server.port=8501", "--server.address=0.0.0.0"]
