from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from typing import Any
from urllib.parse import urlencode
from urllib.parse import urlsplit

import httpx
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.responses import Response

from memoryos_mcp import __version__

SERVER_NAME = "memoryos-mcp"
SERVER_VERSION = __version__
DEFAULT_API_URL = "https://api.memoryo.dev"
DEFAULT_CONSENT_URL = "https://consent.memoryo.dev"
ALLOWED_RAW_PATH_PREFIXES = (
    "/v1/memories",
    "/v1/users",
    "/v1/tenant",
    "/v1/agents",
    "/v1/billing/plans",
    "/v1/universal",
)
ALLOWED_RAW_METHODS = frozenset({"GET", "POST", "PATCH", "DELETE"})
HTTP_TRANSPORTS = frozenset({"http", "streamable-http", "sse"})
MCP_EXPOSURES = frozenset({"private", "public"})
AUTH_MODES = frozenset({"none", "clerk"})
CLERK_TENANT_SCOPES = ["openid", "email", "profile", "user:org:read"]
LOGGER = logging.getLogger(SERVER_NAME)

# A public gateway intentionally starts with a small, capability-safe surface.
# Administrative, destructive, raw, and cross-agent calls stay private until
# their distinct authorisation lanes have been implemented and tested.
PUBLIC_TENANT_REQUESTS = frozenset(
    {
        ("GET", "/v1/memories"),
        ("POST", "/v1/memories/add"),
        ("POST", "/v1/memories/retrieve"),
        ("POST", "/v1/mcp/tenant/remember"),
        ("POST", "/v1/mcp/tenant/context"),
        ("GET", "/v1/mcp/tenant/memories"),
        ("GET", "/v1/billing/plans"),
        ("GET", "/v1/billing/subscription"),
    }
)


def _public_universal_enabled() -> bool:
    """Whether this public MCP instance may use its dedicated global agent."""
    return os.getenv("MEMORYOS_MCP_UNIVERSAL_ENABLED", "false").strip().lower() == "true"


class MemoryOSAPIError(RuntimeError):
    """A redacted HTTP error safe to return through an MCP tool."""

    def __init__(self, *, status_code: int, code: str | None, request_id: str | None) -> None:
        self.status_code = status_code
        self.code = code
        self.request_id = request_id
        super().__init__(
            json.dumps(
                {
                    "status_code": status_code,
                    "code": code or "MEMORYOS_API_ERROR",
                    "request_id": request_id,
                },
                sort_keys=True,
            )
        )


def _is_allowed_raw_path(path: str) -> bool:
    """Allow only a relative API path with an exact configured route boundary."""
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
        return False
    if "\\" in path or any(ord(character) < 32 for character in path):
        return False

    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or parsed.path != path:
        return False
    if any(segment in {".", ".."} for segment in parsed.path.split("/")):
        return False

    return any(
        path == prefix or path.startswith(f"{prefix}/")
        for prefix in ALLOWED_RAW_PATH_PREFIXES
    )


def _normalize_raw_method(method: str) -> str:
    if not isinstance(method, str):
        raise ValueError("method must be a string")
    normalized = method.strip().upper()
    if normalized not in ALLOWED_RAW_METHODS:
        allowed = ", ".join(sorted(ALLOWED_RAW_METHODS))
        raise ValueError(f"method must be one of: {allowed}")
    return normalized


def _log_event(event: str, **fields: Any) -> None:
    """Emit operational logs without request bodies, headers, or credentials."""
    LOGGER.info(json.dumps({"event": event, **fields}, sort_keys=True, default=str))


def _required_environment(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"{name} must be configured")
    return value


def _mcp_exposure() -> str:
    exposure = os.getenv("MEMORYOS_MCP_EXPOSURE", "private").strip().lower()
    if exposure not in MCP_EXPOSURES:
        allowed = ", ".join(sorted(MCP_EXPOSURES))
        raise ValueError(f"MEMORYOS_MCP_EXPOSURE must be one of: {allowed}")
    return exposure


def _public_tenant_request_allowed(method: str, path: str) -> bool:
    """Return whether a public MCP caller may invoke this tenant API route."""
    if (method, path) in PUBLIC_TENANT_REQUESTS:
        return True
    # The detail, history, list, and job-status reads are tenant-isolated by
    # the API. No tenant, user, or organisation identifier is accepted here.
    return method == "GET" and (
        path.startswith("/v1/memories/")
        or path == "/v1/memories"
    )


