FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app
COPY server.py hangman.html topic_words.json words_alpha.txt ./
EXPOSE 8000
CMD ["python", "server.py"]
