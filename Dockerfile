# Stoner Hideout bot. Upload bot.py, requirements.txt, and this Dockerfile.
FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY bot.py .
EXPOSE 8000
CMD ["python", "-u", "bot.py"]