def _reject_public_generic_identity_tool() -> None:
    """Keep public callers on self-scoped tools, never caller-selected identities."""
    if _mcp_exposure() == "public":
        raise PermissionError(
            "This public MCP tool requires a caller-selected external_user_id. "
            "Use the self-scoped memoryos_my_* tools instead."
        )


def _public_caller_bearer_token() -> str | None:
    """Get the verified upstream Clerk access token for the active MCP call.

    FastMCP's OAuth proxy validates its own reference token, then exposes the
    verified upstream Clerk token through the request context.  The API still
    independently verifies that Clerk token and derives the tenant from its
    `org_id`; the MCP process never selects a tenant or holds a shared tenant
    API key in public mode.
    """
    if _mcp_exposure() != "public":
        return None
    from fastmcp.server.dependencies import get_access_token

    access_token = get_access_token()
    token = str(getattr(access_token, "token", "") or "").strip()
    if not token:
        raise PermissionError("Public MemoryOS MCP requires an authenticated Clerk session.")
    return token


def _build_mcp_auth() -> Any | None:
    """Configure inbound MCP authentication without ever reusing tenant API keys."""
    mode = os.getenv("MEMORYOS_MCP_AUTH_MODE", "none").strip().lower()
    if mode not in AUTH_MODES:
        allowed = ", ".join(sorted(AUTH_MODES))
        raise ValueError(f"MEMORYOS_MCP_AUTH_MODE must be one of: {allowed}")
    if mode == "none":
        return None

    # Import only when enabled so stdio/local use has no OAuth configuration cost.
    from fastmcp.server.auth.providers.clerk import ClerkProvider

    redirect_uris = [
        uri.strip()
        for uri in _required_environment("MEMORYOS_MCP_ALLOWED_CLIENT_REDIRECT_URIS").split(",")
        if uri.strip()
    ]
    if not redirect_uris:
        raise ValueError("MEMORYOS_MCP_ALLOWED_CLIENT_REDIRECT_URIS must contain at least one URI")
    return ClerkProvider(
        domain=_required_environment("MEMORYOS_MCP_CLERK_DOMAIN").removeprefix("https://").rstrip("/"),
        client_id=_required_environment("MEMORYOS_MCP_CLERK_CLIENT_ID"),
        client_secret=_required_environment("MEMORYOS_MCP_CLERK_CLIENT_SECRET"),
        base_url=_required_environment("MEMORYOS_MCP_PUBLIC_BASE_URL").rstrip("/"),
        resource_base_url=_required_environment("MEMORYOS_MCP_PUBLIC_BASE_URL").rstrip("/"),
        jwt_signing_key=_required_environment("MEMORYOS_MCP_AUTH_SIGNING_KEY"),
        allowed_client_redirect_uris=redirect_uris,
        required_scopes=CLERK_TENANT_SCOPES,
        valid_scopes=CLERK_TENANT_SCOPES,
    )


