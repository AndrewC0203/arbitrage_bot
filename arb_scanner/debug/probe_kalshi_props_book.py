"""
Phase 0 probe — can Kalshi prop tickers ride the orderbook_delta channel?

The ghost-free arbs design (docs/superpowers/specs/2026-07-02-ghost-free-arbs-
design.md, Layer 2) wants props moved off the seq-less ticker channel onto the
book channel. Unknown to measure first: per-connection subscription limits and
message volume at ~800-2,000 prop tickers (ML uses only ~74 today).

For each subscription count, this opens a FRESH connection, subscribes that
many live prop tickers to orderbook_delta (chunks of 50, same as production),
listens for a fixed window, and records:
  - subscribe acks vs error frames (verbatim)
  - snapshot coverage: distinct tickers that delivered orderbook_snapshot
  - msg/s by type, total bytes
  - whether the per-sid seq scheme holds (gaps / non-monotonic seqs)

Output: per-step summary + go/no-go verdict, saved to
debug/probe_kalshi_props_book_results.json.

FINDING (2026-07-02 live, 951 open props): chunked subscribes to the same
channel share ONE sid — chunk 1 acks `subscribed`, chunks 2..N ack `ok`, and
those acks CONSUME seq numbers on the sid (rule 19). Tracking seq only on book
frames shows phantom gaps at every chunk boundary; tracked across all frames:
0 gaps, 100% snapshot coverage, 241 msg/s, 0 errors at 951 tickers → GO.

One-shot diagnostic — not part of the main loop.
Run from arb_scanner/:
  python debug/probe_kalshi_props_book.py [listen_seconds] [counts]
e.g.  python debug/probe_kalshi_props_book.py 30 250,1000
"""

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import websockets

from ws_manager import (
    KALSHI_WS_URL,
    _load_ws_credentials,
    _ws_auth_headers,
    _ws_subscribe_chunks,
    fetch_kalshi_props,
)

LISTEN_SECONDS_DEFAULT = 30
STEP_COUNTS = [100, 250, 500, 1000, 2000]
RESULTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "probe_kalshi_props_book_results.json")


async def _probe_one(auth_headers: dict, tickers: list[str], listen_seconds: int) -> dict:
    result = {
        "requested": len(tickers),
        "subscribe_acks": 0,
        "errors": [],
        "msg_counts": {},
        "snapshot_tickers": 0,
        "snapshot_coverage": 0.0,
        "seq_gaps": 0,
        "gap_details": [],       # what actually surrounds each gap
        "seq_bearing_types": {}, # which frame types carry sid+seq
        "sids": 0,
        "total_msgs": 0,
        "total_bytes": 0,
        "msgs_per_sec": 0.0,
        "listen_seconds": listen_seconds,
    }
    snapshot_tickers: set = set()
    last_seq: dict[int, int] = {}
    last_type: dict[int, str] = {}

    async with websockets.connect(KALSHI_WS_URL, additional_headers=auth_headers) as ws:
        await _ws_subscribe_chunks(ws, tickers, "orderbook_delta", start_id=1)
        start = time.time()
        while True:
            remaining = listen_seconds - (time.time() - start)
            if remaining <= 0:
                break
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            result["total_msgs"] += 1
            result["total_bytes"] += len(raw)
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                result["errors"].append(f"unparseable frame: {raw[:200]!r}")
                continue
            mtype = data.get("type", "?")
            result["msg_counts"][mtype] = result["msg_counts"].get(mtype, 0) + 1
            if mtype == "subscribed":
                result["subscribe_acks"] += 1
            elif mtype == "error":
                result["errors"].append(json.dumps(data)[:300])
            elif mtype == "orderbook_snapshot":
                ticker = (data.get("msg") or {}).get("market_ticker")
                if ticker:
                    snapshot_tickers.add(ticker)
            # Track seq on EVERY sid+seq-bearing frame (incl. ok/subscribed):
            # production only tracks book frames, so if acks consume seqs,
            # production's gap detector sees phantom gaps at each chunk add.
            sid, seq = data.get("sid"), data.get("seq")
            if sid is not None and seq is not None:
                result["seq_bearing_types"][mtype] = \
                    result["seq_bearing_types"].get(mtype, 0) + 1
                prev = last_seq.get(sid)
                if prev is not None and seq != prev + 1:
                    result["seq_gaps"] += 1
                    if len(result["gap_details"]) < 20:
                        result["gap_details"].append({
                            "sid": sid, "prev_seq": prev, "seq": seq,
                            "prev_type": last_type.get(sid), "type": mtype,
                        })
                last_seq[sid] = seq
                last_type[sid] = mtype

    elapsed = max(time.time() - start, 0.001)
    result["snapshot_tickers"] = len(snapshot_tickers)
    result["snapshot_coverage"] = round(len(snapshot_tickers) / len(tickers), 4)
    result["sids"] = len(last_seq)
    result["msgs_per_sec"] = round(result["total_msgs"] / elapsed, 2)
    return result


