# Imagem base enxuta com Python 3.11
FROM python:3.11-slim

# Instalar dependências de build (precisas para pacotes nativos)
RUN apt-get update && apt-get install -y \
    build-essential \
    gcc \
    curl \
 && rm -rf /var/lib/apt/lists/*

# Diretório de trabalho
WORKDIR /app

# Criar um usuário não-root para segurança
RUN adduser --disabled-password --gecos '' appuser

# Copiar requirements e instalar pacotes Python
COPY requirements.txt ./
RUN pip install --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# Copiar código da aplicação
COPY app/ ./app/

# Ajustar permissões para o usuário appuser
RUN chown -R appuser:appuser /app

# Trocar para usuário não-root
USER appuser

# Expor a porta padrão do Cloud Run (internamente ele injeta $PORT)
EXPOSE 8080

# Comando de execução do Uvicorn (um worker é suficiente no Cloud Run)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]