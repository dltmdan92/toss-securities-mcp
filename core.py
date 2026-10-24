"""Toss Securities REST adapter generated from the bundled official OpenAPI."""
import asyncio
import copy
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import random
import re
import time
from urllib.parse import quote

import httpx
from jsonschema import Draft202012Validator
from vault import Vault

BASE_URL = "https://openapi.tossinvest.com"
ROOT = Path(__file__).resolve().parent


class SafeError(Exception):
    """A sanitized error that can safely be returned to MCP clients."""


def resolve(value, spec, seen=()):
    if isinstance(value, list):
        return [resolve(item, spec, seen) for item in value]
    if not isinstance(value, dict):
        return value
    if "$ref" in value:
        ref = value["$ref"]
        if not ref.startswith("#/") or ref in seen:
            raise ValueError("Only acyclic, local OpenAPI references are supported")
        node = spec
        for part in ref[2:].split("/"):
            node = node[part.replace("~1", "/").replace("~0", "~")]
        combined = {**node, **{k: v for k, v in value.items() if k != "$ref"}}
        return resolve(combined, spec, (*seen, ref))
    result = {key: resolve(item, spec, seen) for key, item in value.items()}
    if result.pop("nullable", False):
        return {"anyOf": [result, {"type": "null"}]}
    return result


def snake(name):
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def build_operations(spec):
    operations = {}
    for path, item in spec["paths"].items():
        if not path.startswith("/api/v1/"):
            continue
        for method, operation in item.items():
            if method not in {"get", "post", "delete"}:
                continue
            properties, required, params = {}, [], []
            account = False
            for raw in [*item.get("parameters", []), *operation.get("parameters", [])]:
                parameter = resolve(raw, spec)
                if parameter["in"] == "header":
                    if parameter["name"].lower() != "x-tossinvest-account":
                        raise ValueError("Unexpected header parameter in specification")
                    account = True
                    properties["account_seq"] = {
                        "type": "integer", "minimum": 1,
                        "description": "GET get_accounts의 accountSeq. 생략하면 로컬 기본 계좌 사용."
                    }
                else:
                    name = parameter["name"]
                    properties[name] = {**parameter["schema"],
                                        "description": parameter.get("description", "")}
                    if parameter.get("required"):
                        required.append(name)
                    params.append(parameter)
            body_spec = operation.get("requestBody")
            if body_spec:
                body_spec = resolve(body_spec, spec)
                properties["body"] = body_spec["content"]["application/json"]["schema"]
                if body_spec.get("required"):
                    required.append("body")
            if method != "get":
                properties["confirm"] = {
                    "type": "boolean", "const": True,
                    "description": "사용자가 구체적인 주문/정정/취소를 명시적으로 요청한 경우에만 true. 실제 거래입니다."
                }
                required.append("confirm")
            schema = {"type": "object", "properties": properties,
                      "required": required, "additionalProperties": False}
            Draft202012Validator.check_schema(schema)
            description = operation.get("summary", "") + "\n" + operation.get("description", "")
            if method != "get":
                description += "\n실제 거래 작업. 로컬 설정에서 거래를 켜고 사용자 주문 확인 후 호출. 자동 재시도 없음."
            operations[snake(operation["operationId"])] = {
                "method": method.upper(), "path": path, "schema": schema,
                "account": account, "parameters": params, "description": description,
                "body": bool(body_spec),
                "group": (re.findall(r"Rate Limits Group\*\*: `([^`]+)`", description) or ["DEFAULT"])[0]
            }
    return operations


