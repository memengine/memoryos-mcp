# MemoryOS MCP Server

Run MemoryOS as a Model Context Protocol server for agent runtimes, IDEs, and local AI tools.

The MCP server is a sidecar around the MemoryOS API. It does not choose the memory schema itself. Your workspace setting still decides whether `add` and `get` use the General Engine, EdTech Schema, Customer Support Schema, or a future domain schema.

## Install

From this repository:

```bash
cd memoryos-mcp
pip install -e .
```

When published:

```bash
pip install memoryo-mcp
```

## Configure

```bash
export MEMORYOS_API_KEY=mem_live_xxx
export MEMORYOS_API_URL=https://api.memoryo.dev
```

For local Docker:

```bash
export MEMORYOS_API_URL=http://localhost:8000
```

For cross-agent universal memory tools, also set:

```bash
export MEMORYOS_AGENT_API_KEY=mem_agent_xxx
export MEMORYOS_UUI_TOKEN=uui_xxx
```

## Run

```bash
memoryo-mcp
```

## HTTP transport for private sidecars

The default remains stdio. For a private sidecar or internal service, enable
streamable HTTP explicitly:

```bash
export MEMORYOS_MCP_TRANSPORT=streamable-http
export MEMORYOS_MCP_HOST=127.0.0.1
export MEMORYOS_MCP_PORT=8080
export MEMORYOS_MCP_HTTP_PATH=/mcp
memoryo-mcp
```

The MCP endpoint is `http://127.0.0.1:8080/mcp`; process health is available at
`http://127.0.0.1:8080/healthz`.

HTTP mode carries the configured MemoryOS API key on behalf of the MCP caller.
Keep it private: bind it to loopback as a sidecar, or place it on an authenticated
internal network. Do not expose it through a public ingress.

## Public MCP with Clerk OAuth

For a public MCP URL, set `MEMORYOS_MCP_EXPOSURE=public`. The server will refuse
to start unless Clerk OAuth is configured; anonymous public mode is deliberately
unsupported.

Create a dedicated Clerk OAuth application for MCP, register the FastMCP callback
`https://<your-mcp-domain>/auth/callback`, and store its secrets outside source
control. Configure:

```bash
export MEMORYOS_MCP_TRANSPORT=streamable-http
export MEMORYOS_MCP_EXPOSURE=public
export MEMORYOS_MCP_PUBLIC_BASE_URL=https://mcp.example.com
export MEMORYOS_MCP_AUTH_MODE=clerk
export MEMORYOS_MCP_CLERK_DOMAIN=clerk.memoryo.dev
export MEMORYOS_MCP_CLERK_CLIENT_ID=...
export MEMORYOS_MCP_CLERK_CLIENT_SECRET=...
export MEMORYOS_MCP_AUTH_SIGNING_KEY=...
export MEMORYOS_MCP_ALLOWED_CLIENT_REDIRECT_URIS=https://<approved-client-callback>
```

`MEMORYOS_MCP_AUTH_SIGNING_KEY` must be a new random secret, not a MemoryOS API
key, Razorpay key, or Clerk secret. In public mode, do **not** set
`MEMORYOS_API_KEY`, `MEMORYOS_AGENT_API_KEY`, or `MEMORYOS_UUI_TOKEN`; startup
will reject them. The caller's verified Clerk access token is forwarded to the
MemoryOS API, which independently validates it and resolves the tenant only from
the active `org_id` claim. Configure the Clerk access-token template used by this
OAuth application to include `org_id`, `email`, and boolean `email_verified`.

The public tenant surface is memory read/add/retrieve, memory detail/history/job
reads, and billing-plan/subscription reads. Raw requests, configuration/admin,
destructive actions, and global-agent registration remain private.

### Optional public Universal Memory

Universal tools are disabled by default. To enable them, register exactly one
dedicated Global Agent for the MCP service and store the returned agent key only
in the MCP host secret store. Do not use a tenant API key or a caller-provided
UUI token:

```bash
export MEMORYOS_MCP_UNIVERSAL_ENABLED=true
export MEMORYOS_MCP_UNIVERSAL_AGENT_API_KEY=mem_agent_server_only
```

For each Universal request, the server exchanges the verified Clerk caller for a
five-minute capability. The API reloads that caller's Universal profile, the MCP
Global Agent, and its live consent grant before every operation. Revoking consent
therefore blocks further calls immediately, even before the capability expires.
The token template must contain verified `email`, and the user must already have
a Universal profile and active consent grant for this MCP Global Agent.

Example Claude Desktop config:

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

## Tools

Core tenant memory:

- `memoryos_add_memory`
- `memoryos_get_context`
- `memoryos_list_memories`
- `memoryos_get_memory`
- `memoryos_update_memory`
- `memoryos_delete_memory`
- `memoryos_get_memory_history`
- `memoryos_get_job_status`
- `memoryos_export_user`
- `memoryos_get_user_stats`
- `memoryos_block_user`

Domain and workspace tools:

- `memoryos_get_domain_schema`
- `memoryos_set_domain_schema`
- `memoryos_get_edtech_profile`
- `memoryos_set_support_type`
- `memoryos_list_support_customers`
- `memoryos_get_support_stats`

Cross-agent tools:

- `memoryos_register_global_agent`
- `memoryos_get_global_agent`
- `memoryos_create_consent_url`
- `memoryos_universal_add_memory`
- `memoryos_universal_get_context`
- `memoryos_universal_get_job_status`

Other:

- `memoryos_list_billing_plans`
- `memoryos_get_billing_subscription`
- `memoryos_api_request`

`memoryos_api_request` is an allowlisted escape hatch for new MemoryOS API endpoints that are not yet first-class MCP tools. It only allows `/v1/memories`, `/v1/users`, `/v1/tenant`, `/v1/agents`, `/v1/billing/plans`, and `/v1/universal`.

Billing checkout creation and payment-signature verification are intentionally not MCP tools. They are browser/payment flows and must remain in the tenant dashboard with server-side verification.

## Production Notes

MCP is best when a company wants MemoryOS outside their main application process. They can run it as a sidecar and let their agent runtime call MemoryOS as tools.

For customer support and other live business workflows, MemoryOS MCP supplies memory context. The customer's own tools still provide live truth and actions such as `get_order`, `create_refund`, `get_invoice`, or `update_ticket`.