def _http_server_config() -> dict[str, Any]:
    transport = os.getenv("MEMORYOS_MCP_TRANSPORT", "stdio").strip().lower()
    if transport == "stdio":
        return {"transport": "stdio"}
    if transport not in HTTP_TRANSPORTS:
        allowed = ", ".join(["stdio", *sorted(HTTP_TRANSPORTS)])
        raise ValueError(f"MEMORYOS_MCP_TRANSPORT must be one of: {allowed}")

    exposure = _mcp_exposure()
    if exposure == "public" and os.getenv("MEMORYOS_MCP_AUTH_MODE", "none").strip().lower() != "clerk":
        raise ValueError(
            "Public HTTP MCP requires MEMORYOS_MCP_AUTH_MODE=clerk. "
            "Do not expose a tenant-API-key-backed MCP server anonymously."
        )
    if exposure == "public":
        static_credentials = (
            "MEMORYOS_API_KEY",
            "MEMORYOS_AGENT_API_KEY",
            "MEMORYOS_UUI_TOKEN",
        )
        configured = [name for name in static_credentials if os.getenv(name, "").strip()]
        if configured:
            raise ValueError(
                "Public HTTP MCP must not be configured with shared API credentials: "
                + ", ".join(configured)
            )
        if _public_universal_enabled() and not os.getenv(
            "MEMORYOS_MCP_UNIVERSAL_AGENT_API_KEY", ""
        ).strip():
            raise ValueError(
                "Public universal MCP requires MEMORYOS_MCP_UNIVERSAL_AGENT_API_KEY."
            )

    host = os.getenv("MEMORYOS_MCP_HOST", "127.0.0.1").strip()
    if not host:
        raise ValueError("MEMORYOS_MCP_HOST must not be empty")
    try:
        port = int(os.getenv("MEMORYOS_MCP_PORT", "8080"))
    except ValueError as exc:
        raise ValueError("MEMORYOS_MCP_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("MEMORYOS_MCP_PORT must be between 1 and 65535")
    path = os.getenv("MEMORYOS_MCP_HTTP_PATH", "/mcp").strip()
    if not path.startswith("/") or path.startswith("//") or "?" in path or "#" in path:
        raise ValueError("MEMORYOS_MCP_HTTP_PATH must be an absolute path without query or fragment")
    return {"transport": transport, "host": host, "port": port, "path": path}

# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------

class MemoryOSClient:
    def __init__(self) -> None:
        self.base_url = os.getenv("MEMORYOS_API_URL", DEFAULT_API_URL).rstrip("/")
        self.api_key = os.getenv("MEMORYOS_API_KEY", "")
        self.agent_api_key = os.getenv("MEMORYOS_AGENT_API_KEY", "")
        self.uui_token = os.getenv("MEMORYOS_UUI_TOKEN", "")
        self.timeout = float(os.getenv("MEMORYOS_TIMEOUT", "30"))
        self.client = httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout,
            follow_redirects=False,
        )

    def close(self) -> None:
        self.client.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        api_key: str | None = None,
        uui_token: str | None = None,
        universal_capability: str | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        public_bearer = _public_caller_bearer_token()
        is_public_universal = bool(public_bearer and universal_capability)
        if public_bearer and not is_public_universal and not _public_tenant_request_allowed(method, path):
            raise PermissionError(
                f"{method} {path} is unavailable through public MemoryOS MCP. "
                "Use the private sidecar for administrative or cross-agent operations."
            )
        resolved_key = api_key if api_key is not None else self.api_key
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": f"{SERVER_NAME}/{SERVER_VERSION}",
        }
        if is_public_universal:
            headers["Authorization"] = f"Bearer {universal_capability}"
        elif public_bearer and api_key is None:
            headers["Authorization"] = f"Bearer {public_bearer}"
            # This marker never conveys identity. It only makes the API apply
            # the stricter active-organisation requirement to MCP traffic.
            headers["X-MemoryOS-MCP-Client"] = "public-v1"
        elif resolved_key:
            headers["Authorization"] = f"ApiKey {resolved_key}"
        if uui_token:
            headers["X-MemoryOS-UUI"] = uui_token
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        started = time.perf_counter()
        try:
            response = self.client.request(method, path, params=params, json=json_body, headers=headers)
        except httpx.HTTPError as exc:
            _log_event(
                "memoryos_api_request_failed",
                method=method,
                path=path,
                error_type=type(exc).__name__,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            raise RuntimeError("MemoryOS API request failed before a response.") from exc
        try:
            payload: Any = response.json()
        except ValueError:
            payload = None
        if response.status_code >= 400:
            _log_event(
                "memoryos_api_response",
                method=method,
                path=path,
                status_code=response.status_code,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            error_payload = payload if isinstance(payload, dict) else {}
            raise MemoryOSAPIError(
                status_code=response.status_code,
                code=(
                    str(error_payload["code"])
                    if error_payload.get("code") is not None
                    else None
                ),
                request_id=(
                    str(error_payload["request_id"])
                    if error_payload.get("request_id") is not None
                    else None
                ),
            )
        _log_event(
            "memoryos_api_response",
            method=method,
            path=path,
            status_code=response.status_code,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        return payload

    def public_universal_capability(self) -> str:
        """Exchange a verified Clerk caller for a short-lived consent capability."""
        if _mcp_exposure() != "public" or not _public_universal_enabled():
            raise PermissionError("Universal public MCP is not enabled.")
        if self.api_key:
            raise PermissionError("Public MCP must not use MEMORYOS_API_KEY.")
        bearer = _public_caller_bearer_token()
        agent_key = os.getenv("MEMORYOS_MCP_UNIVERSAL_AGENT_API_KEY", "").strip()
        if not bearer or not agent_key:
            raise PermissionError("Public universal MCP is not configured.")
        response = self.client.post(
            "/v1/mcp/universal/capability",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {bearer}",
                "X-MemoryOS-MCP-Client": "public-v1",
                "X-MemoryOS-MCP-Universal-Agent-Key": agent_key,
                "User-Agent": f"{SERVER_NAME}/{SERVER_VERSION}",
            },
        )
        try:
            payload: Any = response.json()
        except ValueError:
            payload = None
        if response.status_code >= 400:
            error_payload = payload if isinstance(payload, dict) else {}
            raise MemoryOSAPIError(
                status_code=response.status_code,
                code=str(error_payload.get("code") or "MEMORYOS_API_ERROR"),
                request_id=str(error_payload.get("request_id") or "") or None,
            )
        capability = str((payload or {}).get("data", {}).get("capability", "")).strip()
        if not capability:
            raise RuntimeError("MemoryOS API returned an invalid universal capability response.")
        return capability

    def universal_credentials(self, agent_api_key: str | None, uui_token: str | None) -> tuple[str, str]:
        if _mcp_exposure() == "public":
            if agent_api_key or uui_token:
                raise PermissionError("Public universal MCP does not accept caller-supplied credentials.")
            raise PermissionError("Use public_universal_capability for public universal MCP.")
        key = agent_api_key or self.agent_api_key
        token = uui_token or self.uui_token
        if not key:
            raise ValueError("Universal tools require agent_api_key or MEMORYOS_AGENT_API_KEY.")
        if not token:
            raise ValueError("Universal tools require uui_token or MEMORYOS_UUI_TOKEN.")
        return key, token


# Module-level client and FastMCP app. OAuth configuration is read at process start.
_client = MemoryOSClient()
mcp = FastMCP(SERVER_NAME, version=SERVER_VERSION, auth=_build_mcp_auth())


@mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
async def memoryos_mcp_healthz(_: Request) -> Response:
    """Return process health without exposing API configuration or secrets."""
    return JSONResponse(
        {
            "status": "ok",
            "server": SERVER_NAME,
            "version": SERVER_VERSION,
        }
    )

# ---------------------------------------------------------------------------
# Core tenant memory tools
# ---------------------------------------------------------------------------

@mcp.tool()
def memoryos_add_memory(
    external_user_id: str,
    messages: list[dict],
    agent_id: str | None = None,
    metadata: dict | None = None,
    idempotency_key: str | None = None,
) -> Any:
    """Queue tenant-scoped conversation messages for MemoryOS extraction.
    Uses the workspace's configured general or domain schema."""
    _reject_public_generic_identity_tool()
    body: dict[str, Any] = {
        "external_user_id": external_user_id,
        "messages": messages,
        "metadata": metadata or {},
    }
    if agent_id is not None:
        body["agent_id"] = agent_id
    return _client.request(
        "POST",
        "/v1/memories/add",
        json_body=body,
        idempotency_key=idempotency_key,
    )


@mcp.tool()
def memoryos_get_context(
    external_user_id: str,
    query: str,
    limit: int = 10,
    categories: list[str] | None = None,
    agent_id: str | None = None,
    time_filter_days: int | None = None,
    format: str = "bullets",
    context_max_tokens: int = 500,
    as_of: str | None = None,
) -> Any:
    """Retrieve prompt-ready memory context for a tenant-scoped user.
    Domain schema context is included by the MemoryOS backend when enabled."""
    _reject_public_generic_identity_tool()
    body: dict[str, Any] = {
        "external_user_id": external_user_id,
        "query": query,
        "limit": limit,
        "categories": categories or [],
        "format": format,
        "context_max_tokens": context_max_tokens,
    }
    if agent_id is not None:
        body["agent_id"] = agent_id
    if time_filter_days is not None:
        body["time_filter_days"] = time_filter_days
    if as_of is not None:
        parsed_as_of = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
        if parsed_as_of.tzinfo is None:
            raise ValueError("as_of must include a timezone")
        body["as_of"] = as_of
    return _client.request("POST", "/v1/memories/retrieve", json_body=body)


@mcp.tool()
def memoryos_list_memories(
    external_user_id: str,
    cursor: str | None = None,
    limit: int = 10,
    categories: list[str] | None = None,
    agent_id: str | None = None,
) -> Any:
    """List tenant-scoped memories for a user."""
    _reject_public_generic_identity_tool()
    params: dict[str, Any] = {
        "external_user_id": external_user_id,
        "limit": limit,
    }
    if cursor is not None:
        params["cursor"] = cursor
    if agent_id is not None:
        params["agent_id"] = agent_id
    if categories:
        params["categories"] = categories
    return _client.request("GET", "/v1/memories", params=params)


@mcp.tool()
def memoryos_remember(
    messages: list[dict],
    metadata: dict | None = None,
    idempotency_key: str | None = None,
) -> Any:
    """Remember this conversation for the signed-in user, without requiring a user ID."""
    return _client.request(
        "POST",
        "/v1/mcp/tenant/remember",
        json_body={"messages": messages, "metadata": metadata or {}},
        idempotency_key=idempotency_key,
    )


@mcp.tool()
def memoryos_my_context(
    query: str,
    limit: int = 10,
    categories: list[str] | None = None,
    format: str = "bullets",
    context_max_tokens: int = 500,
) -> Any:
    """Get prompt-ready MemoryOS context for the signed-in user before answering."""
    return _client.request(
        "POST",
        "/v1/mcp/tenant/context",
        json_body={
            "query": query,
            "limit": limit,
            "categories": categories or [],
            "format": format,
            "context_max_tokens": context_max_tokens,
        },
    )


@mcp.tool()
def memoryos_my_memories(
    cursor: str | None = None,
    limit: int = 10,
    categories: list[str] | None = None,
) -> Any:
    """List memories belonging only to the signed-in user."""
    params: dict[str, Any] = {"limit": limit}
    if cursor is not None:
        params["cursor"] = cursor
    if categories:
        params["categories"] = categories
    return _client.request("GET", "/v1/mcp/tenant/memories", params=params)


@mcp.tool()
def memoryos_get_memory(memory_id: str) -> Any:
    """Fetch one memory by id."""
    _reject_public_generic_identity_tool()
    return _client.request("GET", f"/v1/memories/{memory_id}")


@mcp.tool()
def memoryos_update_memory(
    memory_id: str,
    content: str | None = None,
    importance_score: float | None = None,
    is_archived: bool | None = None,
) -> Any:
    """Update memory content, importance, or archived state."""
    _reject_public_generic_identity_tool()
    body = {
        k: v
        for k, v in {
            "content": content,
            "importance_score": importance_score,
            "is_archived": is_archived,
        }.items()
        if v is not None
    }
    return _client.request("PATCH", f"/v1/memories/{memory_id}", json_body=body)


@mcp.tool()
def memoryos_delete_memory(memory_id: str, hard_delete: bool = False) -> Any:
    """Archive or hard-delete a memory."""
    _reject_public_generic_identity_tool()
    return _client.request(
        "DELETE",
        f"/v1/memories/{memory_id}",
        params={"hard_delete": str(hard_delete).lower()},
    )


@mcp.tool()
def memoryos_get_memory_history(memory_id: str) -> Any:
    """Fetch append-only version history for a memory."""
    _reject_public_generic_identity_tool()
    return _client.request("GET", f"/v1/memories/{memory_id}/history")


@mcp.tool()
def memoryos_get_job_status(job_id: str) -> Any:
    """Fetch extraction job status."""
    _reject_public_generic_identity_tool()
    return _client.request("GET", f"/v1/memories/jobs/{job_id}")


# ---------------------------------------------------------------------------
# User management tools
# ---------------------------------------------------------------------------

@mcp.tool()
def memoryos_export_user(external_user_id: str) -> Any:
    """Export a tenant user's memories and version history for access requests."""
    return _client.request("GET", f"/v1/users/{external_user_id}/export")


@mcp.tool()
def memoryos_get_user_stats(external_user_id: str) -> Any:
    """Fetch tenant-scoped memory stats for one external user."""
    return _client.request("GET", f"/v1/users/{external_user_id}/stats")


@mcp.tool()
def memoryos_block_user(external_user_id: str) -> Any:
    """Block a tenant-scoped user from future memory operations."""
    return _client.request("POST", f"/v1/users/{external_user_id}/block")


# ---------------------------------------------------------------------------
# Domain / workspace tools
# ---------------------------------------------------------------------------

@mcp.tool()
def memoryos_get_edtech_profile(external_user_id: str) -> Any:
    """Fetch the structured EdTech profile for a user when the EdTech schema is enabled."""
    _reject_public_generic_identity_tool()
    return _client.request(
        "GET",
        "/v1/memories/edtech-profile",
        params={"external_user_id": external_user_id},
    )


@mcp.tool()
def memoryos_get_domain_schema() -> Any:
    """Read the workspace domain schema setting."""
    return _client.request("GET", "/v1/tenant/domain-schema")


@mcp.tool()
def memoryos_set_domain_schema(domain_schema: str | None) -> Any:
    """Set the workspace domain schema.
    Use null for General Engine, 'edtech' for EdTech, or 'support' for Customer Support."""
    return _client.request(
        "PATCH",
        "/v1/tenant/domain-schema",
        json_body={"domain_schema": domain_schema},
    )


@mcp.tool()
def memoryos_set_support_type(
    support_type_mode: str,
    support_type: str | None = None,
    support_types_allowed: list[str] | None = None,
) -> Any:
    """Configure Customer Support routing mode and allowed support types."""
    body: dict[str, Any] = {
        "support_type_mode": support_type_mode,
        "support_type": support_type,
        "support_types_allowed": support_types_allowed or [],
    }
    return _client.request("PATCH", "/v1/tenant/support-type", json_body=body)


@mcp.tool()
def memoryos_list_support_customers(
    cursor: str | None = None,
    limit: int = 50,
) -> Any:
    """List Customer Support schema customer summaries."""
    params: dict[str, Any] = {"limit": limit}
    if cursor is not None:
        params["cursor"] = cursor
    return _client.request("GET", "/v1/tenant/customers", params=params)


@mcp.tool()
def memoryos_get_support_stats() -> Any:
    """Fetch Customer Support schema aggregate stats."""
    return _client.request("GET", "/v1/tenant/support-stats")


# ---------------------------------------------------------------------------
# Billing
# ---------------------------------------------------------------------------

@mcp.tool()
def memoryos_list_billing_plans() -> Any:
    """Fetch public MemoryOS billing plans."""
    return _client.request("GET", "/v1/billing/plans", api_key="")


@mcp.tool()
def memoryos_get_billing_subscription() -> Any:
    """Fetch the authenticated tenant's read-only subscription and plan limits.

    This tool never creates a checkout, verifies a payment, or changes billing.
    """
    return _client.request("GET", "/v1/billing/subscription")


# ---------------------------------------------------------------------------
# Cross-agent / global agent tools
# ---------------------------------------------------------------------------

@mcp.tool()
def memoryos_register_global_agent(
    name: str,
    redirect_uri: str,
    description: str | None = None,
    logo_url: str | None = None,
    website_url: str | None = None,
    default_categories_requested: list[str] | None = None,
) -> Any:
    """Register a global cross-agent MemoryOS agent and receive its agent API key once."""
    body: dict[str, Any] = {
        "name": name,
        "redirect_uri": redirect_uri,
        "description": description,
        "logo_url": logo_url,
        "website_url": website_url,
        "default_categories_requested": default_categories_requested or [],
    }
    return _client.request("POST", "/v1/agents/global", json_body=body)


@mcp.tool()
def memoryos_get_global_agent(agent_id: str) -> Any:
    """Fetch a public global agent profile."""
    return _client.request("GET", f"/v1/agents/global/{agent_id}", api_key="")


@mcp.tool()
def memoryos_universal_add_memory(
    messages: list[dict],
    metadata: dict | None = None,
    agent_api_key: str | None = None,
    uui_token: str | None = None,
    idempotency_key: str | None = None,
) -> Any:
    """Queue cross-agent universal memory extraction."""
    if _mcp_exposure() == "public":
        if agent_api_key or uui_token:
            raise PermissionError("Public universal MCP does not accept caller-supplied credentials.")
        return _client.request(
            "POST",
            "/v1/universal/memories/add",
            json_body={"messages": messages, "metadata": metadata or {}, **({"idempotency_key": idempotency_key} if idempotency_key else {})},
            universal_capability=_client.public_universal_capability(),
        )
    key, token = _client.universal_credentials(agent_api_key, uui_token)
    return _client.request(
        "POST",
        "/v1/universal/memories/add",
        json_body={
            "messages": messages,
            "metadata": metadata or {},
            **({"idempotency_key": idempotency_key} if idempotency_key else {}),
        },
        api_key=key,
        uui_token=token,
    )


@mcp.tool()
def memoryos_universal_get_context(
    query: str,
    limit: int = 10,
    format: str = "bullets",
    context_max_tokens: int = 500,
    agent_api_key: str | None = None,
    uui_token: str | None = None,
) -> Any:
    """Retrieve cross-agent universal memory context."""
    if _mcp_exposure() == "public":
        if agent_api_key or uui_token:
            raise PermissionError("Public universal MCP does not accept caller-supplied credentials.")
        return _client.request(
            "POST",
            "/v1/universal/memories/retrieve",
            json_body={"query": query, "limit": limit, "format": format, "context_max_tokens": context_max_tokens},
            universal_capability=_client.public_universal_capability(),
        )
    key, token = _client.universal_credentials(agent_api_key, uui_token)
    return _client.request(
        "POST",
        "/v1/universal/memories/retrieve",
        json_body={
            "query": query,
            "limit": limit,
            "format": format,
            "context_max_tokens": context_max_tokens,
        },
        api_key=key,
        uui_token=token,
    )


@mcp.tool()
def memoryos_universal_get_job_status(
    job_id: str,
    agent_api_key: str | None = None,
    uui_token: str | None = None,
) -> Any:
    """Fetch universal memory extraction job status."""
    if _mcp_exposure() == "public":
        if agent_api_key or uui_token:
            raise PermissionError("Public universal MCP does not accept caller-supplied credentials.")
        return _client.request(
            "GET",
            f"/v1/universal/memories/jobs/{job_id}",
            universal_capability=_client.public_universal_capability(),
        )
    key, token = _client.universal_credentials(agent_api_key, uui_token)
    return _client.request(
        "GET",
        f"/v1/universal/memories/jobs/{job_id}",
        api_key=key,
        uui_token=token,
    )


@mcp.tool()
def memoryos_create_consent_url(
    agent_id: str,
    redirect_uri: str,
    state: str | None = None,
    consent_base_url: str | None = None,
) -> dict[str, str]:
    """Create a MemoryOS consent URL for cross-agent memory sharing."""
    base = (consent_base_url or os.getenv("MEMORYOS_CONSENT_URL") or DEFAULT_CONSENT_URL).rstrip("/")
    query: dict[str, str] = {"agent_id": agent_id, "redirect_uri": redirect_uri}
    if state is not None:
        query["state"] = state
    return {"consent_url": f"{base}/consent?{urlencode(query)}"}


# ---------------------------------------------------------------------------
# Raw / escape-hatch tool
# ---------------------------------------------------------------------------

@mcp.tool()
def memoryos_api_request(
    method: str,
    path: str,
    params: dict | None = None,
    json: dict | None = None,
    use_universal_auth: bool = False,
    agent_api_key: str | None = None,
    uui_token: str | None = None,
) -> Any:
    """Advanced allowlisted MemoryOS API request for endpoints not yet promoted
    to first-class MCP tools."""
    if _mcp_exposure() == "public":
        raise PermissionError("Raw API requests are unavailable through public MemoryOS MCP.")
    if not _is_allowed_raw_path(path):
        raise ValueError(f"Path is not allowlisted for MCP raw requests: {path}")
    normalized_method = _normalize_raw_method(method)
    is_universal_path = path == "/v1/universal" or path.startswith("/v1/universal/")
    if use_universal_auth != is_universal_path:
        raise ValueError(
            "Universal paths require use_universal_auth=true, and universal credentials "
            "may only be used with /v1/universal paths."
        )
    key: str | None = None
    token: str | None = None
    if use_universal_auth:
        if _mcp_exposure() == "public":
            raise PermissionError("Raw universal requests are disabled on public MCP.")
        key, token = _client.universal_credentials(agent_api_key, uui_token)
    return _client.request(
        normalized_method,
        path,
        params=params,
        json_body=json,
        api_key=key,
        uui_token=token,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    config = _http_server_config()
    if config["transport"] == "stdio":
        _log_event("memoryos_mcp_starting", transport="stdio")
        mcp.run("stdio", show_banner=False)
        return

    if config["host"] in {"0.0.0.0", "::"}:
        LOGGER.warning(
            "MemoryOS MCP is listening on all interfaces. Keep it behind a private "
            "network boundary and never expose its API-key-backed transport publicly."
        )
    _log_event("memoryos_mcp_starting", **config)
    mcp.run(show_banner=False, **config)


if __name__ == "__main__":
    main()
