"""
Live brokers - real signed order placement against real exchanges. Every
function here can move real money once wired up. Only invoked when
config.LIVE_TRADING is true AND the relevant exchange is in
config.LIVE_EXCHANGES - everything else stays in pure paper-simulation mode
(engine.py's existing _mark_to_market / DB-only logic), regardless of this
module's existence.

Registry keys match the short exchange names used everywhere else in this
codebase (config.EXCHANGES_PER_COIN, config.TAKER_FEE, etc.): "hl", "pac",
"ost", "aster" - NOT each module's own filename. A pair trades live only if
BOTH of its legs are in config.LIVE_EXCHANGES AND have a broker here, since
the strategy is inherently two-legged.
"""
from . import aster as aster_broker
from . import hyperliquid as hl_broker
from . import ostium as ostium_broker
from . import pacifica as pacifica_broker

BROKERS = {
    "aster": aster_broker,
    "hl": hl_broker,
    "pac": pacifica_broker,
    "ost": ostium_broker,
}
