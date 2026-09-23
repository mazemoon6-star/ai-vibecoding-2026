# Automatic Trader v0.2

토스증권 주문 API를 호출하지 않는 `PAPER` 자동매매 시스템입니다. 2단계에서는 토스 현재가를 읽기 전용으로 가져와 PAPER 엔진과 대시보드에 반영할 수 있습니다.

## 실행

```powershell
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python -m auto_trader
```

- API 문서: `http://127.0.0.1:8000/docs`
- 운영 대시보드: `http://127.0.0.1:8000/dashboard`

## 2단계 기능

- 계좌·포지션·주문·전략을 확인하는 대시보드
- `POST /api/v1/market/sync`를 통한 토스 `GET /api/v1/prices` 읽기 전용 연동
- `GET /api/v1/market/related-stocks?keyword=냉각&market=KR` 산업 연관 종목 검색
- 같은 검색창에서 회사명·별칭·티커를 검색하면 개별 종목을 직접 조회합니다. 예: `애플`, `AAPL`, `KB금융`, `105560`
- 최근 시세의 `담기` 버튼으로 자동매매 종목을 고르고 `거래 재개` 시 종목별 이동평균 PAPER 전략 실행
- 보유 포지션의 종목을 눌러 수집된 틱을 1분 단위 종가 차트로 확인
- 다중 이동평균 전략 등록과 실행
- 일시 정지·재개·긴급 중지 제어

산업 검색은 `auto_trader/related_stock_search.py`의 동의어·기술용어 확장과 회사별 사업·산업·제품·기술 프로필을 이용해 관련도를 계산합니다. 회사명 검색은 종목 별칭 색인을 사용하고, 티커 검색은 토스 종목·시세 API로 이름과 현재가를 확인합니다. 토스의 종목 API는 티커 기반 조회이므로 회사명 별칭 색인은 `DIRECT_STOCKS`에 관리하며, 새 별칭을 추가할 수 있습니다. 토스 조회가 불가능하면 저장된 PAPER 시세가 있는 경우에만 가격을 표시합니다.

검색 로직 테스트:

```powershell
python -m unittest discover -s tests -p test_related_stock_search.py
```

토스 연동은 `.env`에 Client ID와 Secret을 입력하고 `TOSS_MARKET_DATA_ENABLED=true`로 설정한 경우에만 호출됩니다. 현재 토스 주문 API는 구현하지 않았으며 모든 주문은 PAPER 엔진에서만 체결됩니다.

자동매매 흐름은 최근 시세의 종목을 목록에 담고 주문 수량을 정한 뒤 `거래 재개`를 누르는 방식입니다. 서버는 활성 종목 시세를 15초 간격으로 읽고, 백테스트로 선택한 고정 이동평균 창(단기 180·장기 460)의 교차를 평가해 PAPER 매매를 실행합니다. 시세 연동이 설정되지 않으면 자동 시세 수집과 자동매매가 작동하지 않습니다. 선택 종목과 전략은 로컬 PAPER 상태에 저장됩니다. 1분 차트도 수집한 시세를 바탕으로 생성됩니다.

대시보드의 `AI 매매 도우미`는 현재 PAPER 계좌·포지션·주문·전략 상태를 읽어 설명하는 읽기 전용 채팅입니다. `.env`에 `OPENAI_API_KEY`를 설정하면 활성화되며, API 키는 브라우저로 전송되지 않습니다. 모델은 `OPENAI_MODEL`로 바꿀 수 있습니다.

## 환경 변수

- `PAPER_INITIAL_CASH`: 가상 시작 자금, 기본 `1000000`
- `PAPER_FEE_RATE`: 가상 수수료율, 기본 `0.00015`
- `PAPER_SLIPPAGE_RATE`: 가상 슬리피지율, 기본 `0`
- `TOSS_MARKET_DATA_ENABLED`: 토스 읽기 전용 시세 연동, 기본 `false`
- `TOSS_API_TIMEOUT_SECONDS`: 토스 API 요청 제한 시간, 기본 `10`

## 수동 PAPER 예시

```powershell
$strategy = @{ name = "demo-cross"; symbol = "TEST"; short_window = 2; long_window = 3; order_quantity = "1" } | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:8000/api/v1/strategies -Method Post -ContentType "application/json" -Body $strategy

$tick = @{ symbol = "TEST"; price = "100"; bid = "99"; ask = "101" } | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:8000/api/v1/market/ticks -Method Post -ContentType "application/json" -Body $tick
```

실제 계좌와 주문 API는 2단계에서도 비활성화되어 있습니다. 실계좌 전환은 PRD의 PAPER 안정화 기준과 운영자 승인 이후 별도 단계로 진행합니다.
