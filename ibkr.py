import asyncio
import concurrent.futures
import logging
import math
import threading
from datetime import datetime


def _is_real(x) -> bool:
    """True if x is a non-None, non-NaN numeric value."""
    return x is not None and not math.isnan(x)


_MONTH_ABBR = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def _human_symbol(c) -> str:
    """Build a readable symbol from an ib_insync ``Contract``.

    For options/futures we want a stable, human-readable identifier rather
    than the underlier alone (which collides across contracts) or the OCC
    string (which is unreadable).
    Examples:
      - Stock      "AAPL"            -> "AAPL"
      - OPT        symbol=NVDA, lastTradeDateOrContractMonth=20271217,
                   strike=110, right=P -> "NVDA 17DEC27 110 P"
      - FUT        symbol=M6E, lastTradeDateOrContractMonth=20260615 -> "M6E 15JUN26"
    """
    sec = getattr(c, "secType", "STK")
    if sec == "STK":
        return c.symbol
    expiry = getattr(c, "lastTradeDateOrContractMonth", "") or ""
    if len(expiry) >= 8:
        y, m, d = expiry[:4], expiry[4:6], expiry[6:8]
        try:
            mon = _MONTH_ABBR[int(m) - 1]
            tail = f"{int(d):02d}{mon}{y[2:]}"
        except (ValueError, IndexError):
            tail = expiry
    else:
        tail = expiry
    if sec == "OPT":
        right = getattr(c, "right", "") or ""
        strike = getattr(c, "strike", 0) or 0
        return f"{c.symbol} {tail} {strike:g} {right}".strip()
    return f"{c.symbol} {tail}".strip()


log = logging.getLogger(__name__)

_ib_available = True
try:
    from ib_insync import IB, Stock
except ImportError:
    _ib_available = False


