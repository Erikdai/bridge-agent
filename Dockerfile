# Minimal image for MCP registries to start the server and introspect its tools.
# The executors themselves need Windows (UI Automation); run the real thing with `bridgeagent mcp` on Windows.
FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .
CMD ["bridgeagent", "mcp"]
