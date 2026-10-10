"""Stand-ins for mkt-data, secmaster-svc and calendar-svc (app/upstream.py)."""

from datetime import date

from app.upstream import InstrumentInfo, Period, Value

KEYS = {
    "UST-PAR": {"BC_10YEAR": 12, "BC_1_5MONTH": 2},
    "H15-TCM": {"RIFLGFCY10_N.B": 12},
    # Three securities in FedInvest's 2026-10-05 page (tests/fixtures): a bill, a note, a TIPS.
    "CUSIP": {"912797UJ4": 101, "91282CRK9": 102, "912810FD5": 103},
    "NYFED-SOFR": {"SOFR": 201},
    "NYFED-EFFR": {"EFFR": 202},
    "FRB-H10-RATES": {"RXI$US_N.B.EU": 203, "RXI_N.B.JA": 204},
    "FRB-H10": {"JRXWTFB_N.B": 205},
    "ECB-EXR": {"EXR.D.JPY.EUR.SP00.A": 206},
    "CFTC": {"043602": 301},  # both CFTC reports resolve in secmaster-svc's CFTC scheme
}
NAMES = {12: "UST-10Y-CMT", 2: "UST-1.5M-CMT", 101: "UST-B-2026-10-08", 102: "UST-3.5-2028-09-30",
         103: "UST-TII-3.625-2028-04-15", 201: "SOFR", 202: "EFFR", 203: "EURUSD-H10", 204: "USDJPY-H10",
         205: "USD-BROAD-H10", 206: "EURJPY-ECB", 301: "TY"}
TYPES = {12: "cmt_yield", 2: "cmt_yield", 101: "ust_bill", 102: "ust_note", 103: "ust_tips", 201: "rate_fixing",
         202: "rate_fixing", 203: "fx_fixing", 204: "fx_fixing", 205: "fx_index", 206: "fx_fixing",
         301: "fut_product"}


class FakeUpstream:
    def __init__(self):
        # source -> period -> (capture_id, [(key, as_of, value)])
        from app.load import SOURCES

        self.data: dict[str, dict[str, tuple[int, list]]] = {src: {} for src in SOURCES}
        self.status: dict[int, str] = {}  # sec_id -> status, "active" if not set
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
        for row in rows:  # (key, as_of, value) for a yield, or (key, as_of, field, value, unit)
            key, as_of, field, value, unit = row if len(row) == 5 else (row[0], row[1], "yield", row[2], "percent")
            self._obs += 1
            out.append(Value(self._obs, key, as_of, field, value, unit, cap))
        return out

    def resolve(self, scheme, keys):
        self.resolves.append((scheme, list(keys)))
        known = KEYS.get(scheme, {})
        return {k: known[k] for k in keys if k in known}, [k for k in keys if k not in known]

    def instruments(self):
        return {i: InstrumentInfo(n, TYPES[i], self.status.get(i, "active")) for i, n in NAMES.items()}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeCalendars:
    """calendar-svc's answers: SIFMA-US covers 2026 (closed 2026-10-12), FED covers 2025."""

    def __init__(self):
        self.covered = {"SIFMA-US": {2026}, "FED": {2025, 2026}}
        self.closed = {"SIFMA-US": {date(2026, 10, 12)}, "FED": {date(2025, 12, 25)}}

    def covered_years(self, calendar):
        return self.covered.get(calendar, set())

    def closed_days(self, calendar, start, end):
        return {d for d in self.closed.get(calendar, set()) if start <= d <= end}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
