FROM python:3.11-slim
WORKDIR /app
COPY . .
ENV PORT=8000
EXPOSE 8000
CMD python3 -m http.server "${PORT:-8000}" --directory /app --bind 0.0.0.0
