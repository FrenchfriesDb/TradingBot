"""Wait for the market to open in a way a sleeping laptop cannot break.

lumibot's Broker._await_market_to_open computes the wait ONCE and hands it to a single
time.sleep(), despite a docstring promising an "infinite loop until market opens":

    isOpen = self.is_market_open()
    if not isOpen:
        time_to_open = self.get_time_to_open()
        ...
        self.sleep(sleeptime)

Started at 15:42 on 2026-09-17, that became time.sleep(53280) — 14h48m, due to end at
the 06:30 open. On macOS time.sleep is measured against a clock that does not advance
while the system is asleep, and pmset shows the machine slept 5.28 hours that night
("Entering Sleep state ... Using Bat" — caffeinate -i -s only holds sleep off on AC
power). At 07:15 the bot was still waiting; its timer was due to expire at about 11:46,
against a 13:00 close.

The defect is not the sleeping laptop. It is computing a deadline once and trusting a
timer to still mean something hours later. Anything that stops a monotonic clock breaks
it — suspend, hibernate, a paused VM — and the opposite error, waking early into a
market that has not opened, is just as wrong.

So: sleep in bounded chunks and ask the broker again each time. The broker's own
is_market_open() is the authority; an arithmetic prediction made hours ago is not.
"""

DEFAULT_MAX_CHUNK = 60.0        # re-check at least once a minute
# FLOOR for the give-up cap, not the cap itself. This was the cap, at 36h, and a normal
# US weekend is Fri 13:00 close to Mon 06:30 open = 65.5h — so the safety net meant for
# "this market never opens" fired every single Friday night. Live on 2026-09-20 07:58:12,
# after waiting from Fri 19:37: "Gave up waiting for the market to open". The bot
# recovered, but a false alarm that fires weekly trains you to ignore the real one.
DEFAULT_MAX_WAIT = 36 * 3600.0
# The broker knows when it expects to open; trust that over a constant, with headroom for
# the estimate drifting. Bounded so a nonsense estimate cannot mean "wait for ever".
ESTIMATE_HEADROOM = 1.25
ABSOLUTE_MAX_WAIT = 8 * 24 * 3600.0
_MIN_SLEEP = 1.0                # get_time_to_open() can return <= 0 while still closed


def await_market_open(is_market_open, get_time_to_open, sleep,
                      max_chunk=DEFAULT_MAX_CHUNK, max_wait=None):
    """Block until the market opens. Returns True if it opened, False if we gave up.

    Every argument is injected so this is testable without a broker or a real clock:
      is_market_open()   -> bool   the authority. Asked again after every chunk.
      get_time_to_open() -> float  seconds, advisory only — used to avoid overshooting
                                   a near open, never trusted as a deadline.
      sleep(seconds)               how to wait.

    Both callbacks are allowed to raise; a network blip during an overnight wait must
    not take the strategy down before the session it was waiting for.
    """
    # Derive the give-up cap from the broker's own first estimate unless the caller
    # named one: a weekend is 65.5h and no constant chosen in the abstract covers it.
    if max_wait is None:
        try:
            first = float(get_time_to_open())
        except Exception:
            first = 0.0
        max_wait = min(ABSOLUTE_MAX_WAIT,
                       max(DEFAULT_MAX_WAIT, first * ESTIMATE_HEADROOM))

    waited = 0.0
    while waited < max_wait:
        try:
            if is_market_open():
                return True
        except Exception:
            pass          # a blip is not an answer — wait and ask again

        try:
            remaining = float(get_time_to_open())
        except Exception:
            remaining = max_chunk          # no estimate: fall back to the poll interval

        # Cap by the chunk so a suspend cannot swallow the whole wait, and by the
        # estimate so we do not sleep a full minute past an open twenty seconds away.
        chunk = min(max_chunk, remaining if remaining > 0 else max_chunk)
        chunk = max(_MIN_SLEEP, chunk)
        sleep(chunk)
        waited += chunk

    try:
        return bool(is_market_open())
    except Exception:
        return False
