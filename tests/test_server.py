from __future__ import annotations

import asyncio
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from memoryos_mcp.server import MemoryOSAPIError
from memoryos_mcp.server import MemoryOSClient
from memoryos_mcp.server import _build_mcp_auth
from memoryos_mcp.server import _http_server_config
from memoryos_mcp.server import _public_tenant_request_allowed
from memoryos_mcp.server import main
from memoryos_mcp.server import mcp
from memoryos_mcp.server import memoryos_create_consent_url
from memoryos_mcp.server import memoryos_add_memory
from memoryos_mcp.server import memoryos_get_billing_subscription
from memoryos_mcp.server import memoryos_get_context
from memoryos_mcp.server import memoryos_my_context
from memoryos_mcp.server import memoryos_my_memories
from memoryos_mcp.server import memoryos_remember
from memoryos_mcp.server import memoryos_session_context
from memoryos_mcp.server import memoryos_why_memory
from memoryos_mcp.server import memoryos_correct_memory
from memoryos_mcp.server import memoryos_forget_memory
from memoryos_mcp.server import memoryos_my_clarifications
from memoryos_mcp.server import memoryos_answer_clarification
from memoryos_mcp.server import memoryos_universal_add_memory
from memoryos_mcp.server import memoryos_api_request
from memoryos_mcp.server import memoryos_mcp_healthz
from memoryos_mcp.server import memoryos_update_memory


class MemoryOSToolRegistrationTests(unittest.TestCase):
    """Verify all expected tools are registered with the FastMCP app."""

    def _tool_names(self) -> set[str]:
        tools = asyncio.run(mcp._local_provider.list_tools())
        return {t.name for t in tools}

    def test_exposes_core_memory_tools(self) -> None:
        names = self._tool_names()
        self.assertIn("memoryos_add_memory", names)
        self.assertIn("memoryos_get_context", names)
        self.assertIn("memoryos_list_memories", names)
        self.assertIn("memoryos_my_context", names)
        self.assertIn("memoryos_my_memories", names)
        self.assertIn("memoryos_remember", names)
        self.assertIn("memoryos_session_context", names)
        self.assertIn("memoryos_why_memory", names)
        self.assertIn("memoryos_correct_memory", names)
        self.assertIn("memoryos_forget_memory", names)
        self.assertIn("memoryos_my_clarifications", names)
        self.assertIn("memoryos_answer_clarification", names)
        self.assertIn("memoryos_delete_memory", names)
        self.assertIn("memoryos_get_billing_subscription", names)
        self.assertIn("memoryos_api_request", names)

    def test_exposes_domain_and_universal_tools(self) -> None:
        names = self._tool_names()
        self.assertIn("memoryos_get_domain_schema", names)
        self.assertIn("memoryos_get_edtech_profile", names)
        self.assertIn("memoryos_set_support_type", names)
        self.assertIn("memoryos_get_support_stats", names)
        self.assertIn("memoryos_create_consent_url", names)
        self.assertIn("memoryos_universal_get_context", names)