async def main() -> None:
    listen_seconds = int(sys.argv[1]) if len(sys.argv) > 1 else LISTEN_SECONDS_DEFAULT
    step_counts = ([int(c) for c in sys.argv[2].split(",")] if len(sys.argv) > 2
                   else STEP_COUNTS)

    creds = _load_ws_credentials()
    if creds is None:
        sys.exit("No Kalshi credentials — set KALSHI_KEY_ID and KALSHI_KEY_PATH in .env")
    api_key_id, private_key = creds

    print("Fetching open prop tickers via REST...")
    props, ok_series = fetch_kalshi_props(datetime.now(timezone.utc).date())
    tickers = sorted({p["ticker"] for p in props})
    print(f"  {len(tickers)} open prop tickers across {sorted(ok_series)}")
    if not tickers:
        sys.exit("No open prop tickers right now — rerun on a game day.")

    counts = [c for c in step_counts if c <= len(tickers)]
    if not counts or counts[-1] < len(tickers):
        counts.append(len(tickers))  # always test everything available

    runs = []
    for n in counts:
        print(f"\n=== {n} tickers on orderbook_delta, listening {listen_seconds}s ===")
        # Fresh auth per connection: signature timestamps go stale
        auth_headers = _ws_auth_headers(api_key_id, private_key)
        try:
            r = await _probe_one(auth_headers, tickers[:n], listen_seconds)
        except Exception as exc:
            r = {"requested": n, "fatal": f"{type(exc).__name__}: {exc}"}
            print(f"  FATAL: {r['fatal']}")
            runs.append(r)
            continue
        runs.append(r)
        acks = r["subscribe_acks"] + r["msg_counts"].get("ok", 0)
        print(f"  acks {acks}/{-(-n // 50)} chunks "
              f"(subscribed {r['subscribe_acks']} + ok {r['msg_counts'].get('ok', 0)}) | "
              f"snapshots {r['snapshot_tickers']}/{n} ({r['snapshot_coverage']:.1%}) | "
              f"{r['total_msgs']} msgs ({r['msgs_per_sec']}/s, {r['total_bytes']} B) | "
              f"sids {r['sids']} | seq gaps {r['seq_gaps']} | errors {len(r['errors'])}")
        for e in r["errors"][:5]:
            print(f"    error: {e}")

    ok_runs = [r for r in runs if "fatal" not in r]
    max_viable = max(
        (r["requested"] for r in ok_runs
         if r["snapshot_coverage"] >= 0.99 and not r["errors"] and r["seq_gaps"] == 0),
        default=0,
    )
    go = max_viable >= len(tickers)
    verdict = {
        "probed_at": datetime.now(timezone.utc).isoformat(),
        "open_prop_tickers": len(tickers),
        "listen_seconds": listen_seconds,
        "max_viable_subscription": max_viable,
        "go_for_phase2": go,
        "runs": runs,
    }
    with open(RESULTS_PATH, "w") as f:
        json.dump(verdict, f, indent=2)

    print(f"\n{'='*70}")
    print(f"  Max viable subscription: {max_viable} of {len(tickers)} open props")
    print(f"  Phase 2 (props on book channel): {'GO' if go else 'NO-GO'}")
    print(f"  Full results: {RESULTS_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
