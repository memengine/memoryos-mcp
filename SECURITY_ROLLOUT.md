# Public MCP security rollout checklist

## Completed safeguards

- [x] Public HTTP requires Clerk OAuth and rejects anonymous startup.
- [x] Public HTTP rejects shared tenant, agent, and UUI credentials.
- [x] Public tenant calls forward the authenticated Clerk access token; the API resolves the tenant from the signed active `org_id` claim.
- [x] Public MCP permits only tenant memory read/add/retrieve and read-only billing routes.
- [x] Raw API routing, configuration/admin mutations, destructive tools, and universal tools are refused in public mode.
- [x] Universal job status is bound to the submitting Universal User and global agent before any status is returned.
- [x] Public Universal calls use a five-minute server-issued capability bound to one Universal User, one Global Agent, and one PermissionGrant.
- [x] Every capability call reloads the user, agent, and grant; revocation, expiry, or deactivation is rejected immediately.

## Required before public tenant deployment

- [ ] Configure the dedicated Clerk OAuth access-token template with `org_id`.
- [ ] Include `email` and boolean `email_verified: true` in that same Clerk template; this links a verified Clerk user to their Universal profile without accepting a client-supplied UUI token.
- [ ] Configure `mcp_clerk_audience` with the dedicated MCP OAuth client ID and `clerk_jwt_audiences` with both the dashboard and MCP client IDs. Public MCP requests fail closed until the dedicated audience is configured.
- [ ] Store MCP OAuth and signing secrets in the deployment secret store; never use tenant API keys.
- [ ] Run tenant isolation checks with two organisations and confirm a caller cannot read a job or memory from the other organisation.
- [ ] Confirm logs contain no `Authorization`, API-key, UUI-token, message content, or memory content values.

## Required before enabling public universal MCP

- [x] Add a separate server-issued universal capability that is bound to one Global Agent, one Universal User, and one active PermissionGrant.
- [x] Re-check the PermissionGrant on each sensitive universal action and invalidate access immediately on revocation/expiry.
- [x] Never accept a tenant capability on `/v1/universal/*` or a universal capability on tenant/billing routes.
- [x] Reject agent API keys and UUI tokens supplied to public Universal MCP tool calls.
- [ ] Create one dedicated, active Global Agent for the public MCP service. Store its returned agent API key only in the MCP host's secret store as `MEMORYOS_MCP_UNIVERSAL_AGENT_API_KEY`; never put it in Terraform, the browser, or an API task definition.
- [ ] Create `memoryos/MCP_UNIVERSAL_CAPABILITY_SECRET` with a new random value before applying the backend Terraform change. This secret is injected only into API and worker tasks.
- [ ] Enable `MEMORYOS_MCP_UNIVERSAL_ENABLED=true` only after the two prior steps and the isolation checks below pass.
- [ ] Add isolation tests for agent A versus agent B, user A versus user B, grant revocation, category restrictions, and job ownership.

## Explicitly out of scope for the first public release

- Billing checkout/payment actions.
- Raw route execution.
- Tenant settings, support configuration, user blocking, and hard deletion.
- Global-agent registration and consent administration from the public MCP surface.
