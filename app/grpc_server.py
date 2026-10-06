"""gRPC server (ADR-0020): quote-svc's service-to-service API.

Internal-only: listens on GRPC_PORT (9090 by convention), plaintext, reachable
only by other containers on the `home-platform` Docker network at
`quote-svc:9090`. Never routed through Traefik. Runs in the same process
and event loop as the FastAPI app (started from its lifespan in app/main.py).

Serves `quote_svc.Quotes` (proto/quotes.proto): series, bars, curves, source
comparisons and latest values, for mkt-api. Also the standard
grpc.health.v1.Health service.
"""

import asyncio
from datetime import date

import grpc
from grpc_health.v1 import health, health_pb2, health_pb2_grpc

from app import db, quotes
from app.grpc_gen import quotes_pb2, quotes_pb2_grpc

QUOTES = quotes_pb2.DESCRIPTOR.services_by_name["Quotes"].full_name


def _date(v: str, name: str) -> date:
    try:
        return date.fromisoformat(v)
    except ValueError:
        raise ValueError(f"{name} {v!r} isn't YYYY-MM-DD") from None


def _series(r) -> quotes_pb2.GetSeriesResponse:
    start, end = _date(r.start, "start"), _date(r.end, "end")
    with db.session() as s:
        rows = quotes.series(s, list(r.sec_ids), start, end, r.field or "yield", r.source)
    return quotes_pb2.GetSeriesResponse(series=[
        quotes_pb2.Series(sec_id=x["sec_id"], short_name=x["short_name"],
                          points=[quotes_pb2.Point(**p) for p in x["points"]]) for x in rows])


def _bars(r) -> quotes_pb2.GetBarsResponse:
    start, end = _date(r.start, "start"), _date(r.end, "end")
    with db.session() as s:
        rows = quotes.bars(s, list(r.sec_ids), start, end, r.interval or "day", r.field or "yield", r.source)
    return quotes_pb2.GetBarsResponse(series=[
        quotes_pb2.BarSeries(sec_id=x["sec_id"], short_name=x["short_name"],
                             bars=[quotes_pb2.Bar(**b) for b in x["bars"]]) for x in rows])


def _curve(r) -> quotes_pb2.GetCurveResponse:
    as_of = _date(r.as_of, "as_of") if r.as_of else None
    with db.session() as s:
        c = quotes.curve(s, list(r.sec_ids), as_of, r.field or "yield")
    return quotes_pb2.GetCurveResponse(as_of=c["as_of"], missing=c["missing"],
                                       points=[quotes_pb2.CurvePoint(**p) for p in c["points"]])


def _compare(r) -> quotes_pb2.CompareSourcesResponse:
    start, end = _date(r.start, "start"), _date(r.end, "end")
    with db.session() as s:
        rows = quotes.compare(s, list(r.sec_ids), start, end, r.field or "yield", r.only_differences)
    return quotes_pb2.CompareSourcesResponse(rows=[
        quotes_pb2.Comparison(sec_id=x["sec_id"], short_name=x["short_name"], as_of=x["as_of"], differs=x["differs"],
                              values=[quotes_pb2.SourceValue(**v) for v in x["values"]]) for x in rows])


def _latest(r) -> quotes_pb2.GetLatestResponse:
    with db.session() as s:
        rows = quotes.latest(s, list(r.sec_ids), r.field or "yield")
    return quotes_pb2.GetLatestResponse(latest=[quotes_pb2.Latest(**x) for x in rows])


class Quotes(quotes_pb2_grpc.QuotesServicer):
    # The database work is synchronous SQLAlchemy, so it runs in a thread.
    async def _run(self, fn, request, context):
        try:
            return await asyncio.to_thread(fn, request)
        except ValueError as e:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(e))

    async def GetSeries(self, request, context):
        return await self._run(_series, request, context)

    async def GetBars(self, request, context):
        return await self._run(_bars, request, context)

    async def GetCurve(self, request, context):
        return await self._run(_curve, request, context)

    async def CompareSources(self, request, context):
        return await self._run(_compare, request, context)

    async def GetLatest(self, request, context):
        return await self._run(_latest, request, context)


async def start_grpc_server(port: int) -> tuple[grpc.aio.Server, int]:
    """Start the server; returns it and the bound port (port 0 picks a free one)."""
    server = grpc.aio.server()
    quotes_pb2_grpc.add_QuotesServicer_to_server(Quotes(), server)

    health_servicer = health.aio.HealthServicer()
    health_pb2_grpc.add_HealthServicer_to_server(health_servicer, server)

    bound = server.add_insecure_port(f"[::]:{port}")
    await server.start()
    # "" is the overall server status; each service also reports its own.
    for service in ("", QUOTES):
        await health_servicer.set(service, health_pb2.HealthCheckResponse.SERVING)
    return server, bound
