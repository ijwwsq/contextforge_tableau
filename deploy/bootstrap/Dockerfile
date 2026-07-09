FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN pip install --upgrade pip \
 && pip install "httpx>=0.27" "pyjwt>=2.8"

COPY register.py mint_token.py ./

CMD ["python", "register.py"]
