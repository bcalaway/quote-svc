import asyncio

import grpc
from grpc_health.v1 import health_pb2, health_pb2_grpc

from app.grpc_server import QUOTES, start_grpc_server


async def _call(fn):
    # Port 0: the OS picks a free port, so tests never collide with 9090.
    server, port = await start_grpc_server(0)
    try:
        async with grpc.aio.insecure_channel(f"localhost:{port}") as channel:
            return await fn(channel)
    finally:
        await server.stop(grace=None)


def test_health_reports_serving():
    async def check(channel):
        stub = health_pb2_grpc.HealthStub(channel)
        overall = await stub.Check(health_pb2.HealthCheckRequest(service=""))
        q = await stub.Check(health_pb2.HealthCheckRequest(service=QUOTES))
        return overall.status, q.status

    serving = health_pb2.HealthCheckResponse.SERVING
    assert asyncio.run(_call(check)) == (serving, serving)


def test_quotes(migrated_db):
    from app import db
    from app.grpc_gen import quotes_pb2 as pb
    from app.grpc_gen import quotes_pb2_grpc
    from app.load import run_load
    from tests.test_load import _up

    with db.session() as s:
        run_load(s, _up())

    async def read(channel):
        stub = quotes_pb2_grpc.QuotesStub(channel)
        series = await stub.GetSeries(pb.GetSeriesRequest(sec_ids=[12], start="2026-10-01", end="2026-10-31"))
        curve = await stub.GetCurve(pb.GetCurveRequest(sec_ids=[2, 12]))
        cmp = await stub.CompareSources(pb.CompareSourcesRequest(sec_ids=[12], start="2026-10-01", end="2026-10-31",
                                                                 only_differences=True))
        latest = await stub.GetLatest(pb.GetLatestRequest(sec_ids=[12]))
        try:
            await stub.GetSeries(pb.GetSeriesRequest(sec_ids=[12], start="soon", end="2026-10-31"))
            bad = None
        except grpc.aio.AioRpcError as e:
            bad = e.code()
        return series, curve, cmp, latest, bad

    series, curve, cmp, latest, bad = asyncio.run(_call(read))
    assert [p.value for p in series.series[0].points] == ["0.041", "0.0412"]
    assert curve.as_of == "2026-10-02" and list(curve.missing) == ["UST-1.5M-CMT"]
    assert len(cmp.rows) == 1 and cmp.rows[0].differs
    assert latest.latest[0].short_name == "UST-10Y-CMT" and latest.latest[0].value == "0.0412"
    assert bad == grpc.StatusCode.INVALID_ARGUMENT
