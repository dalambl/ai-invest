from dataclasses import asdict, dataclass, fields


@dataclass
class Position:
    timestamp: str
    account: str
    symbol: str
    sec_type: str
    quantity: float
    avg_cost: float
    market_price: float
    market_value: float
    unrealized_pnl: float
    realized_pnl: float
    currency: str

    @classmethod
    def csv_header(cls):
        return [f.name for f in fields(cls)]

    def to_row(self):
        return list(asdict(self).values())


@dataclass
class Trade:
    trade_date: str
    symbol: str
    description: str
    asset_class: str
    action: str
    quantity: float
    price: float
    currency: str
    commission: float
    net_amount: float
    exchange: str
    order_type: str
    account: str
    trade_id: str

    @classmethod
    def csv_header(cls):
        return [f.name for f in fields(cls)]

    def to_row(self):
        return list(asdict(self).values())


@dataclass
class Snapshot:
    date: str
    symbol: str
    quantity: float
    market_price: float
    market_value: float
    day_pnl: float
    cost_basis: float

    @classmethod
    def csv_header(cls):
        return [f.name for f in fields(cls)]

    def to_row(self):
        return list(asdict(self).values())
