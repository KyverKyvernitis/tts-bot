FROM python:3.11-slim

# Instala ffmpeg (necessário pro áudio/TTS no Discord)
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Instala dependências
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia o restante do projeto
COPY . .

# Inicia o bot
CMD ["python", "bot.py"]
