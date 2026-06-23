"""
Live brokers - real signed order placement against real exchanges. Every
function here can move real money once wired up. Only invoked when
config.LIVE_TRADING is true AND the relevant exchange is in
config.LIVE_EXCHANGES - everything else stays in pure paper-simulation mode
(engine.py's existing _mark_to_market / DB-only logic), regardless of this
module's existence.

Build order (simplest auth -> most complex): Aster (HMAC REST, done) ->
Hyperliquid (wallet-signed via official SDK) -> Pacifica (agent-wallet
signing) -> Ostium (on-chain, Arbitrum, needs a gas-funded wallet). Each
exchange only goes live once its broker module is added here AND its name
is added to LIVE_EXCHANGES - a pair trades live only if BOTH of its legs
have a broker, since the strategy is inherently two-legged.
"""
from . import aster as aster_broker

BROKERS = {
    "aster": aster_broker,
}
