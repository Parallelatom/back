# Preparing Strategy → Buy → Redeem

Status: **live Accounts buy and locally signed claim are implemented** through the explicit
`strategy_lab.execution.run_live` entry point. Default Collector/dashboard deployment does
not start it. Paper mode remains available independently. No real transaction was sent
while developing or testing this adapter; the live end-to-end tests use a mock service.

## Live setup on VPS

### Overnight test with shares > 1.30

Live entries now require **strictly more than 1.300000 quoted shares for 1 USDC**.
Exactly 1.300000 is rejected. `min_quote_shares_micro` defaults to `1300000` even in existing
local configs, so copying over a populated config is unnecessary. Paper/dashboard logic is
unchanged. The position records the stricter of this floor (+1 raw share unit) and the
existing 1% quote tolerance. An actual fill below that stored minimum is still accounted
and claimed, but persistently stops future buys. The API cannot atomically enforce minimum
output, so quote success is not a guaranteed fill price.

Stop the old live runner with Ctrl-C before starting the timed one, using the SAME ledger.
For the requested run ending **2026-09-17 09:00 Asia/Bangkok**:

```sh
git pull --ff-only origin main
bash run-live.sh XYZCL --execute --watch --until '2026-09-17T09:00:00+07:00'
```

To survive an SSH disconnect, run it under `nohup` instead (do not start both commands):

```sh
nohup bash run-live.sh XYZCL --execute --watch --until '2026-09-17T09:00:00+07:00' >> data/XYZCL-night.log 2>&1 &
tail -f data/XYZCL-night.log
```

Ctrl-C exits `tail`, not the background bot. Creating `data/HALT-XYZCL` stops new entries
while reconciliation and claims continue. Never delete the ledger. The Collector and
dashboard remain running as before. The selected profile must already be enabled and have
slippage acknowledgement; the command does not edit credentials or config.

`--until` requires a timezone offset and a new deadline within the next 24 hours. Alternatively,
`--overnight-hours 8` starts an eight-hour run. Timed mode overrides the old lifetime
`max_trades: 1` and daily cumulative spend cap with **unlimited attempts and cumulative
buy spend until the deadline, recycling confirmed claim proceeds from the 10 USDC initial
bankroll**. A **2 USDC gross realized-loss stop** persists for the whole session; the daily
loss limit also remains. Cash, live wallet balance, and exposure checks still apply. A
completed earlier trial is excluded from session loss totals; an outstanding earlier
position must still finish before another can open. Only one position is open at a time.
Finality delays, skips, insufficient funds or the loss stop can prevent trades before 09:00.
Existing saved timed sessions adopt this policy on upgrade without resetting their deadline
or loss history. Untimed trials and paper runners retain their existing spend/count limits.
Gas is separate from the USDC budget; every claim still has its configured gas ceiling.

Session baseline, start and deadline are durable. Neither restart nor UTC midnight resets
the totals or extends the deadline. Restart with the original command, or omit the timed flag
to resume the saved session. A different deadline is refused. After the deadline, new buys
stop but the process keeps reconciling and claiming pending positions. There is no automatic
second night or budget reset. Unknown transactions remain reserved and are never resent.

At 09:00, compare with the original dashboard's **Delta Edge** model:

```sh
.venv/bin/python -m strategy_lab.execution.compare --symbol XYZCL --csv data/XYZCL-night-comparison.csv
```

The read-only report pairs by Round, showing sides, actual/model shares, state, realized
USDC PnL and separately confirmed claim gas in ETH. The CSV also includes entry times and
whether the paper model's shares would exceed 1.30. The dashboard baseline keeps its normal
quality filters and has NO share-floor filter, gas cost, live finality waits or one-position
limit. It is a model comparison, not an expectation of equal profits. Missing/pending values
are `--`, not losses or zero payouts. Later reruns update pending outcomes. These commands
do not change the existing forward dashboard or send trades themselves.