class TossService:
    def __init__(self, spec_path=ROOT / "openapi.json", store=None, transport=None):
        self.spec = json.loads(Path(spec_path).read_text("utf-8-sig"))
        self.operations = build_operations(self.spec)
        self.store = store if store is not None else Vault(ROOT / ".local")
        self.http = httpx.AsyncClient(base_url=BASE_URL, timeout=20, follow_redirects=False,
                                      trust_env=False, transport=transport)
        self.request_lock = asyncio.Lock()
        self.last_request = {}
        self.redactions = set()

    async def close(self):
        await self.http.aclose()

    def settings(self):
        try:
            stored = self.store.read("credentials") or {}
        except RuntimeError as error:
            raise SafeError(str(error)) from None
        result = dict(stored)
        for env_name, key in [("TOSS_CLIENT_ID", "client_id"), ("TOSS_CLIENT_SECRET", "client_secret"),
                              ("TOSS_ACCOUNT_SEQ", "account_seq")]:
            if os.environ.get(env_name):
                result[key] = os.environ[env_name]
        if "TOSS_ENABLE_TRADING" in os.environ:
            result["enable_trading"] = os.environ["TOSS_ENABLE_TRADING"].lower() == "true"
        self.redactions.update(str(result[k]) for k in ["client_id", "client_secret"] if result.get(k))
        return result

    def clean(self, data):
        if isinstance(data, dict):
            return {k: ("[REDACTED]" if k.lower() in {"access_token", "client_secret", "authorization", "client_id"}
                        else self.clean(v)) for k, v in data.items()}
        if isinstance(data, list):
            return [self.clean(v) for v in data]
        if isinstance(data, str):
            for secret in sorted(self.redactions, key=len, reverse=True):
                data = data.replace(secret, "[REDACTED]")
        return data

    def status(self):
        settings = self.settings()
        return {"server": "toss-securities", "api_base": BASE_URL,
                "spec_version": self.spec["info"]["version"],
                "credentials_configured": bool(settings.get("client_id") and settings.get("client_secret")),
                "default_account_configured": bool(settings.get("account_seq")),
                "trading_enabled": settings.get("enable_trading") is True,
                "rest_tools": len(self.operations), "network_checked": False,
                "setup": "configure.ps1에서 키를 저장하고 WTS 설정 > Open API에서 현재 공인 IP를 허용하세요."}

    def failure(self, response, write=False):
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        if isinstance(error, str):
            error = {"code": error, "message": payload.get("error_description", "")}
        code = error.get("code", "http-error")
        message = error.get("message", "토스 API 요청 실패")
        hint = ""
        if response.status_code == 403:
            hint = " WTS 설정 > Open API > 허용 IP 관리에서 이 PC의 공인 IP를 확인하세요."
        elif code == "token-revoked":
            hint = " 다른 연동에서 같은 client_id의 토큰을 발급하면 기존 토큰이 폐기됩니다."
        elif response.status_code == 401:
            hint = " 로컬 설정의 API 키와 만료 여부를 확인하세요."
        if write and response.status_code >= 500:
            hint += " 주문 접수 여부가 불명확합니다. 주문 내역을 조회한 뒤 판단하세요. 자동 재시도하지 않습니다."
        details = {"status": response.status_code, "code": code, "message": message + hint,
                   "request_id": response.headers.get("X-Request-Id")}
        raise SafeError(json.dumps(self.clean(details), ensure_ascii=False))

    async def token(self, settings, force=False):
        if not settings.get("client_id") or not settings.get("client_secret"):
            raise SafeError("API 키가 없습니다. configure.ps1에서 client_id와 client_secret을 입력하세요.")
        fingerprint = hashlib.sha256((settings["client_id"] + "\0" + settings["client_secret"]).encode()).hexdigest()
        try:
            async with self.store.lock("token"):
                cached = self.store.read("token") or {}
                if not force and cached.get("fingerprint") == fingerprint and cached.get("expires_at", 0) > time.time() + 60:
                    self.redactions.add(cached["access_token"])
                    return cached["access_token"]
                response = await self.http.post("/oauth2/token", data={
                    "grant_type": "client_credentials", "client_id": settings["client_id"],
                    "client_secret": settings["client_secret"]})
                if response.status_code != 200:
                    self.failure(response)
                result = response.json()
                access_token = result["access_token"]
                self.redactions.add(access_token)
                self.store.write("token", {"fingerprint": fingerprint, "access_token": access_token,
                                          "expires_at": time.time() + int(result["expires_in"])})
                return access_token
        except (RuntimeError, KeyError, ValueError):
            raise SafeError("인증 응답 또는 로컬 토큰 저장을 처리할 수 없습니다. 키 설정을 확인하세요.") from None

    async def call(self, name, arguments):
        if name not in self.operations:
            raise SafeError("알 수 없는 도구입니다.")
        operation = self.operations[name]
        args = copy.deepcopy(arguments)
        def contains_control(value):
            if isinstance(value, str):
                return any(ord(char) < 32 or ord(char) == 127 for char in value)
            if isinstance(value, dict):
                return any(contains_control(k) or contains_control(v) for k, v in value.items())
            if isinstance(value, list):
                return any(contains_control(v) for v in value)
            return False
        if contains_control(args):
            raise SafeError("입력값에 제어 문자를 사용할 수 없습니다.")
        errors = list(Draft202012Validator(
            operation["schema"], format_checker=Draft202012Validator.FORMAT_CHECKER
        ).iter_errors(args))
        if errors:
            error = errors[0]
            field = ".".join(map(str, error.absolute_path)) or "arguments"
            raise SafeError(f"입력값 오류: {field} ({error.validator}). 도구 스키마를 확인하세요.")
        settings = self.settings()
        write = operation["method"] != "GET"
        if write and settings.get("enable_trading") is not True:
            raise SafeError("실제 거래 기능이 꺼져 있습니다. configure.ps1에서 거래 허용을 켜세요.")
        headers, query, path = {}, [], operation["path"]
        if operation["account"]:
            account = args.get("account_seq", settings.get("account_seq"))
            if isinstance(account, bool) or not re.fullmatch(r"[1-9][0-9]*", str(account or "")):
                raise SafeError("계좌가 필요합니다. get_accounts로 accountSeq를 조회한 뒤 account_seq로 전달하세요.")
            headers["X-Tossinvest-Account"] = str(account)
        for parameter in operation["parameters"]:
            key = parameter["name"]
            if key not in args:
                continue
            value = args[key]
            if parameter["in"] == "path":
                # Reject traversal after percent decoding at any intermediary.
                if not re.fullmatch(r"[A-Za-z0-9._-]+", str(value)) or str(value) in {".", ".."}:
                    raise SafeError("경로 식별자 형식이 올바르지 않습니다.")
                path = path.replace("{" + key + "}", quote(str(value), safe=""))
            else:
                if isinstance(value, list):
                    if parameter.get("explode", True):
                        query.extend((key, str(v)) for v in value)
                        continue
                    value = ",".join(map(str, value))
                elif isinstance(value, bool):
                    value = str(value).lower()
                query.append((key, str(value)))
        body = args.get("body")
        if name == "create_order":
            if ("quantity" in body) == ("orderAmount" in body):
                raise SafeError("quantity와 orderAmount 중 하나만 지정하세요.")
            if body["orderType"] == "LIMIT" and "price" not in body:
                raise SafeError("지정가 주문에는 price가 필요합니다.")
            if body["orderType"] == "MARKET" and "price" in body:
                raise SafeError("시장가 주문에는 price를 지정할 수 없습니다.")
            if any(Decimal(body[key]) <= 0 for key in ("quantity", "orderAmount", "price") if key in body):
                raise SafeError("주문 수량·금액·가격은 0보다 커야 합니다.")
        if name in {"create_order", "create_conditional_order"}:
            if not body.get("clientOrderId"):
                raise SafeError("주문 중복 방지를 위해 body.clientOrderId를 지정하세요. 재확인 시 같은 키를 유지하세요.")
        async with self.request_lock:
            try:
                token = await self.token(settings)
                headers["Authorization"] = f"Bearer {token}"
                group = operation["group"]
                delay = self.last_request.get(group, 0) + 1.05 - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                for attempt in range(3):
                    self.last_request[group] = time.monotonic()
                    request_kwargs = {"params": query, "headers": headers}
                    if operation["body"]:
                        request_kwargs["json"] = body
                    response = await self.http.request(operation["method"], path, **request_kwargs)
                    if not write and attempt == 0 and response.status_code == 401:
                        try:
                            code = response.json().get("error", {}).get("code")
                        except (ValueError, AttributeError):
                            code = None
                        if code == "expired-token":
                            headers["Authorization"] = f"Bearer {await self.token(settings, force=True)}"
                            continue
                    if not write and response.status_code == 429 and attempt < 2:
                        try:
                            delay = float(response.headers.get("Retry-After", 2 ** attempt))
                        except ValueError:
                            delay = 2 ** attempt
                        if delay > 10:
                            self.failure(response)
                        await asyncio.sleep(max(delay, 0.1) + random.uniform(0, 0.2))
                        continue
                    if not 200 <= response.status_code < 300:
                        self.failure(response, write)
                    return self.clean(response.json()) if response.content else {"ok": True}
            except httpx.RequestError:
                suffix = " 주문 접수 여부가 불명확하므로 주문 내역을 확인하세요. 자동 재시도하지 않습니다." if write else " 잠시 후 조회를 다시 시도하세요."
                raise SafeError("토스 API 네트워크 연결 실패." + suffix) from None
            except (ValueError, KeyError):
                raise SafeError("토스 API 응답 형식을 해석할 수 없습니다.") from None
