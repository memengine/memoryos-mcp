FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MEMORYOS_MCP_TRANSPORT=streamable-http \
    MEMORYOS_MCP_HOST=0.0.0.0 \
    MEMORYOS_MCP_PORT=8080 \
    MEMORYOS_MCP_HTTP_PATH=/mcp

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY memoryos_mcp ./memoryos_mcp
RUN pip install --no-cache-dir . && useradd --create-home --uid 10001 memoryos

USER memoryos
EXPOSE 8080
CMD ["memoryo-mcp"]