class IBConnection:
    def __init__(self, host="127.0.0.1", port=7496, client_id=10):
        self.host = host
        self.port = port
        self.client_id = client_id
        self._ib = None
        self._loop = None
        self._thread = None
        self._connected = False
        self._close_prices = {}
        self._prices_fetched = False

    @property
    def connected(self):
        return self._connected

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ib = IB()
        try:
            self._loop.run_until_complete(
                self._ib.connectAsync(self.host, self.port, clientId=self.client_id)
            )
            self._connected = True
            log.info(f"Connected to TWS at {self.host}:{self.port}")
        except Exception as e:
            log.warning(f"TWS connect error: {e}")
            self._connected = False
            return
        self._ib.run()

    async def connect(self):
        if not _ib_available:
            log.warning("ib_insync not available")
            return False
        if self.connected:
            return True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        for _ in range(50):
            await asyncio.sleep(0.2)
            if self._connected:
                return True
        return False

    def disconnect(self):
        if self._ib and self._connected:
            self._call(self._ib.disconnect)
        self._connected = False
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)

    def _call(self, fn, *args, **kwargs):
        if not self._loop or not self._connected:
            return None
        fut = concurrent.futures.Future()

        async def _do():
            try:
                result = fn(*args, **kwargs)
                if asyncio.iscoroutine(result) or asyncio.isfuture(result):
                    result = await result
                fut.set_result(result)
            except Exception as e:
                fut.set_exception(e)

        self._loop.call_soon_threadsafe(asyncio.ensure_future, _do())
        try:
            return fut.result(timeout=30)
        except Exception as e:
            log.warning(f"IB call failed ({fn.__name__ if hasattr(fn, '__name__') else fn}): {e}")
            return None

    def fetch_close_prices(self):
        if not self.connected:
            return
        assert self._ib is not None
        positions = self._call(self._ib.positions)
        if not positions:
            return
        log.info(f"Fetching close prices for {len(positions)} positions...")
        for pos in positions:
            c = pos.contract
            sym = c.symbol
            if sym in self._close_prices:
                continue
            try:
                self._call(self._ib.qualifyContractsAsync, c)
                bars = self._call(
                    self._ib.reqHistoricalDataAsync,
                    c,
                    endDateTime="",
                    durationStr="2 D",
                    barSizeSetting="1 day",
                    whatToShow="TRADES",
                    useRTH=True,
                )
                if bars:
                    self._close_prices[sym] = bars[-1].close
                    log.info(f"  {sym}: {bars[-1].close}")
                else:
                    log.info(f"  {sym}: no data, using avg_cost")
            except Exception as e:
                log.warning(f"  {sym}: failed ({e})")
        self._prices_fetched = True
        log.info(f"Close prices fetched: {len(self._close_prices)}/{len(positions)}")

    def _enrich_position(
        self,
        sym,
        qty,
        avg_cost,
        currency,
        sec_type,
        account,
        live_price=None,
        live_mv=None,
        live_upnl=None,
        live_rpnl=None,
        multiplier=1,
    ):
        price = live_price
        has_live_price = _is_real(price) and price != 0
        if not has_live_price:
            price = self._close_prices.get(sym, 0.0)
            has_live_price = price is not None and price != 0
        if not has_live_price:
            price = avg_cost / multiplier if multiplier > 1 else avg_cost

        # IB's portfolio() returns marketValue/unrealizedPNL in the account
        # base currency (USD here), not the contract's local currency. Our
        # downstream pipeline assumes market_value/unrealized_pnl are in the
        # contract's local currency, so always compute locally from
        # qty × price (price comes from item.marketPrice, which is local).
        mv = round(qty * price * multiplier, 2)
        cost = qty * avg_cost
        upnl = round(mv - cost, 2)

        _ = live_mv  # IB-base-currency value, ignored — see comment above
        _ = live_upnl
        rpnl = live_rpnl if _is_real(live_rpnl) else 0.0
        return {
            "timestamp": datetime.now().isoformat(),
            "account": account,
            "symbol": sym,
            "sec_type": sec_type,
            "quantity": qty,
            "avg_cost": round(avg_cost, 4),
            "market_price": round(float(price or 0.0), 4),
            "market_value": round(mv, 2),
            "unrealized_pnl": round(float(upnl or 0.0), 2),
            "realized_pnl": round(float(rpnl or 0.0), 2),
            "currency": currency,
            "purchase_date": "2025-01-01",
        }

    def get_positions(self):
        if not self.connected:
            return []
        assert self._ib is not None
        positions = self._call(self._ib.positions)
        if not positions:
            return []
        return [
            self._enrich_position(
                pos.contract.symbol,
                float(pos.position),
                pos.avgCost,
                pos.contract.currency,
                pos.contract.secType,
                pos.account,
                multiplier=int(pos.contract.multiplier or 1),
            )
            for pos in positions
        ]

    def get_portfolio(self):
        if not self.connected:
            return []
        assert self._ib is not None
        items = self._call(self._ib.portfolio)
        if not items:
            return []
        return [
            self._enrich_position(
                item.contract.symbol,
                float(item.position),
                item.averageCost,
                item.contract.currency,
                item.contract.secType,
                item.account,
                live_price=item.marketPrice,
                live_mv=item.marketValue,
                live_upnl=item.unrealizedPNL,
                live_rpnl=item.realizedPNL,
                multiplier=int(item.contract.multiplier or 1),
            )
            for item in items
        ]

    def get_account_summary(self):
        if not self.connected:
            return {}
        assert self._ib is not None
        summary = self._call(self._ib.accountSummaryAsync)
        if not summary:
            return {}
        result = {}
        for item in summary:
            if item.tag in (
                "NetLiquidation",
                "TotalCashValue",
                "BuyingPower",
                "GrossPositionValue",
                "MaintMarginReq",
            ):
                result[item.tag] = {"value": float(item.value), "currency": item.currency}
        return result

    def get_fills(self):
        if not self.connected:
            return []
        assert self._ib is not None
        fills = self._call(self._ib.fills)
        if not fills:
            return []
        result = []
        for fill in fills:
            ex = fill.execution
            c = fill.contract
            comm = fill.commissionReport.commission if fill.commissionReport else 0
            side_mult = 1 if ex.side == "BOT" else -1
            result.append(
                {
                    "trade_date": str(ex.time),
                    "symbol": _human_symbol(c),
                    "description": c.localSymbol or c.symbol,
                    "asset_class": c.secType,
                    "action": "BUY" if ex.side == "BOT" else "SELL",
                    "quantity": float(ex.shares),
                    "price": float(ex.price),
                    "currency": c.currency,
                    "commission": float(comm),
                    "net_amount": round(float(ex.shares) * float(ex.price) * side_mult, 2),
                    "exchange": ex.exchange,
                    "order_type": "",
                    "account": ex.acctNumber,
                    "trade_id": ex.execId,
                }
            )
        return result

    def get_completed_orders(self):
        if not self.connected:
            return []
        assert self._ib is not None
        completed = self._call(self._ib.reqCompletedOrdersAsync, False)
        if not completed:
            return []
        result = []
        for trade in completed:
            order = trade.order
            c = trade.contract
            for fill in trade.fills:
                ex = fill.execution
                comm = fill.commissionReport.commission if fill.commissionReport else 0
                side_mult = 1 if ex.side == "BOT" else -1
                result.append(
                    {
                        "trade_date": str(ex.time),
                        "symbol": c.symbol,
                        "description": c.localSymbol or c.symbol,
                        "asset_class": c.secType,
                        "action": "BUY" if ex.side == "BOT" else "SELL",
                        "quantity": float(ex.shares),
                        "price": float(ex.price),
                        "currency": c.currency,
                        "commission": float(comm),
                        "net_amount": round(float(ex.shares) * float(ex.price) * side_mult, 2),
                        "exchange": ex.exchange,
                        "order_type": order.orderType,
                        "account": ex.acctNumber,
                        "trade_id": ex.execId,
                    }
                )
        return result

    def get_historical_data(
        self,
        symbol,
        duration="1 Y",
        bar_size="1 day",
        what="TRADES",
        currency="USD",
        exchange="SMART",
    ):
        if not self.connected:
            return []
        assert self._ib is not None
        try:
            contract = Stock(symbol, exchange, currency)
            self._call(self._ib.qualifyContractsAsync, contract)
            bars = self._call(
                self._ib.reqHistoricalDataAsync,
                contract,
                endDateTime="",
                durationStr=duration,
                barSizeSetting=bar_size,
                whatToShow=what,
                useRTH=True,
            )
            if not bars:
                return []
            return [
                {
                    "date": str(b.date),
                    "open": b.open,
                    "high": b.high,
                    "low": b.low,
                    "close": b.close,
                    "volume": b.volume,
                }
                for b in bars
            ]
        except Exception as e:
            log.warning(f"Historical data error: {e}")
            return []

    def get_live_quote(self, symbol, currency="USD", exchange="SMART"):
        if not self.connected:
            return {}
        assert self._ib is not None
        try:
            contract = Stock(symbol, exchange, currency)
            self._call(self._ib.qualifyContractsAsync, contract)
            ticker = self._call(self._ib.reqMktData, contract, "", False, False)
            if not ticker:
                return {"symbol": symbol}
            self._call(self._ib.sleep, 2)
            self._call(self._ib.cancelMktData, contract)
            return {
                "symbol": symbol,
                "bid": ticker.bid if ticker.bid == ticker.bid else 0,
                "ask": ticker.ask if ticker.ask == ticker.ask else 0,
                "last": ticker.last if ticker.last == ticker.last else 0,
                "volume": ticker.volume if ticker.volume == ticker.volume else 0,
                "high": ticker.high if ticker.high == ticker.high else 0,
                "low": ticker.low if ticker.low == ticker.low else 0,
                "close": ticker.close if ticker.close == ticker.close else 0,
            }
        except Exception as e:
            log.warning(f"Quote error: {e}")
            return {"symbol": symbol}


ib_conn = IBConnection()
