from datetime import timedelta

CLAIM_WINDOW_HOURS = 48
CLAIM_WINDOW = timedelta(hours=CLAIM_WINDOW_HOURS)
SELF_SERVICE_MODES = frozenset({"unique", "shared"})
