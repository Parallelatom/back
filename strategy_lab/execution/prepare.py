"""Offline request preparation and optional finalized receipt inspection. No trading."""
import argparse
import json
import time

from .accounts import (ReadOnlyRPC, claim_transaction, credential_present,
                       inspect_receipt, mint_payload, read_profiles)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--symbol", choices=["BTC", "XYZCL"], required=True)
    parser.add_argument("--pool")
    parser.add_argument("--outcome")
    parser.add_argument("--receipt")
    parser.add_argument("--share-token")
    parser.add_argument("--operation", choices=["buy", "claim"], default="buy")
    args = parser.parse_args()
    try:
        config = read_profiles(args.config, selected_symbol=args.symbol)
        profile = config["wallets"][args.symbol]
        result = {"mode": "preparation_only", "symbol": args.symbol,
                  "authorization_present": credential_present(profile["authorization_env"]),
                  "signer_configured": False, "live_ready": False}
        if args.pool:
            result["unsigned_claim"] = claim_transaction(profile["address"], args.pool)
            if args.outcome:
                result["unsigned_buy_payload"] = mint_payload(args.pool, args.outcome, int(time.time() * 1000))
        if args.receipt:
            if not args.pool or not args.share_token:
                raise ValueError("receipt inspection requires --pool and --share-token")
            receipt = ReadOnlyRPC().finalized_receipt(args.receipt)
            result["finalized_receipt_transfers"] = inspect_receipt(
                receipt, args.receipt, profile["address"], args.pool, args.share_token, args.operation)
        print(json.dumps(result, indent=2))
    except (ValueError, KeyError, TypeError, OSError):
        parser.exit(2, "Preparation failed: check public config, arguments, or finalized receipt. No transaction sent.\n")


if __name__ == "__main__":
    main()