class MemoryOSLocalLogicTests(unittest.TestCase):
    """Tests for tools that execute locally without hitting the API."""

    def test_consent_url_is_generated_locally(self) -> None:
        result = memoryos_create_consent_url(
            agent_id="agent_123",
            redirect_uri="https://example.com/callback",
            state="abc",
            consent_base_url="https://consent.example.com",
        )
        self.assertEqual(
            result,
            {
                "consent_url": "https://consent.example.com/consent?agent_id=agent_123&redirect_uri=https%3A%2F%2Fexample.com%2Fcallback&state=abc"
            },
        )

    def test_raw_request_blocks_non_memoryos_paths(self) -> None:
        with self.assertRaises(ValueError):
            memoryos_api_request(method="GET", path="https://example.com/private")

    def test_raw_request_requires_exact_route_boundary_and_safe_method(self) -> None:
        blocked_paths = (
            "/v1/memories-private",
            "/v1/memories/../internal/system-health",
            "/v1/memories?unsafe=query",
            "//api.memoryo.dev/v1/memories",
        )
        for path in blocked_paths:
            with self.subTest(path=path), self.assertRaises(ValueError):
                memoryos_api_request(method="GET", path=path)

        with self.assertRaises(ValueError):
            memoryos_api_request(method="TRACE", path="/v1/memories")

    def test_raw_universal_auth_cannot_be_used_outside_universal_routes(self) -> None:
        with self.assertRaises(ValueError):
            memoryos_api_request(
                method="GET",
                path="/v1/memories",
                use_universal_auth=True,
            )

        with self.assertRaises(ValueError):
            memoryos_api_request(
                method="GET",
                path="/v1/universal/memories/jobs/example",
            )

    def test_add_memory_forwards_idempotency_as_header_argument(self) -> None:
        with patch("memoryos_mcp.server._client.request", return_value={"status": "queued"}) as request:
            memoryos_add_memory(
                external_user_id="customer-123",
                messages=[{"role": "user", "content": "I prefer concise answers."}],
                idempotency_key="event-123",
            )

        kwargs = request.call_args.kwargs
        self.assertEqual(kwargs["idempotency_key"], "event-123")
        self.assertNotIn("idempotency_key", kwargs["json_body"])

    def test_get_context_forwards_timezone_aware_as_of(self) -> None:
        with patch("memoryos_mcp.server._client.request", return_value={"data": []}) as request:
            memoryos_get_context(
                external_user_id="customer-123",
                query="What was true then?",
                as_of="2026-08-01T12:00:00Z",
            )

            self.assertEqual(request.call_args.kwargs["json_body"]["as_of"], "2026-08-01T12:00:00Z")

    def test_self_scoped_tools_never_accept_external_user_id(self) -> None:
        with patch("memoryos_mcp.server._client.request", return_value={"data": []}) as request:
            memoryos_my_context(query="What do I prefer?")
            memoryos_my_memories(limit=3)
            memoryos_remember(messages=[{"role": "user", "content": "Remember this."}])
            memoryos_session_context()
            memoryos_why_memory("memory-1")
            memoryos_correct_memory("memory-1", "Corrected preference")
            memoryos_forget_memory("memory-1")
            memoryos_my_clarifications()
            memoryos_answer_clarification("clarification-1", "A")

        calls = request.call_args_list
        self.assertEqual(calls[0].args[:2], ("POST", "/v1/mcp/tenant/context"))
        self.assertEqual(calls[1].args[:2], ("GET", "/v1/mcp/tenant/memories"))
        self.assertEqual(calls[2].args[:2], ("POST", "/v1/mcp/tenant/remember"))
        self.assertEqual(calls[3].args[:2], ("POST", "/v1/mcp/tenant/session-context"))
        self.assertEqual(calls[3].kwargs["json_body"], {"context_max_tokens": 180})
        self.assertEqual(calls[4].args[:2], ("GET", "/v1/mcp/tenant/memories/memory-1/why"))
        self.assertEqual(calls[5].args[:2], ("POST", "/v1/mcp/tenant/memories/memory-1/correct"))
        self.assertEqual(calls[6].args[:2], ("DELETE", "/v1/mcp/tenant/memories/memory-1"))
        self.assertEqual(calls[7].args[:2], ("GET", "/v1/mcp/tenant/clarifications"))
        self.assertEqual(
            calls[8].args[:2],
            ("POST", "/v1/mcp/tenant/clarifications/clarification-1/answer"),
        )
        self.assertEqual(calls[8].kwargs["json_body"], {"answer": "A"})
        for call in calls:
            self.assertNotIn("external_user_id", call.kwargs.get("json_body", {}))
            self.assertNotIn("external_user_id", call.kwargs.get("params", {}))

    def test_public_generic_identity_tools_fail_closed(self) -> None:
        with patch.dict(os.environ, {"MEMORYOS_MCP_EXPOSURE": "public"}):
            with self.assertRaises(PermissionError):
                memoryos_add_memory(
                    external_user_id="someone-else",
                    messages=[{"role": "user", "content": "Not allowed publicly."}],
                )

    def test_billing_subscription_uses_authenticated_tenant_contract(self) -> None:
        with patch(
            "memoryos_mcp.server._client.request",
            return_value={"data": {"plan_tier": "free", "status": "free"}},
        ) as request:
            result = memoryos_get_billing_subscription()

        self.assertEqual(result["data"]["status"], "free")
        self.assertEqual(request.call_args.args, ("GET", "/v1/billing/subscription"))
        self.assertNotIn("api_key", request.call_args.kwargs)

    def test_universal_add_forwards_body_idempotency_key(self) -> None:
        with patch(
            "memoryos_mcp.server._client.universal_credentials",
            return_value=("agent-key", "uui-token"),
        ), patch("memoryos_mcp.server._client.request", return_value={"status": "queued"}) as request:
            memoryos_universal_add_memory(
                messages=[{"role": "user", "content": "Remember this."}],
                idempotency_key="universal-event-123",
            )

        self.assertEqual(
            request.call_args.kwargs["json_body"]["idempotency_key"],
            "universal-event-123",
        )

    def test_api_error_is_redacted(self) -> None:
        client = MemoryOSClient()
        self.addCleanup(client.close)
        client.client = httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    403,
                    json={
                        "error": "forbidden",
                        "code": "AUTH_403",
                        "request_id": "request-safe",
                        "content": "must-not-leak",
                    },
                )
            ),
            base_url="https://api.memoryo.dev",
        )

        with self.assertRaises(MemoryOSAPIError) as raised:
            client.request("GET", "/v1/memories")

        message = str(raised.exception)
        self.assertIn("AUTH_403", message)
        self.assertIn("request-safe", message)
        self.assertNotIn("must-not-leak", message)

    def test_public_mcp_forwards_verified_clerk_token_not_static_api_key(self) -> None:
        client = MemoryOSClient()
        self.addCleanup(client.close)
        seen_headers = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_headers.update(request.headers)
            return httpx.Response(200, json={"data": []})

        client.client = httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="https://api.memoryo.dev",
        )
        with patch.dict(
            os.environ,
            {"MEMORYOS_MCP_EXPOSURE": "public", "MEMORYOS_API_KEY": "must-not-be-used"},
            clear=True,
        ), patch(
            "fastmcp.server.dependencies.get_access_token",
            return_value=SimpleNamespace(token="clerk-access-token"),
        ):
            client.request("POST", "/v1/mcp/tenant/session-context", json_body={})

        self.assertEqual(seen_headers["authorization"], "Bearer clerk-access-token")
        self.assertEqual(seen_headers["x-memoryos-mcp-client"], "public-v1")

    def test_public_mcp_refuses_raw_and_universal_routes(self) -> None:
        client = MemoryOSClient()
        self.addCleanup(client.close)
        with patch.dict(
            os.environ,
            {"MEMORYOS_MCP_EXPOSURE": "public"},
            clear=True,
        ), patch(
            "fastmcp.server.dependencies.get_access_token",
            return_value=SimpleNamespace(token="clerk-access-token"),
        ):
            with self.assertRaisesRegex(PermissionError, "unavailable through public"):
                client.request("PATCH", "/v1/memories/example", json_body={"content": "no"})
            with self.assertRaisesRegex(PermissionError, "unavailable through public"):
                client.request("POST", "/v1/universal/memories/add", api_key="agent-key")
            with self.assertRaisesRegex(PermissionError, "does not accept caller-supplied credentials"):
                client.universal_credentials("agent-key", "uui-token")

    def test_public_mcp_rejects_legacy_generic_memory_routes(self) -> None:
        client = MemoryOSClient()
        self.addCleanup(client.close)
        with patch.dict(
            os.environ,
            {"MEMORYOS_MCP_EXPOSURE": "public"},
            clear=True,
        ), patch(
            "fastmcp.server.dependencies.get_access_token",
            return_value=SimpleNamespace(token="clerk-access-token"),
        ):
            with self.assertRaisesRegex(PermissionError, "unavailable through public"):
                client.request("POST", "/v1/memories/retrieve", json_body={"query": "hello"})

    def test_public_clarification_route_allows_only_one_answer_target(self) -> None:
        self.assertTrue(_public_tenant_request_allowed("GET", "/v1/mcp/tenant/clarifications"))
        self.assertTrue(
            _public_tenant_request_allowed(
                "POST",
                "/v1/mcp/tenant/clarifications/clarification-1/answer",
            )
        )
        self.assertFalse(
            _public_tenant_request_allowed(
                "POST",
                "/v1/mcp/tenant/clarifications/one/two/answer",
            )
        )
        self.assertFalse(
            _public_tenant_request_allowed(
                "GET",
                "/v1/mcp/tenant/clarifications/clarification-1/answer",
            )
        )

    def test_public_universal_call_exchanges_clerk_identity_for_capability(self) -> None:
        client = MemoryOSClient()
        self.addCleanup(client.close)
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path == "/v1/mcp/universal/capability":
                return httpx.Response(200, json={"data": {"capability": "short-lived-capability"}})
            return httpx.Response(200, json={"status": "queued"})

        client.client = httpx.Client(
            transport=httpx.MockTransport(handler), base_url="https://api.memoryo.dev"
        )
        with patch.dict(
            os.environ,
            {
                "MEMORYOS_MCP_EXPOSURE": "public",
                "MEMORYOS_MCP_UNIVERSAL_ENABLED": "true",
                "MEMORYOS_MCP_UNIVERSAL_AGENT_API_KEY": "server-only-agent-key",
            },
            clear=True,
        ), patch(
            "fastmcp.server.dependencies.get_access_token",
            return_value=SimpleNamespace(token="clerk-access-token"),
        ), patch("memoryos_mcp.server._client", client):
            result = memoryos_universal_add_memory(
                messages=[{"role": "user", "content": "Remember this."}]
            )

        self.assertEqual(result["status"], "queued")
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0].headers["authorization"], "Bearer clerk-access-token")
        self.assertEqual(seen[0].headers["x-memoryos-mcp-client"], "public-v1")
        self.assertEqual(seen[0].headers["x-memoryos-mcp-universal-agent-key"], "server-only-agent-key")
        self.assertEqual(seen[1].headers["authorization"], "Bearer short-lived-capability")
        self.assertNotIn("x-memoryos-mcp-client", seen[1].headers)


