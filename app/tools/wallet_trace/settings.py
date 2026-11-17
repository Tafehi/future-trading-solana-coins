"""Every number the tracer uses, in one block (PLAN §5-7). They are starting points fitted to the 2026-11-17 case
study, not truths: change them here, never inline.
"""

# --- pacing (PLAN §4) ---------------------------------------------------------------------------------------------
# Hyperliquid's limit is by WEIGHT, per IP: 1200 a minute. Most info calls weigh 20 (userRole 60, the account-state
# calls 2), and the fill/funding lists add 1 per 20 items returned (docs "Rate limits and user limits", read
# 2026-11-17). So 2 calls a second (2 x 20 x 60 = 2400) would break it: the weight budget paces Hyperliquid to about
# one call a second, and the request rate below is the upper cap for both chains.
MAX_CALLS_PER_SECOND = 2.0
HL_WEIGHT_PER_MINUTE = 900  # the limit is 1200; headroom for item weight learned only after an answer
HL_429_RETRIES = 2  # a 429 that outlasts the HTTP backoff is waited out this many times ...
HL_429_WAIT_S = 60.0  # ... for this long each
HL_WEIGHT_DEFAULT = 20
HL_WEIGHT = {"clearinghouseState": 2, "spotClearinghouseState": 2, "userRole": 60, "allMids": 2}
HL_WEIGHT_PER_ITEMS = 20  # +1 weight per this many items for the list endpoints below
HL_ITEM_WEIGHTED = {
    "userFillsByTime",
    "userFunding",
    "historicalOrders",
    "userTwapSliceFillsByTime",
    "delegatorHistory",
}

# --- paging (Hyperliquid docs, info endpoint, read 2026-11-17) ------------------------------------------------------
HL_PAGE_TIME_RANGE = 500  # "Responses that take a time range will only return 500 elements"
HL_PAGE_FILLS = 2000  # userFillsByTime: "at most 2000 fills per response"
HL_FILLS_VISIBLE = 10_000  # "only the 10000 most recent fills are available"

# --- walk (PLAN §7) -------------------------------------------------------------------------------------------------
DEFAULT_DEPTH = 2
DEFAULT_TOP = 5
DEFAULT_MAX_CALLS = 500

# --- profile (PLAN §5) ----------------------------------------------------------------------------------------------
EXIT_DOOR_MINUTES = 10.0  # toPerp then withdraw within this = money leaving, not trading (case: ~5 min)
BIG_TRANSFER_USD = 1_000.0  # an incoming transfer this big is checked for pass-through
ROUND_LOT_MIN = 100.0  # a sell order is "round" if its size is >= this ...
ROUND_LOT_STEP = 50.0  # ... and a multiple of this (100, 250, 300, 500, 1,000 ...)
BALANCE_ABS_TOL = 1e-3  # balance-sum check: allowed gap per token ...
BALANCE_REL_TOL = 1e-6  # ... or this share of everything that moved through it, whichever is bigger ...
BALANCE_USD_TOL = 10.0  # ... and a gap worth less than this (at the wallet's own last price) is noted, not failed

# --- classify (PLAN §5, rules 1-9) ---------------------------------------------------------------------------------
PASSTHROUGH_MINUTES = 30.0
PASSTHROUGH_SHARE = 0.9
PASSTHROUGH_MAX_FILLS = 10
CASH_OUT_MAX_LIFETIME_DAYS = 3.0
CASH_OUT_SELL_SHARE = 0.9
CASH_OUT_WITHDRAW_SHARE = 0.5
ONE_SHOT_OUT_SHARE = 0.9
ONE_SHOT_WINDOW_HOURS = 24.0
BOT_MIN_ORDERS = 300
BOT_MIN_ACTIVE_DAYS = 30
BOT_MIN_FILLS_PER_DAY = 20.0
BOT_MIN_PERP_SHARE = 0.5
BOT_SOL_MIN_SWAPS = 300
BOT_SOL_MIN_TOKENS = 20
PERP_MIN_SHARE = 0.5
PERP_MIN_JUP_EPISODES = 5
SLOW_SELL_SHARE = 0.8
SLOW_SELL_ROUND_SHARE = 0.5
SLOW_SELL_MIN_DAYS = 5

# --- links (PLAN §6) ------------------------------------------------------------------------------------------------
TWIN_AIRDROP_POINTS = 3  # each airdrop token received at the same second ...
TWIN_AIRDROP_MAX_POINTS = 3  # ... but all airdrops together count as ONE clue: early wallets share dozens of them
TWIN_SMALL_SENDER_POINTS = 2  # each small sender that paid both within the window
TWIN_SMALL_SENDER_USD = 50.0  # case: 15 LQNA ($23) and 0.0001 HYPE from the same senders
TWIN_SMALL_SENDER_SECONDS = 120
TWIN_FUNDER_POINTS = 3
TWIN_VAULT_POINTS = 1
TWIN_VAULT_DAYS = 7
TWIN_BOTH_WAYS_POINTS = 1
TWIN_MASTER_POINTS = 5
TWIN_MIN_SCORE = 6  # "same owner, very likely"

# --- Solana (PLAN §4.2) ---------------------------------------------------------------------------------------------
SOL_DEFAULT_RPC = "https://api.mainnet-beta.solana.com"  # free public RPC; never the bot's SOLANA_RPC_URL by default
SOL_CALLS_PER_SECOND = 1.0  # measured 2026-11-17: ~2/s drew 429 after ~10 getTransaction calls, 1/s passed 20 in a row
SOL_MAX_TX_PER_WALLET = 100  # newest transactions decoded per wallet (plus the oldest few, for the funder)
# With the Chainstack URL from the secret (user, 2026-11-17, D-27). Chainstack allowed 5 requests/s for filtered
# getProgramAccounts (docs.chainstack.com/docs/limits, read 2026-11-13 for the removed bot); 4/s keeps a margin.
SOL_CALLS_PER_SECOND_PAID = 4.0
SOL_MAX_TX_PER_WALLET_PAID = 300
SOL_OLDEST_TX = 3
SOL_SIG_PAGE = 1000
SOL_MAX_SIG_PAGES = 3  # more than 3,000 transactions: the funder is out of reach (noted in the report)
SOL_JUP_TX_PER_POSITION = 200  # newest transactions read per Jupiter Perps Position account
SOL_DUST_LAMPORTS = 1_000  # after the network fee, a swap's SOL move below this is noise (a real sell: 0.0023 SOL)
SOL_SMALL_TRANSFER_SOL = 0.05  # a "small sender" on Solana (twin clue)
