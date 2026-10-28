# 토스증권 MCP

토스증권 공식 OpenAPI v1.2.24의 REST 작업 40개를 제공하는 로컬 STDIO MCP 서버입니다. Codex 등 STDIO MCP를 지원하는 클라이언트에서 사용할 수 있습니다. 토스증권의 공식 MCP 제품은 아닙니다.

## 설치

Windows 10/11 또는 macOS, Python 3.11 이상, 토스증권 WTS에서 발급한 Open API Client ID와 Client Secret이 필요합니다. Windows는 DPAPI, macOS는 명시적인 macOS Keychain 백엔드와 파일 잠금을 사용합니다. Codex에 등록하려면 `codex` CLI도 설치되어 있어야 합니다.

이 저장소를 복제하거나 소스를 내려받은 후, 저장소 폴더에서 PowerShell을 열고 실행합니다.

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
```

`requirements.txt`는 직접 의존성 목록이고 `requirements-lock.txt`는 플랫폼별 의존성을 구분한 고정 버전 목록입니다. 가상환경과 인증정보는 저장소에 포함하지 않습니다.

키를 아래 연결 절차로 저장한 후, 같은 폴더에서 Codex에 등록합니다. 실행 파일과 서버 경로는 현재 저장소의 절대 경로로 변환됩니다.

```powershell
$tossPythonPath = (Resolve-Path .\.venv\Scripts\python.exe).Path
$tossServerPath = (Resolve-Path .\server.py).Path
codex mcp add toss_securities -- $tossPythonPath -u $tossServerPath
```

다른 MCP 클라이언트에서는 명령에 `.venv\Scripts\python.exe`의 절대 경로를, 인수에 `-u`와 `server.py`의 절대 경로를 지정합니다. 작업 폴더와 관계없이 서버 폴더의 `openapi.json`과 `.local/`을 사용합니다.

### macOS 설치

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-lock.txt
codex mcp add toss-securities -- "$PWD/.venv/bin/python" -u "$PWD/server.py"
.venv/bin/python configure_web.py
```

출력된 로컬 URL에서 키를 입력하세요. macOS 키체인 접근 허용 창이 나오면 승인합니다. 인증정보와 토큰은 저장소 절대 경로에 따라 분리된 키체인 항목에 저장하며 평문 파일로 저장하지 않습니다. 키체인을 사용할 수 없으면 오류로 종료하며 다른 저장소로 대체하지 않습니다. 거래는 기본적으로 꺼져 있습니다. Mac의 `.local/`에는 잠금 파일만 생성됩니다.

## 연결

1. `연결설정.cmd`를 실행하고 발급받은 Client ID와 Client Secret을 입력합니다. 키를 채팅이나 GitHub에 올리지 마세요.
2. 토스증권 WTS → 설정 → Open API → 허용 IP 관리에서 이 PC가 사용하는 공인 IP를 등록합니다.
3. Codex의 설정 → MCP 서버에서 `toss_securities`를 다시 시작하거나 앱을 다시 시작합니다. 새 채팅에서 사용할 수 있습니다.
4. “토스증권 연결 상태 확인하고 계좌 목록 조회해 줘”라고 요청합니다.

기본 accountSeq는 비워 둘 수 있습니다. `get_accounts` 응답의 `accountSeq`를 계좌 관련 도구의 `account_seq`로 전달하면 됩니다. 기본값을 저장하려면 연결 설정을 다시 열고 키와 함께 입력합니다.

Windows 설정창이 표시되지 않는 환경에서는 `.\.venv\Scripts\python.exe .\configure_web.py`를 실행하고 출력된 로컬 URL을 브라우저에서 여세요. 이 설정 페이지는 127.0.0.1에서만 열리며 저장 후 종료됩니다. 저장하지 않으면 20분 뒤 종료됩니다.

## 기능

- 시세 5개: 현재가, 호가, 체결, 상·하한가, 캔들
- 종목·시장: 종목 정보와 전체 목록, 매수 유의사항, 투자자 매매·프로그램매매·공매도·신용·대차, 환율, 장 운영, 랭킹, 지수, 섹터
- 계좌: 계좌 목록, 보유 주식, 주문 내역, 매수 가능 금액, 매도 가능 수량, 수수료
- 거래 6개: 일반 주문 생성·정정·취소, 조건주문 생성·수정·취소
- `connection_status`: 비밀값을 출력하지 않는 로컬 설정 확인

