"""A stand-in for mkt-data and secmaster-svc (app/upstream.py's Upstream)."""

from app.upstream import Period, Value

KEYS = {
    "UST-PAR": {"BC_10YEAR": 12, "BC_1_5MONTH": 2},
    "H15-TCM": {"RIFLGFCY10_N.B": 12},
}
NAMES = {12: "UST-10Y-CMT", 2: "UST-1.5M-CMT"}


class FakeUpstream:
    def __init__(self):
        # source -> period -> (capture_id, [(key, as_of, value)])
        self.data: dict[str, dict[str, tuple[int, list]]] = {"UST-PAR": {}, "H15-TCM": {}}
        self.reads: list[tuple[str, str]] = []
        self.resolves: list[tuple[str, list[str]]] = []
        self._obs = 0

    def put(self, source, period, capture_id, rows):
        self.data[source][period] = (capture_id, rows)

    # Upstream
    def list_periods(self, source):
        return [Period(p, cap, len(rows)) for p, (cap, rows) in sorted(self.data[source].items())]

    def get_period(self, source, period):
        self.reads.append((source, period))
        cap, rows = self.data[source][period]
        out = []
        for key, as_of, value in rows:
            self._obs += 1
            out.append(Value(self._obs, key, as_of, "yield", value, "percent", cap))
        return out

    def resolve(self, scheme, keys):
        self.resolves.append((scheme, list(keys)))
        known = KEYS.get(scheme, {})
        return {k: known[k] for k in keys if k in known}, [k for k in keys if k not in known]

    def instruments(self):
        return dict(NAMES)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
