"""STDIO MCP server. stdout is reserved exclusively for MCP JSON-RPC."""
import asyncio
import json
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types
from core import SafeError, TossService

service = TossService()
server = Server(
    "toss-securities", version="1.0.0",
    instructions=("토스증권 공식 REST API 연동입니다. connection_status로 설정 상태를 확인하세요. "
                  "키를 채팅이나 도구 인자로 받지 말고 로컬 configure.ps1에서 입력하게 하세요. "
                  "시세와 자산은 공식 응답 그대로 해석하고 원화/달러 및 조회 시각을 구분하세요. "
                  "실제 거래는 사용자가 종목·수량/금액·주문방식 등 구체적 내용을 명시한 경우만 호출하세요. "
                  "기본 거래는 꺼져 있으며 로컬 설정에서 켤 수 있습니다. 쓰기 작업은 자동 재시도하지 마세요. "
                  "주문이 실패하거나 연결이 끊기면 주문 내역을 먼저 확인하세요. "
                  "clientOrderId는 주문별로 고유하게 만들고 동일 주문 재확인에는 같은 키를 사용하세요. "
                  "get_accounts로 계좌를 조회하고 accountSeq를 account_seq로 전달하세요. "
                  "한 client_id에는 토큰 하나만 유효하므로 다른 연동과 키를 공유하면 토큰이 폐기될 수 있습니다.")
)


@server.list_tools()
async def list_tools():
    tools = [types.Tool(
        name="connection_status", description="로컬 설정 상태 조회. 네트워크 요청과 비밀값 출력 없음.",
        inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
        annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                         idempotentHint=True, openWorldHint=False))]
    for name, op in service.operations.items():
        read_only = op["method"] == "GET"
        tools.append(types.Tool(
            name=name, description=op["description"], inputSchema=op["schema"],
            annotations=types.ToolAnnotations(readOnlyHint=read_only, destructiveHint=not read_only,
                                             idempotentHint=read_only, openWorldHint=True)))
    return tools


@server.call_tool()
async def call_tool(name, arguments):
    try:
        result = service.status() if name == "connection_status" else await service.call(name, arguments)
        return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
                                    structuredContent=result if isinstance(result, dict) else {"result": result})
    except SafeError as error:
        return types.CallToolResult(isError=True, content=[types.TextContent(type="text", text=str(error))])
    except Exception:
        return types.CallToolResult(isError=True, content=[types.TextContent(
            type="text", text="로컬 연동 처리 오류입니다. 설정을 확인하세요. 비밀값은 출력하지 않습니다.")])


async def main():
    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        await service.close()


if __name__ == "__main__":
    if "--check" in sys.argv:
        sys.stdout.reconfigure(encoding="utf-8")
        async def check():
            try:
                status = service.status()
                if status["credentials_configured"]:
                    response = await service.call("get_accounts", {})
                    result = response.get("result", [])
                    status["network_checked"] = True
                    status["account_count"] = len(result) if isinstance(result, list) else None
                print(json.dumps(status, ensure_ascii=False))
            except SafeError as error:
                print(str(error))
                sys.exit(1)
            finally:
                await service.close()
        asyncio.run(check())
    else:
        asyncio.run(main())
