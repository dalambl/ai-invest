"""Connect to Interactive Brokers TWS and print account info."""

import argparse

from ib_insync import IB


def main():
    parser = argparse.ArgumentParser(description="Connect to IB TWS")
    parser.add_argument("--host", default="127.0.0.1", help="TWS host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=7496, help="TWS port (7496=live, 7497=paper)")
    parser.add_argument("--client-id", type=int, default=1, help="Client ID (default: 1)")
    args = parser.parse_args()

    ib = IB()

    host = args.host
    port = args.port
    client_id = args.client_id

    print(f"Connecting to TWS at {host}:{port}...")
    ib.connect(host, port, clientId=client_id)
    print("Connected.")

    print(f"\nManaged accounts: {ib.managedAccounts()}")

    summary = ib.accountSummary()
    for item in summary:
        if item.tag in ("NetLiquidation", "TotalCashValue", "BuyingPower"):
            print(f"  {item.tag}: {item.value} {item.currency}")

    positions = ib.positions()
    if positions:
        print(f"\nPositions ({len(positions)}):")
        for pos in positions:
            print(f"  {pos.contract.symbol}: {pos.position} @ avg {pos.avgCost:.2f}")
    else:
        print("\nNo open positions.")

    ib.disconnect()
    print("\nDisconnected.")


if __name__ == "__main__":
    main()