class MemoryOSHttpTransportTests(unittest.TestCase):
    """Verify HTTP transport remains private-by-default and observable."""

    def test_default_transport_is_stdio(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_http_server_config(), {"transport": "stdio"})

    def test_http_transport_configuration_is_forwarded_to_fastmcp(self) -> None:
        environment = {
            "MEMORYOS_MCP_TRANSPORT": "streamable-http",
            "MEMORYOS_MCP_HOST": "127.0.0.1",
            "MEMORYOS_MCP_PORT": "9080",
            "MEMORYOS_MCP_HTTP_PATH": "/private-mcp",
        }
        with patch.dict(os.environ, environment, clear=True), patch.object(mcp, "run") as run:
            main()

        run.assert_called_once_with(
            show_banner=False,
            transport="streamable-http",
            host="127.0.0.1",
            port=9080,
            path="/private-mcp",
        )

    def test_invalid_http_configuration_is_rejected(self) -> None:
        with patch.dict(os.environ, {"MEMORYOS_MCP_TRANSPORT": "public"}, clear=True):
            with self.assertRaises(ValueError):
                _http_server_config()

    def test_public_http_refuses_anonymous_mode(self) -> None:
        with patch.dict(
            os.environ,
            {
                "MEMORYOS_MCP_TRANSPORT": "streamable-http",
                "MEMORYOS_MCP_EXPOSURE": "public",
                "MEMORYOS_MCP_AUTH_MODE": "none",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "requires MEMORYOS_MCP_AUTH_MODE=clerk"):
                _http_server_config()

    def test_public_http_refuses_shared_static_credentials(self) -> None:
        with patch.dict(
            os.environ,
            {
                "MEMORYOS_MCP_TRANSPORT": "streamable-http",
                "MEMORYOS_MCP_EXPOSURE": "public",
                "MEMORYOS_MCP_AUTH_MODE": "clerk",
                "MEMORYOS_API_KEY": "mem_live_not_allowed_here",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "must not be configured with shared API credentials"):
                _http_server_config()

    def test_clerk_auth_uses_dedicated_mcp_oauth_configuration(self) -> None:
        environment = {
            "MEMORYOS_MCP_AUTH_MODE": "clerk",
            "MEMORYOS_MCP_PUBLIC_BASE_URL": "https://mcp.example.com",
            "MEMORYOS_MCP_CLERK_DOMAIN": "https://clerk.example.com",
            "MEMORYOS_MCP_CLERK_CLIENT_ID": "mcp-client-id",
            "MEMORYOS_MCP_CLERK_CLIENT_SECRET": "mcp-client-secret",
            "MEMORYOS_MCP_AUTH_SIGNING_KEY": "dedicated-signing-key",
            "MEMORYOS_MCP_ALLOWED_CLIENT_REDIRECT_URIS": "https://client.example/callback, https://other.example/callback",
        }
        with patch.dict(os.environ, environment, clear=True), patch(
            "fastmcp.server.auth.providers.clerk.ClerkProvider"
        ) as provider:
            _build_mcp_auth()

        provider.assert_called_once_with(
            domain="clerk.example.com",
            client_id="mcp-client-id",
            client_secret="mcp-client-secret",
            base_url="https://mcp.example.com",
            resource_base_url="https://mcp.example.com",
            jwt_signing_key="dedicated-signing-key",
            allowed_client_redirect_uris=[
                "https://client.example/callback",
                "https://other.example/callback",
            ],
            required_scopes=["openid", "email", "profile", "user:org:read"],
            valid_scopes=["openid", "email", "profile", "user:org:read"],
        )

        with patch.dict(
            os.environ,
            {
                "MEMORYOS_MCP_TRANSPORT": "streamable-http",
                "MEMORYOS_MCP_HTTP_PATH": "/mcp?unsafe=true",
            },
            clear=True,
        ):
            with self.assertRaises(ValueError):
                _http_server_config()

    def test_health_endpoint_does_not_expose_configuration(self) -> None:
        response = asyncio.run(memoryos_mcp_healthz(None))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            json.loads(response.body),
            {"status": "ok", "server": "memoryos-mcp", "version": "0.4.0"},
        )


class MemoryOSSingleSourceVersionTests(unittest.TestCase):
    """Version must come from one source so reporting never drifts."""

    def test_server_version_tracks_package_version(self) -> None:
        import memoryos_mcp
        from memoryos_mcp.server import SERVER_VERSION

        self.assertEqual(SERVER_VERSION, memoryos_mcp.__version__)
        self.assertEqual(SERVER_VERSION, "0.4.0")

    def test_health_version_matches_package_version(self) -> None:
        import memoryos_mcp

        health = json.loads(asyncio.run(memoryos_mcp_healthz(None)).body)
        self.assertEqual(health["version"], memoryos_mcp.__version__)


class MemoryOSToolContractTests(unittest.TestCase):
    """Pin request contracts for tools not previously exercised."""

    def test_update_memory_only_forwards_populated_fields(self) -> None:
        with patch("memoryos_mcp.server._client.request", return_value={"ok": True}) as request:
            memoryos_update_memory("mem_1", content="edited", is_archived=True)

        self.assertEqual(
            request.call_args.kwargs["json_body"],
            {"content": "edited", "is_archived": True},
        )

    def test_update_memory_with_no_fields_sends_empty_body(self) -> None:
        from memoryos_mcp.server import memoryos_update_memory

        with patch("memoryos_mcp.server._client.request", return_value={"ok": True}) as request:
            memoryos_update_memory("mem_1")

        self.assertEqual(request.call_args.kwargs["json_body"], {})

    def test_list_memories_forwards_cursor_and_categories(self) -> None:
        from memoryos_mcp.server import memoryos_list_memories

        with patch("memoryos_mcp.server._client.request", return_value={"data": []}) as request:
            memoryos_list_memories(
                external_user_id="customer-123",
                cursor="next-page",
                limit=5,
                categories=["general", "work"],
            )

        self.assertEqual(
            request.call_args.kwargs["params"],
            {"external_user_id": "customer-123", "limit": 5, "cursor": "next-page", "categories": ["general", "work"]},
        )

    def test_set_support_type_forwards_configured_mode(self) -> None:
        from memoryos_mcp.server import memoryos_set_support_type

        with patch("memoryos_mcp.server._client.request", return_value={"ok": True}) as request:
            memoryos_set_support_type(
                support_type_mode="picklist",
                support_types_allowed=["bug", "question"],
            )

        self.assertEqual(request.call_args.args[:2], ("PATCH", "/v1/tenant/support-type"))
        self.assertEqual(
            request.call_args.kwargs["json_body"],
            {"support_type_mode": "picklist", "support_type": None, "support_types_allowed": ["bug", "question"]},
        )

    def test_set_domain_schema_sends_patch_json(self) -> None:
        from memoryos_mcp.server import memoryos_set_domain_schema

        with patch("memoryos_mcp.server._client.request", return_value={"ok": True}) as request:
            memoryos_set_domain_schema("edtech")

        self.assertEqual(request.call_args.args[:2], ("PATCH", "/v1/tenant/domain-schema"))
        self.assertEqual(request.call_args.kwargs["json_body"], {"domain_schema": "edtech"})

    def test_get_domain_schema_reads_tenant_setting(self) -> None:
        from memoryos_mcp.server import memoryos_get_domain_schema

        with patch("memoryos_mcp.server._client.request", return_value={"domain_schema": "support"}) as request:
            memoryos_get_domain_schema()

        self.assertEqual(request.call_args.args[:2], ("GET", "/v1/tenant/domain-schema"))

    def test_list_support_customers_forwards_cursor(self) -> None:
        from memoryos_mcp.server import memoryos_list_support_customers

        with patch("memoryos_mcp.server._client.request", return_value={"data": []}) as request:
            memoryos_list_support_customers(cursor="pg-2", limit=25)

        self.assertEqual(
            request.call_args.kwargs["params"],
            {"limit": 25, "cursor": "pg-2"},
        )


if __name__ == "__main__":
    unittest.main()
