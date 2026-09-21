# Automatic Trader v0.1

토스증권 주문 API를 호출하지 않는 `PAPER` 전용 MVP입니다. 현재가를 직접 입력하면 가상계좌에서 체결을 계산하고, 이동평균 교차 전략이 신호를 생성합니다.

## 실행

```powershell
python -m pip install -r requirements.txt
python -m auto_trader
```

서버가 `http://127.0.0.1:8000`에서 실행됩니다. 개발용 API 문서는 `http://127.0.0.1:8000/docs`에서 확인할 수 있습니다.

환경 변수:

- `PAPER_INITIAL_CASH`: 가상 시작 자금, 기본 `1000000`
- `PAPER_FEE_RATE`: 가상 수수료율, 기본 `0.00015`
- `PAPER_SLIPPAGE_RATE`: 시장가 슬리피지율, 기본 `0`

## 빠른 사용 예

전략을 만든 뒤 가격을 여러 번 입력하면 이동평균 교차 시 가상 주문이 생성됩니다.

```powershell
$strategy = @{ name = "demo-cross"; symbol = "TEST"; short_window = 2; long_window = 3; order_quantity = "1" } | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:8000/api/v1/strategies -Method Post -ContentType "application/json" -Body $strategy

$tick = @{ symbol = "TEST"; price = "100"; bid = "99"; ask = "101" } | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:8000/api/v1/market/ticks -Method Post -ContentType "application/json" -Body $tick
```

주문·체결·잔고 조회:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/v1/account
Invoke-RestMethod http://127.0.0.1:8000/api/v1/positions
Invoke-RestMethod http://127.0.0.1:8000/api/v1/orders
```

`/api/v1/controls/pause`, `/resume`, `/kill-switch`로 가상매매 실행을 제어할 수 있습니다. 버전 0.1에는 실제 계좌·토스증권 주문 API 연결이 없습니다.