Use Python 3.9+ and a dedicated EOA for each Symbol. The claim private key must derive the
same public address as that Symbol's configured wallet. API Authorization must belong to
that wallet; preflight can validate the signer but cannot prove API credential ownership
or expiry without an actual API operation. Do not share these wallets with another bot,
manual trade session, signer, or another VPS while this executor runs.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-live.txt
umask 077
cp -n execution-accounts.example.json execution-accounts.json
cp -n execution-secrets.env.example execution-secrets.env
chmod 600 execution-accounts.json execution-secrets.env
mkdir -p data
```

Fill these locally with an editor (never paste secrets into chat or shell history):

- `execution-accounts.json`: public address of each Symbol you use. When running only
  XYZCL, leave BTC disabled with an empty address. If both addresses are filled, they must
  be different. A selected or enabled Symbol always needs a valid public address.
- `execution-secrets.env`: full `NINELIVES_BTC_AUTHORIZATION` and
  `NINELIVES_XYZCL_AUTHORIZATION`, without adding `Bearer`; also each wallet's
  `NINELIVES_BTC_PRIVATE_KEY` / `NINELIVES_XYZCL_PRIVATE_KEY` for claiming.
  Private keys are hex keys for dedicated bot wallets, not seed phrases.
- Fund USDC for buys (intended budget 10 USDC per wallet) and ETH for claim gas separately.
  Default per-claim gas ceiling is 0.0001 ETH; pre-buy checks require that much ETH available.

Read-only preflight, no purchases or claims:

```sh
bash run-live.sh XYZCL
bash run-live.sh BTC
```

Preflight checks chain ID, token decimals, required contract code, EOA type, balances and
signer/address agreement. It does not send Authorization or sign a transaction. It creates
an empty live ledger bound to that wallet and Symbol. Only the selected wallet's secrets
need be filled; an unused disabled Symbol may have a blank public address.

Preflight errors use safe diagnostic codes: `CONFIG_ADDRESS_XYZCL` means an invalid/missing
public address, `PRIVATE_KEY_MISSING`/`PRIVATE_KEY_INVALID` means the selected signing key is
missing/malformed, `PRIVATE_KEY_MISMATCH` means it belongs to another wallet, and
`AUTHORIZATION_MISSING` means the selected Authorization variable is empty/invalid.
`RPC_REQUEST` means the RPC rejected a request or was unreachable. These messages do not
print supplied values. Other failures identify the stage (configuration, lock, ledger,
credentials, RPC or execution). Do not delete a live ledger to clear an error.

To run real money, set `enabled: true` for the chosen profile and deliberately set
`accept_unprotected_slippage: true`: the observed Accounts `Mint` input does not offer a
verified minimum-output parameter. A fresh quote is checked before sending, but cannot
atomically protect the fill. An actual fill below the quoted 1% tolerance is accounted
correctly, still resolved/claimed, and halts further entries persistently.

```sh
bash run-live.sh XYZCL --execute --watch
# Run separately for BTC:
bash run-live.sh BTC --execute --watch
```

Both `--execute` and `enabled: true` are needed for buying. `--execute` also allows outstanding
claims even with entries disabled. Untimed trial defaults are 1 USDC per Round, initial budget 10 USDC,
one open position, daily spend 10 USDC, daily realized loss stop 2 USDC, and `max_trades: 1`
for the first full-cycle test. `max_trades` counts positions over the entire ledger lifetime,
including uncertain/expired attempts; set up to 10 after review without deleting the ledger.
These are local bot limits, not restrictions on the credential itself or on external activity.

Collector must be running and writing `data/lab.db`. The live runner uses only fresh complete
Round observations and the same Delta Edge signal. It additionally checks pool expiry,
binary outcomes, share-token mapping, existing shares, quote and balances directly on chain.
Live Delta Edge does not require recent recorded reserves: those records only advance when
reserves change. Its direction signal uses prices and Strike, and a fresh successful chain
quote is mandatory before any reservation or submission. There is no opening-price fallback.
Paper execution keeps requiring fresh recorded reserves for its simulated fill. This
exception does not enable reserve-dependent live strategies.
It skips unsupported DPPM pools, stale signals and submissions less than 75 seconds from expiry.
A signal that ages beyond five seconds during quote reads is skipped. Skipping is expected
when recordings are incomplete/stale; it must not be interpreted as an API failure.

### Readable live logs

The default log format is human-readable text, with clock times in `Asia/Bangkok`.
Each market line includes the latest observed oracle price (six decimals), Strike,
signed Delta percentage, time remaining, feed timestamp and age, plus the current reason
for waiting or submitting. `[STALE]` means the latest recorded price is older than the
15-second price freshness limit. This is the Collector's observed feed, not a separate
direct WebSocket connection or an independently scraped website price.

The loop polls every two seconds plus any RPC processing time. A new tick or state prints
on the next pass; otherwise a heartbeat prints about every five seconds (rounded to a loop
pass). The upstream feed can update more slowly. During slow RPC calls logging also waits;
feed timestamps/ages make that lag visible. Price formatting may differ from the website's
rounding. No additional network request is made solely to render a log line.

`ROUND` announces a new Round, `ORDER` shows a changed position, and `TX` includes an explorer
link as soon as its hash is known, before finality. `LEDGER` is explicitly labeled as the
computed budget balance, not the wallet's live balance. The on-chain wallet balance is shown
at preflight. Log output does not include Authorization, private keys or signed raw bytes.

```sh
bash run-live.sh XYZCL --execute --watch
# Optional machine-readable output, including market timestamps/prices:
bash run-live.sh XYZCL --execute --watch --log-format json
# Optional display settings (do not run simultaneously for the same wallet):
bash run-live.sh XYZCL --execute --watch --log-timezone UTC --log-interval 10
```

Even if new entries are disabled or the trade limit is reached, available prices continue
to display while outstanding positions reconcile. Logging does not change strategy or
submission conditions. A Collector read failure blocks new buys but does not stop existing
on-chain reconciliation; never treat a missing display value as a quote.

### Stop and recovery

```sh
touch data/HALT-XYZCL    # stops new buys; existing positions keep reconciling/claiming
# Ctrl-C stops the runner entirely. Restart with the same live ledger to resume.
```

Ledger paths are `data/live-BTC.db` / `data/live-XYZCL.db`, separate from paper ledgers.
Back up these databases consistently with SQLite's backup API. Do not delete/reset them to
clear limits or pending states. The ledger stores operation hashes, unsigned request data and
signed claim bytes, never Authorization or private keys. Signed bytes can broadcast the
specific recorded claim; keep database files private. A host-wide wallet lock prevents
parallel local runners, but cannot protect against another machine using the same wallet.

Buy HTTP requests are attempted once, with no redirects or automatic retries. HTTP errors,
GraphQL errors or a lost response leave `BUY_PENDING` and reserve funds. A returned hash
still needs a canonical finalized receipt proving the pool/outcome, wallet USDC debit and
share mint. API success alone does not debit the ledger. If the response hash was lost,
find the actual transaction on the explorer, then attach it (no new buy is sent):

```sh
bash run-live.sh XYZCL --execute --attach-buy POSITION_ID TX_HASH
```

Only a successful finalized matching purchase is accepted. If no matching hash is known,
leave the position pending; never guess that timeout means no purchase happened.

Settlement uses finalized on-chain `details(bytes8)`, not an inferred oracle result.
Winning shares are claimed through `payoff(address[])` for one pool. Before signing, the
runner checks exact share balance, simulated payout, estimated gas ceiling, ETH balance and
pending nonce. Signed bytes and hash are committed before broadcasting. A missing response
is reconciled by that hash; it never triggers signing another nonce automatically.

For an interrupted claim, explicitly request recovery after inspecting the ledger:

```sh
bash run-live.sh XYZCL --execute --retry-claim POSITION_ID
```

If signed bytes exist, this rebroadcasts only those exact bytes. If no operation exists,
no claim was broadcast by the runner, so it reopens the claim-preparation step. A reverted
claim becomes `REDEEM_FAILED` and needs manual review, not automatic retries. Manual or
externally auto-claimed positions cause a balance mismatch and stay blocked for review;
this build does not silently credit external payouts. A replaced/dropped transaction also
remains pending; automatic fee replacement is intentionally not implemented.

Finality may substantially lag inclusion. While waiting, money stays reserved and no second
position is opened. The runner reports claim gas separately in wei; cash/PnL in USDC exclude
ETH gas. A persistent below-quote or reverted-transaction halt is recorded in `live_flags`;
there is no automatic clearing. Existing positions still reconcile. The dashboard still
shows research results, not this live ledger.

### Validation and deployment boundaries

Tests cover the full live adapter path against fake Accounts/RPC transports, real local
signing with an unfunded test key, restart recovery, missing acknowledgements, wrong wallets,
slippage, gas/nonce failures and loss settlement. The known XYZCL historical receipts were
also decoded, and deployed `shareAddr`, `details` and `isDppm` were checked read-only on
Arbitrum. Live API credential acceptance and a funded full-cycle run remain deployment checks.

The normal Docker Compose services remain read-only research/collection services. Use the
commands above on the VPS for live execution; `deploy.sh` does not implicitly start trading.
The `.dockerignore` excludes local wallet config and secret files from the build context.
The public examples can be committed, but local populated files must remain ignored.

## Requested setup

| Setting | BTC wallet | XYZCL wallet |
| --- | --- | --- |
| Strategy | Delta Edge | Delta Edge |
| Stake per Round | 1 USD | 1 USD |
| Starting funding budget | 10 USD | 10 USD |
| Symbol | BTC only | XYZCL only |
| Paper ledger | `data/execution-btc.db` | `data/execution-xyzcl.db` |

Total starting funding is 20 USD across two distinct wallets. The rehearsal uses the
existing 0.1% Delta Edge threshold per Symbol. The XYZCL public wallet and example buy/claim
transactions were supplied and inspected; BTC remains unspecified. Templates deliberately
leave both addresses empty for local configuration rather than publishing a wallet mapping.

Default limits are one open position per wallet, at most 10 USD spent per
UTC day, stop new entries after 2 USD of realized losing results that day, and 1% simulated
quote slippage. Review these limits before enabling the live profile.
Cash may exceed the initial 10 USD after simulated profits; no additional funding is added.
Positions with ambiguous submissions continue to reserve funds and occupy the position limit.

## Run locally

The offline demonstration uses an in-memory database and no network:

```sh
.venv/bin/python -m strategy_lab.execution --demo
```

It exercises a Delta Edge signal, a simulated buy receipt, settlement, a simulated redeem
receipt, and cash moving from 10 to 9 to 10.314422 USD. Repeated reconciliation does not
credit the same payout twice. `paper:...` references are local receipt identifiers, not
transaction hashes. Fees come from the existing AMM model; gas is not modeled.

To watch Collector recordings, copy the two example files:

```sh
cp execution-btc.example.json execution-btc.json
cp execution-xyzcl.example.json execution-xyzcl.json
```

Set `enabled` to `true` in these **paper** configs, then run each command in its own terminal:

```sh
.venv/bin/python -m strategy_lab.execution --config execution-btc.json --recordings data/lab.db --ledger data/execution-btc.db --halt-file data/HALT-BTC --watch
.venv/bin/python -m strategy_lab.execution --config execution-xyzcl.json --recordings data/lab.db --ledger data/execution-xyzcl.db --halt-file data/HALT-XYZCL --watch
```

Omit `--watch` for one pass. Disable entries with `enabled: false` and restart, or create
the relevant halt file while the runner is active. Existing positions still reconcile
and receive simulated payouts. Remove the halt file to resume entries. Configuration is
loaded at startup, not hot-reloaded. A disabled config with no existing ledger does nothing.

The ledger must differ from the Collector database and cannot be shared between the BTC
and XYZCL wallet labels. Back up these ledgers independently if the rehearsal matters;
the existing `backup.sh` covers `lab.db` only. Examples contain no credentials. Local config
copies and ledgers are gitignored.

## What the rehearsal verifies

- The Strategy sees only prices and Reserves available at the decision timestamp. Known
  future outcomes and reconstructed Rounds cannot trigger a new entry.
- Entries require current Round metadata, recent prices, coverage from Round start, and
  fresh observed Reserves. Seed-only/missing/stale Reserves cause a skip, not a 0.50 guess.
- A signal more than five seconds old is not executed late after a restart. The normal
  300-to-60-second Trade Window remains enforced.
- Buy intent and reserved funding are committed before submission. One Symbol/ending can
  have only one position per ledger, even if Strategy settings change.
- A timeout after submission leaves the position pending. Subsequent passes look up its
  receipt; they never blindly resubmit. A committed but unsubmitted intent is expired.
- Confirmed paper buys debit cash. Missing settlement stays unresolved. Only the Collector's
  outcome-decided event is accepted for settlement; following-Strike inference is insufficient.
- Losing positions close without a redeem. Winning positions pass through a separate redeem
  acknowledgement and receipt; only that receipt credits cash. Failed redemption remains
  visible and holds its exposure for manual review.
- State and local receipts survive restarts. Unknown transport exception text is not logged,
  because transport exceptions could include credentials in that text.

The Collector currently records reconciliation only when Reserves change. Therefore the
strict freshness gate can skip otherwise valid Rounds whose pool has not changed recently.
The live adapter instead requires a fresh on-chain quote after the Delta Edge signal gate;
it still requires fresh metadata and complete, fresh oracle prices. Rehearsal results are not expected to match
the retrospective dashboard. The existing dashboard does not display these new ledgers.

## Integration research, checked 2026-09-16

The historical ADR-0001 statement that no API keys exist is not a sufficient integration
reference now. The [official RPC help](https://arb-rpc.9lives.so/help) documents scoped API
keys, a `mint` method returning a transaction hash, and a custodial EOA. Its `canBuy` scope
also allows withdrawals. Two API keys for one account are not evidence of two independent
wallets: the account-to-EOA mapping must be checked. The help mixes Arbitrum and older
Superposition examples, so endpoint, chain, token, and account behavior need read-only
verification before any funding or submission. The listed methods do not establish a
standalone redeem endpoint; withdrawing cash is not redeeming winning shares.

The [official mint source](https://github.com/fluidity-money/9lives.so/blob/main/src/contract_trading_mint.rs)
has both `mint_8_A_059_B_6_E` and an enclave-only `mint_schedule_claim_C_8_A_5591_F`. The
[quote/payoff source](https://github.com/fluidity-money/9lives.so/blob/main/src/contract_trading_quotes.rs)
contains `payoff_C_B_6_F_2565`. This proves that both explicit payoff and a scheduled-claim
path exist in source, not that a particular deployed endpoint uses either. Contract source
must be matched to the deployed pool implementation and receipt events before selecting
the adapter. Do not send both a manual claim and a scheduled claim without reconciliation.

The direct mint signature shown in source has no minimum-shares argument. The simulator's
slippage rejection is not an on-chain guarantee: a live design must establish atomic price
protection (for example through a verified supported router) or explicitly document its
absence. A quote check immediately before signing does not itself enforce minimum output.

## Accounts route observed in the user's browser

The browser capture establishes POST `https://arb-accounts.superposition.so/`, GraphQL
`mutation ($mint: Mint!) { ninelivesMint(mint: $mint) }`, and an `Authorization` header.
The user confirms this header value is what the old 5-minute script called `API_KEY`.
The response contains `data.ninelivesMint`, a transaction hash. The old script points to
`accounts.superposition.so`; do not reuse that endpoint for the observed Arbitrum route.
Do not prepend `Bearer` or infer that an address alone authenticates the request.
The credential format, expiry, scope and wallet mapping remain unverified. This route
differs from both the public `requestPaymaster` hook and the RPC API-key documentation.

