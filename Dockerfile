FROM python:3.12-slim
RUN pip install --no-cache-dir "notebooklm-py[headless]" feedparser PyYAML "trafilatura>=2,<3"
WORKDIR /app
COPY briefing.py ./
ENV BRIEFING_OUT=/data BRIEFING_CONFIG=/data/config.yaml
CMD ["python", "briefing.py"]
