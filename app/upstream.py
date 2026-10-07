"""What quote-svc reads from: mkt-data's observations and secmaster-svc's map (gRPC).

`Upstream` is the interface the load job uses; `GrpcUpstream` talks to the
real services on the home-platform network (proto/observations.proto and
proto/securities.proto, copied from those repos). Tests use a fake with the
same four methods.
"""

from dataclasses import dataclass
from typing import Protocol

TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class Period:
    period: str  # YYYY-MM
    latest_capture_id: int
    values: int


@dataclass(frozen=True)
class Value:
    observation_id: int
    source_key: str
    as_of: str  # YYYY-MM-DD
    field: str
    value: str  # a decimal string, as mkt-data stores it
    unit: str
    capture_id: int


@dataclass(frozen=True)
class InstrumentInfo:
    short_name: str
    type: str  # cmt_yield, ust_bill, ust_note, ...
    status: str  # active, matured, called, withdrawn, ...


class Upstream(Protocol):
    def list_periods(self, source: str) -> list[Period]: ...
    def get_period(self, source: str, period: str) -> list[Value]: ...
    def resolve(self, scheme: str, keys: list[str]) -> tuple[dict[str, int], list[str]]: ...
    def instruments(self) -> dict[int, InstrumentInfo]: ...


class GrpcUpstream:
    """mkt-data (Observations) and secmaster-svc (Securities) over gRPC. Use as a context manager."""

    def __init__(self, mkt_data: str, secmaster: str):
        self.targets = (mkt_data, secmaster)

    def __enter__(self):
        import grpc  # here, so the rest of the app (and its tests) runs without compiled grpcio

        from app.grpc_gen import observations_pb2_grpc, securities_pb2_grpc

        self._channels = [grpc.insecure_channel(t) for t in self.targets]
        self._obs = observations_pb2_grpc.ObservationsStub(self._channels[0])
        self._sec = securities_pb2_grpc.SecuritiesStub(self._channels[1])
        return self

    def __exit__(self, *exc):
        for c in self._channels:
            c.close()

    def list_periods(self, source: str) -> list[Period]:
        from app.grpc_gen import observations_pb2 as pb

        r = self._obs.ListPeriods(pb.ListPeriodsRequest(source=source), timeout=TIMEOUT_SECONDS)
        return [Period(p.period, p.latest_capture_id, p.values) for p in r.periods]

    def get_period(self, source: str, period: str) -> list[Value]:
        from app.grpc_gen import observations_pb2 as pb

        r = self._obs.GetPeriod(pb.GetPeriodRequest(source=source, period=period), timeout=TIMEOUT_SECONDS)
        return [Value(v.id, v.source_key, v.as_of, v.field, v.value, v.unit, v.capture_id) for v in r.values]

    def resolve(self, scheme: str, keys: list[str]) -> tuple[dict[str, int], list[str]]:
        from app.grpc_gen import securities_pb2 as pb

        r = self._sec.Resolve(pb.ResolveRequest(scheme=scheme, values=keys), timeout=TIMEOUT_SECONDS)
        return {m.value: m.sec_id for m in r.matches}, list(r.unknown)

    def instruments(self) -> dict[int, InstrumentInfo]:
        from app.grpc_gen import securities_pb2 as pb

        r = self._sec.ListInstruments(pb.ListInstrumentsRequest(include_inactive=True), timeout=TIMEOUT_SECONDS)
        return {i.sec_id: InstrumentInfo(i.short_name, i.type, i.status) for i in r.instruments}


class GrpcCalendars:
    """calendar-svc (Calendars) over gRPC, for coverage. Use as a context manager."""

    def __init__(self, target: str, timeout: float = TIMEOUT_SECONDS):
        self.target = target
        self.timeout = timeout

    def __enter__(self):
        import grpc

        from app.grpc_gen import calendars_pb2_grpc

        self._channel = grpc.insecure_channel(self.target)
        self._stub = calendars_pb2_grpc.CalendarsStub(self._channel)
        return self

    def __exit__(self, *exc):
        self._channel.close()

    def covered_years(self, calendar: str) -> set[int]:
        from app.grpc_gen import calendars_pb2 as pb

        r = self._stub.Coverage(pb.CoverageRequest(calendar=calendar), timeout=self.timeout)
        return {y.year for y in r.years if y.kind != "projected"}

    def closed_days(self, calendar: str, start, end) -> set:
        from datetime import date

        from app.grpc_gen import calendars_pb2 as pb

        r = self._stub.Closes(pb.ClosesRequest(calendar=calendar, start=start.isoformat(), end=end.isoformat()),
                              timeout=self.timeout)
        return {date.fromisoformat(c.date) for c in r.closes if c.status == "closed"}
