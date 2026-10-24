"""Offline integration checks. Every credential and HTTP response is synthetic."""

from __future__ import annotations

import asyncio
import copy
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import SafeError, TossService


CLIENT_ID = "tsck_live_unit-test-client"
CLIENT_SECRET = "s_fake-secret-never-valid"
ACCESS_TOKEN = "unit-test-access-token-never-valid"
ORDER = {
    "clientOrderId": "offline-order-001",
    "symbol": "005930",
    "side": "BUY",
    "orderType": "LIMIT",
    "quantity": "1",
    "price": "70000",
}


class MemoryStore:
    def __init__(self, credentials=None):
        self.data = {
            "credentials": credentials if credentials is not None else {
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "account_seq": "7",
                "enable_trading": False,
            }
        }
        self.locks = {}

    def read(self, name):
        return copy.deepcopy(self.data.get(name))

    def write(self, name, data):
        self.data[name] = copy.deepcopy(data)

    @asynccontextmanager
    async def lock(self, name):
        lock = self.locks.setdefault(name, asyncio.Lock())
        async with lock:
            yield


class FakeAPI:
    def __init__(self, handler=None):
        self.requests = []
        self.handler = handler
        self.token_count = 0

    def __call__(self, request):
        self.requests.append(request)
        if request.url.path == "/oauth2/token":
            self.token_count += 1
            return httpx.Response(200, json={
                "access_token": ACCESS_TOKEN,
                "token_type": "Bearer",
                "expires_in": 86400,
            })
        if self.handler:
            return self.handler(request)
        return httpx.Response(200, json={"result": {"offline": True}})

    @property
    def api_requests(self):
        return [r for r in self.requests if r.url.path != "/oauth2/token"]


class CoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env_patch = patch.dict(os.environ, {}, clear=True)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.store = MemoryStore()
        self.api = FakeAPI()
        self.service = self.make_service()

    def make_service(self, api=None, store=None):
        service = TossService(
            ROOT / "openapi.json", store=store or self.store,
            transport=httpx.MockTransport(api or self.api),
        )
        self.addAsyncCleanup(service.close)
        return service

    async def test_all_40_documented_api_operations_and_no_raw_token_tool(self):
        self.assertEqual(len(self.service.operations), 40)
        self.assertNotIn("issue_o_auth2_token", self.service.operations)
        self.assertIn("get_prices", self.service.operations)
        self.assertIn("create_order", self.service.operations)
        self.assertEqual(self.api.requests, [])

    async def test_oauth_form_bearer_and_query_serialization(self):
        result = await self.service.call("get_prices", {"symbols": "005930,AAPL"})
        self.assertEqual(result, {"result": {"offline": True}})
        self.assertEqual(self.api.token_count, 1)
        token_request = self.api.requests[0]
        self.assertEqual(token_request.method, "POST")
        self.assertEqual(token_request.url.host, "openapi.tossinvest.com")
        self.assertTrue(token_request.headers["content-type"].startswith(
            "application/x-www-form-urlencoded"))
        self.assertEqual(parse_qs(token_request.content.decode()), {
            "grant_type": ["client_credentials"],
            "client_id": [CLIENT_ID],
            "client_secret": [CLIENT_SECRET],
        })
        self.assertNotIn("authorization", token_request.headers)
        request = self.api.api_requests[0]
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.url.path, "/api/v1/prices")
        self.assertEqual(request.url.params["symbols"], "005930,AAPL")
        self.assertEqual(request.headers["authorization"], f"Bearer {ACCESS_TOKEN}")
        self.assertNotIn("x-tossinvest-account", request.headers)

    async def test_default_account_is_header_only(self):
        await self.service.call("get_holdings", {"symbol": "AAPL"})
        request = self.api.api_requests[0]
        self.assertEqual(request.headers["x-tossinvest-account"], "7")
        self.assertEqual(dict(request.url.params), {"symbol": "AAPL"})
        self.assertEqual(request.content, b"")

    async def test_explicit_account_overrides_default(self):
        await self.service.call("get_holdings", {"account_seq": 9})
        request = self.api.api_requests[0]
        self.assertEqual(request.headers["x-tossinvest-account"], "9")
        self.assertEqual(str(request.url), "https://openapi.tossinvest.com/api/v1/holdings")

    async def test_account_input_cannot_inject_headers_or_use_bool(self):
        for account in [True, 0, -1, "9\r\nAuthorization: wrong"]:
            with self.subTest(account=account):
                with self.assertRaises(SafeError):
                    await self.service.call("get_holdings", {"account_seq": account})
                self.assertEqual(self.api.requests, [])

    async def test_missing_default_account_requires_selection_offline(self):
        self.store.data["credentials"].pop("account_seq")
        with self.assertRaises(SafeError):
            await self.service.call("get_holdings", {})
        self.assertEqual(self.api.requests, [])

    async def test_boolean_and_timezone_offset_query_encoding(self):
        before = "2026-03-25T09:00:00+09:00"
        await self.service.call("get_candles", {
            "symbol": "005930", "interval": "1m", "count": 2,
            "before": before, "adjusted": False,
        })
        request = self.api.api_requests[0]
        self.assertEqual(request.url.params["before"], before)
        self.assertIn("%2B09%3A00", str(request.url))
        self.assertEqual(request.url.params["adjusted"], "false")
        self.assertEqual(request.url.params["count"], "2")

    async def test_valid_opaque_order_id_stays_in_path(self):
        order_id = "0d5QIHjmtksbsmM-hBRAgP_ExI8iodGm9fA"
        await self.service.call("get_order", {"orderId": order_id})
        self.assertEqual(self.api.api_requests[0].url.path, f"/api/v1/orders/{order_id}")

    async def test_status_is_offline_and_has_no_secrets(self):
        status = self.service.status()
        self.assertTrue(status["credentials_configured"])
        self.assertTrue(status["default_account_configured"])
        self.assertFalse(status["trading_enabled"])
        self.assertFalse(status["network_checked"])
        self.assertEqual(status["rest_tools"], 40)
        self.assertNotIn(CLIENT_ID, json.dumps(status))
        self.assertNotIn(CLIENT_SECRET, json.dumps(status))
        self.assertEqual(self.api.requests, [])

    async def test_environment_settings_override_local_store(self):
        with patch.dict(os.environ, {
            "TOSS_CLIENT_ID": "fake-environment-client",
            "TOSS_CLIENT_SECRET": "fake-environment-secret",
            "TOSS_ACCOUNT_SEQ": "11",
            "TOSS_ENABLE_TRADING": "true",
        }):
            settings = self.service.settings()
            self.assertEqual(settings["client_id"], "fake-environment-client")
            self.assertEqual(settings["client_secret"], "fake-environment-secret")
            self.assertEqual(settings["account_seq"], "11")
            self.assertIs(settings["enable_trading"], True)
        self.assertEqual(self.api.requests, [])

    async def test_missing_credentials_do_not_connect(self):
        service = self.make_service(store=MemoryStore({}))
        with self.assertRaises(SafeError):
            await service.call("get_prices", {"symbols": "AAPL"})
        self.assertEqual(self.api.requests, [])

    async def test_bad_arguments_are_rejected_before_even_token_request(self):
        cases = [
            ("does_not_exist", {}),
            ("get_prices", {}),
            ("get_prices", {"symbols": "AAPL", "url": "https://example.invalid"}),
            ("get_prices", {"symbols": ["AAPL"]}),
            ("get_prices", {"symbols": "AAPL&client_secret=wrong"}),
            ("get_orderbook", {"symbol": "AAPL\r\nX-Test: bad"}),
            ("get_orders", {"status": "INVALID"}),
            ("get_orders", {"status": "OPEN", "limit": 101}),
            ("get_orders", {"status": "OPEN", "limit": True}),
            ("get_orders", {"status": "CLOSED", "from": "2026-02-30"}),
        ]
        for name, arguments in cases:
            with self.subTest(name=name, arguments=arguments):
                with self.assertRaises(SafeError):
                    await self.service.call(name, arguments)
                self.assertEqual(self.api.requests, [])

    async def test_path_and_parameter_injection_rejected_offline(self):
        cases = [
            ("get_stock_warnings", {"symbol": "../oauth2/token"}),
            ("get_stock_warnings", {"symbol": "AAPL?symbols=MSFT"}),
            ("get_stock_warnings", {"symbol": "AAPL\n"}),
            ("get_order", {"orderId": "../accounts"}),
            ("get_order", {"orderId": "%2e%2e%2faccounts"}),
            ("get_order", {"orderId": "x?accountSeq=9"}),
        ]
        for name, arguments in cases:
            with self.subTest(name=name, arguments=arguments):
                with self.assertRaises(SafeError):
                    await self.service.call(name, arguments)
                self.assertEqual(self.api.requests, [])

    async def test_token_reused_in_memory_and_across_service_instances(self):
        await self.service.call("get_prices", {"symbols": "AAPL"})
        await self.service.call("get_prices", {"symbols": "MSFT"})
        other = self.make_service()
        await other.call("get_prices", {"symbols": "005930"})
        self.assertEqual(self.api.token_count, 1)
        self.assertEqual(len(self.api.api_requests), 3)

    async def test_expired_cached_token_is_reissued(self):
        await self.service.call("get_prices", {"symbols": "AAPL"})
        self.store.data["token"]["expires_at"] = 0
        other = self.make_service()
        await other.call("get_prices", {"symbols": "MSFT"})
        self.assertEqual(self.api.token_count, 2)

    async def test_new_credentials_do_not_reuse_old_credentials_token(self):
        await self.service.call("get_prices", {"symbols": "AAPL"})
        self.store.data["credentials"]["client_secret"] = "s_fake-rotated-secret"
        other = self.make_service()
        await other.call("get_prices", {"symbols": "MSFT"})
        self.assertEqual(self.api.token_count, 2)
        token_requests = [r for r in self.api.requests if r.url.path == "/oauth2/token"]
        self.assertEqual(parse_qs(token_requests[-1].content.decode())["client_secret"],
                         ["s_fake-rotated-secret"])

    async def test_expired_token_response_refreshes_get_only_once(self):
        def handler(request):
            if len(self.api.api_requests) == 1:
                return httpx.Response(401, json={"error": {"code": "expired-token"}})
            return httpx.Response(200, json={"result": "refreshed"})
        self.api.handler = handler
        self.assertEqual(await self.service.call("get_prices", {"symbols": "AAPL"}),
                         {"result": "refreshed"})
        self.assertEqual(self.api.token_count, 2)
        self.assertEqual(len(self.api.api_requests), 2)

    async def test_revoked_token_does_not_start_token_reissue_war(self):
        self.api.handler = lambda request: httpx.Response(401, json={"error": {
            "code": "token-revoked", "message": "another client refreshed",
        }})
        with self.assertRaises(SafeError) as caught:
            await self.service.call("get_prices", {"symbols": "AAPL"})
        self.assertIn("token-revoked", str(caught.exception))
        self.assertEqual(self.api.token_count, 1)
        self.assertEqual(len(self.api.api_requests), 1)

    async def test_trading_default_off_is_enforced_before_auth(self):
        with self.assertRaises(SafeError):
            await self.service.call("create_order", {"body": ORDER, "confirm": True})
        self.assertEqual(self.api.requests, [])

    async def test_trade_confirmation_and_idempotency_key_required_offline(self):
        self.store.data["credentials"]["enable_trading"] = True
        without_id = {k: v for k, v in ORDER.items() if k != "clientOrderId"}
        for arguments in [{"body": ORDER}, {"body": ORDER, "confirm": False},
                          {"body": without_id, "confirm": True}]:
            with self.subTest(arguments=arguments):
                with self.assertRaises(SafeError):
                    await self.service.call("create_order", arguments)
                self.assertEqual(self.api.requests, [])

    async def test_order_json_decimal_strings_account_and_confirm_separation(self):
        self.store.data["credentials"]["enable_trading"] = True
        await self.service.call("create_order", {
            "account_seq": 8, "body": ORDER, "confirm": True,
        })
        request = self.api.api_requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.url.path, "/api/v1/orders")
        self.assertEqual(request.headers["x-tossinvest-account"], "8")
        self.assertEqual(request.headers["content-type"], "application/json")
        self.assertEqual(json.loads(request.content), ORDER)
        self.assertEqual(str(request.url), "https://openapi.tossinvest.com/api/v1/orders")
        self.assertIsInstance(json.loads(request.content)["price"], str)

    async def test_invalid_order_body_does_not_connect(self):
        self.store.data["credentials"]["enable_trading"] = True
        bodies = [
            {**ORDER, **change} for change in [
                {"quantity": 1}, {"quantity": "-1"}, {"quantity": "0"},
                {"side": "HOLD"}, {"orderAmount": "10"},
                {"clientOrderId": "bad/key"}, {"orderType": "MARKET"},
                {"price": "0"},
            ]
        ]
        bodies.extend([
            {k: v for k, v in ORDER.items() if k != "price"},
            {**{k: v for k, v in ORDER.items() if k not in {"quantity", "price"}},
             "orderType": "MARKET", "orderAmount": "0"},
        ])
        for body in bodies:
            with self.subTest(body=body):
                api = FakeAPI()
                service = self.make_service(api=api)
                with self.assertRaises(SafeError):
                    await service.call("create_order", {
                        "body": body, "confirm": True,
                    })
                self.assertEqual(api.requests, [])

    async def test_post_never_retries_on_api_failure(self):
        self.store.data["credentials"]["enable_trading"] = True
        for status in [429, 500, 401]:
            with self.subTest(status=status):
                api = FakeAPI(lambda request: httpx.Response(status, json={"error": {
                    "code": "expired-token" if status == 401 else "failed",
                }}))
                service = self.make_service(api=api, store=MemoryStore({
                    **self.store.data["credentials"],
                }))
                with self.assertRaises(SafeError):
                    await service.call("create_order", {"body": ORDER, "confirm": True})
                self.assertEqual(api.token_count, 1)
                self.assertEqual(len(api.api_requests), 1)

    async def test_post_transport_failure_is_not_retried(self):
        self.store.data["credentials"]["enable_trading"] = True
        def fail(request):
            raise httpx.ReadTimeout("synthetic timeout")
        self.api.handler = fail
        with self.assertRaises(SafeError) as caught:
            await self.service.call("create_order", {"body": ORDER, "confirm": True})
        self.assertEqual(len(self.api.api_requests), 1)
        self.assertIn("주문", str(caught.exception))

    async def test_cancel_order_preserves_empty_json_body(self):
        self.store.data["credentials"]["enable_trading"] = True
        await self.service.call("cancel_order", {
            "orderId": "synthetic-order-id", "body": {}, "confirm": True,
        })
        request = self.api.api_requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.url.path, "/api/v1/orders/synthetic-order-id/cancel")
        self.assertEqual(json.loads(request.content), {})

    async def test_delete_conditional_order_has_no_body_or_retry(self):
        self.store.data["credentials"]["enable_trading"] = True
        self.api.handler = lambda request: httpx.Response(429, json={})
        with self.assertRaises(SafeError):
            await self.service.call("cancel_conditional_order", {
                "conditionalOrderId": "synthetic-conditional-id", "confirm": True,
            })
        self.assertEqual(len(self.api.api_requests), 1)
        request = self.api.api_requests[0]
        self.assertEqual(request.method, "DELETE")
        self.assertEqual(request.url.path,
                         "/api/v1/conditional-orders/synthetic-conditional-id")
        self.assertEqual(request.content, b"")

    async def test_success_response_recursive_secret_redaction(self):
        self.api.handler = lambda request: httpx.Response(200, json={"result": {
            "access_token": "unexpected-server-token",
            "client_secret": CLIENT_SECRET,
            "nested": [f"echo {ACCESS_TOKEN} {CLIENT_SECRET}"],
        }})
        result = await self.service.call("get_prices", {"symbols": "AAPL"})
        text = json.dumps(result)
        self.assertNotIn(ACCESS_TOKEN, text)
        self.assertNotIn(CLIENT_SECRET, text)
        self.assertNotIn("unexpected-server-token", text)
        self.assertIn("[REDACTED]", text)

    async def test_get_429_retry_is_bounded(self):
        self.api.handler = lambda request: httpx.Response(
            429, headers={"Retry-After": "0"},
            json={"error": {"code": "too-many-requests", "message": "slow down"}},
        )
        with self.assertRaises(SafeError):
            await self.service.call("get_prices", {"symbols": "AAPL"})
        self.assertGreaterEqual(len(self.api.api_requests), 2)
        self.assertLessEqual(len(self.api.api_requests), 3)
        self.assertEqual(self.api.token_count, 1)

    async def test_get_429_can_recover(self):
        def handler(request):
            if len(self.api.api_requests) == 1:
                return httpx.Response(429, headers={"Retry-After": "0"}, json={})
            return httpx.Response(200, json={"result": "recovered"})
        self.api.handler = handler
        self.assertEqual(await self.service.call("get_prices", {"symbols": "AAPL"}),
                         {"result": "recovered"})
        self.assertEqual(len(self.api.api_requests), 2)

    async def test_errors_never_echo_server_secret_values(self):
        self.api.handler = lambda request: httpx.Response(400, json={"error": {
            "code": "invalid-request",
            "message": f"{CLIENT_SECRET} {ACCESS_TOKEN}",
            "data": {"client_secret": CLIENT_SECRET, "access_token": ACCESS_TOKEN},
        }})
        with self.assertRaises(SafeError) as caught:
            await self.service.call("get_prices", {"symbols": "AAPL"})
        text = str(caught.exception)
        self.assertNotIn(CLIENT_SECRET, text)
        self.assertNotIn(ACCESS_TOKEN, text)

    async def test_transport_errors_do_not_echo_request_details(self):
        def fail(request):
            raise httpx.ConnectError(f"failed with {CLIENT_SECRET} {ACCESS_TOKEN}")
        self.api.handler = fail
        with self.assertRaises(SafeError) as caught:
            await self.service.call("get_prices", {"symbols": "AAPL"})
        self.assertNotIn(CLIENT_SECRET, str(caught.exception))
        self.assertNotIn(ACCESS_TOKEN, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
