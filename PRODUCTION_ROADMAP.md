# MemoryOS MCP Production Roadmap

The current MemoryOS MCP server is a **stdio MCP server**.

That is the right v1 for:

- Claude Desktop
- Cursor / IDE agents
- local developer agents
- internal tools where the agent runtime starts the MCP process

In this mode, the MCP client starts `memoryo-mcp` automatically from config:

```json
{
  "mcpServers": {
    "memoryos": {
      "command": "memoryo-mcp",
      "env": {
        "MEMORYOS_API_KEY": "mem_live_xxx",
        "MEMORYOS_API_URL": "https://api.memoryo.dev"
      }
    }
  }
}
```

## Do not oversell stdio as the production sidecar story

Before selling MCP as a production Docker/Kubernetes sidecar for customer support platforms, MemoryOS should add an **HTTP/SSE MCP transport**.

That production sidecar story should look like:

```text
Customer support agent service
  -> HTTP/SSE MCP client
  -> memoryo-mcp sidecar/service
  -> MemoryOS API
```

This is different from stdio MCP, where the agent runtime launches the server process directly.

## Required before production-sidecar positioning

- Add HTTP/SSE transport while keeping stdio support.
- Add Docker image for `memoryo-mcp`.
- Add health endpoint for orchestration.
- Add structured logs without leaking memory content or API keys.
- Add request timeout and retry configuration.
- Add deployment examples for Docker Compose and Kubernetes.
- Add guidance for secret injection via environment variables or secret managers.
- Add tests for both stdio and HTTP/SSE transports.

## Product positioning until then

Use this wording:

> MemoryOS MCP is ideal for local agents, IDEs, Claude Desktop, and agent runtimes that start MCP tools directly.

Avoid this wording until HTTP/SSE transport exists:

> Run MemoryOS MCP as a production sidecar for high-scale support infrastructure.

For high-scale production backends today, recommend:

- REST API for maximum control
- Python/TypeScript SDK for fastest backend integration
- stdio MCP for agent-runtime and local-tool integrations