실제 거래는 기본적으로 꺼져 있습니다. 연결 설정에서 ‘실제 주문 기능 사용’을 켜면 사용할 수 있습니다. 거래 도구는 사용자가 구체적인 주문을 요청한 경우 `confirm: true`가 필요합니다. 생성 주문에는 `body.clientOrderId`가 필요하며, 같은 주문을 재확인할 때 같은 값을 사용합니다. 토스의 멱등키 유효기간은 10분입니다. 네트워크 오류나 5xx 발생 시 접수 여부를 주문 내역으로 확인해야 합니다. 서버는 거래 요청을 자동 재시도하지 않습니다.

WebSocket 스트리밍은 이 버전에 포함하지 않았습니다. 현재가·호가·체결 조회는 REST를 사용합니다.

## 보관과 인증

Windows에서는 인증정보와 토큰을 `.local/`에 현재 사용자 DPAPI로 암호화하여 저장합니다. macOS에서는 네이티브 키체인에 저장합니다. `config.toml`과 소스 코드에는 키를 저장하지 않습니다. 토스는 client_id마다 유효한 토큰 하나만 허용하므로 여러 서버 프로세스가 파일 잠금과 암호화된 토큰 캐시를 공유합니다. 다른 프로그램이 같은 키로 새 토큰을 발급하면 `token-revoked` 오류가 날 수 있습니다. 해당 오류는 자동 토큰 재발급 없이 알려줍니다. 다른 연동과 키를 분리하거나, 다른 연동을 중지한 후 Windows에서는 `.local/token.dpapi`만 지우고 다시 호출하세요. macOS에서는 키체인 접근 앱에서 `toss-securities-mcp:`로 시작하는 해당 설치의 `token` 항목만 제거하세요.

환경변수 `TOSS_CLIENT_ID`, `TOSS_CLIENT_SECRET`, `TOSS_ACCOUNT_SEQ`, `TOSS_ENABLE_TRADING=true`로 로컬 설정을 덮어쓸 수도 있습니다. 기본 설치는 환경변수를 사용하지 않습니다. 네트워크는 공식 HTTPS 주소로만 전송하며 리다이렉트와 환경 프록시 자동 적용을 끕니다.

## 관리

```powershell
# 실제 인증·계좌 조회 확인: 성공 여부와 계좌 수만 출력
.\.venv\Scripts\python.exe .\server.py --check

# 모의 HTTP를 이용한 테스트
.\.venv\Scripts\python.exe -m unittest discover -s tests -v

# Codex 등록 확인 / 등록 해제
codex mcp get toss_securities
codex mcp remove toss_securities
```

폴더를 이동하면 Codex 등록 경로도 갱신해야 합니다. `codex mcp remove toss_securities` 후 새 폴더에서 설치 항목의 등록 명령을 다시 실행하세요. 다른 PC에서는 가상환경을 새로 만들고 의존성을 설치해야 합니다. `.local`은 다른 Windows 계정이나 PC로 옮겨도 복호화되지 않으므로 키를 다시 입력해야 합니다.

`.gitignore`는 `.local/`, 가상환경, `.env`와 DPAPI 파일, 로그를 제외합니다. 인증정보를 별도 파일이나 다른 이름으로 저장했다면 커밋하기 전에 직접 제외 여부를 확인하세요.

공식 문서: [개발자 가이드](https://developers.tossinvest.com/), [REST 명세](https://openapi.tossinvest.com/openapi-docs/latest/openapi.json), [인증·IP·사용 한도](https://openapi.tossinvest.com/openapi-docs/overview.md).

조회 오류 403은 우선 WTS의 허용 IP 등록을 확인하세요. API 401은 키·토큰 상태를 확인하세요. 조회 요청의 429는 Retry-After를 반영해 최대 두 번 재시도하며, 긴 대기는 오류로 반환합니다. 한 서버 프로세스에서 API 그룹별 요청 간격은 최소 1.05초입니다. 여러 프로세스·다른 앱의 합산 호출은 토스 측 한도를 따릅니다.