The supplied successful buy/claim receipts show 1 USDC spent, 1,314,422 share units minted,
then the same share units burned and 1.314422 USDC returned. This is one historical example,
not proof of API access for a second wallet or an automated signer. Manual claim uses
Claimant Helper `0xf8Da8d65120b317331C79092Bf65e99bed6e65dE`, selector `0xeaca3a20`
(`payoff(address[])`). Helper success alone is insufficient: it may catch individual failures.

The live implementation is `strategy_lab/execution/live.py`; use the setup and recovery
commands at the top of this document. The older `execution.prepare` CLI remains an offline
request builder/read-only receipt inspector; it does not enable or replace the live runner.

Live Delta Edge allows up to 15 seconds from its first signal, including quote RPC time
(paper runner retains 5 seconds). It still skips the Round after expiry rather than
chasing a later signal. Oracle and metadata must remain at most 15 seconds old after
quote reads and before submission; the separate 5-second intent-to-submit limit and
75-second Round cutoff remain. Dashboard paper results do not include this RPC delay.

Claim policy: start checking at Round end + 300 seconds. Read the winner from `latest`;
if unresolved, keep polling. A winning position must pass the existing share-balance,
positive payout simulation, gas and nonce checks before one persisted signed transaction
is broadcast. If buy finality is still pending after this delay, a canonical mined receipt
with the same transfer/event validation may establish the position for claiming. This
accepts pre-finality chain risk for claim submission; loss classification and claim payout
credit still require finalized chain evidence. The one-open-position limit therefore still
blocks new buys until claim finality. Restart preserves existing signed claims and never
signs them again. Simulation/gas/nonce failures before signing remain pending for inspection
and explicit retry; unresolved winners alone are polled automatically.
