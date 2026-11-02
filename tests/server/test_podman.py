from datetime import UTC, datetime

from app.server.podman import (
    parse_health,
    parse_images,
    parse_info,
    parse_port,
    parse_ps,
    parse_stats,
    parse_system_df,
)
from app.shared import stamp as format_stamp


def _unix(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)


def test_parse_stats_and_ps():
    # Podman 4.9 --format json prints snake_case keys; table headers also parse.
    samples = parse_stats('[{"name":"heldso","cpu_percent":"1.5%","mem_usage":"20MB / 1GB"}]')
    assert samples["heldso"].cpu == "1.5%"
    assert samples["heldso"].mem == "20MB / 1GB"
    assert samples["heldso"].cpu_percent == 1.5
    assert samples["heldso"].mem_bytes == 20_000_000
    old = parse_stats('[{"Name":"heldso","CPUPerc":"1.5%","MemUsage":"20MB / 1GB"}]')
    assert old["heldso"].cpu == "1.5%"
    raw = """[
        {"Names":["heldso"],"State":"running","Status":"Up 2 minutes (healthy)",
         "StartedAt":1710000000,"ExitCode":0,"Restarts":3},
        {"Names":["shop"],"State":"exited","Status":"Exited (1) 4 minutes ago","ExitedAt":1710000200,"ExitCode":1}
    ]"""
    got = parse_ps(raw)
    assert got["heldso"].running
    assert got["heldso"].restarts == 3
    assert got["shop"].restarts == 0
    assert got["heldso"].health == "healthy"
    assert got["heldso"].started == _unix(1710000000)
    assert not got["shop"].running
    assert got["shop"].exit_code == 1
    assert got["shop"].health == ""
    assert got["shop"].exited == _unix(1710000200)


def test_parse_ps_reads_name_and_labels():
    raw = """[
        {"Id":"app","Names":["heldso-app-1"],
         "Labels":{"com.docker.compose.project":"heldso","com.docker.compose.service":"app"}},
        {"Id":"build","Names":["deploy-backup"]},
        {"Id":"bare","Names":["caddy"]}
    ]"""
    got = parse_ps(raw)

    # An app container: compose's project is the app, its service is the service.
    assert got["heldso-app-1"].app == "heldso"
    assert got["heldso-app-1"].service == "app"
    assert got["heldso-app-1"].role == "app"

    # An apply container is deploy-<app>.
    assert got["deploy-backup"].app == "backup"
    assert got["deploy-backup"].role == "deploy"

    # A container with no compose labels has no app.
    assert got["caddy"].app == ""
    assert got["caddy"].service == ""
    assert got["caddy"].role == "app"


def test_parse_info():
    raw = (
        '{"host":{"cpus":12,"memTotal":67320864768},'
        '"store":{"graphRootAllocated":467909804032},"version":{"Version":"5.3.1"}}'
    )
    host = parse_info(raw)
    assert host.cpus == 12
    assert host.version == "5.3.1"
    assert host.mem == 67320864768
    assert host.disk == 467909804032
    assert parse_info("nope").cpus == 0


def test_parse_images():
    raw = (
        '[{"Names": ["localhost/app:abc", "localhost/app:latest"], "Created": 1710000000, "Size": 400},'
        ' {"Names": ["<none>"], "Created": "bad", "Size": "x"}]'
    )
    found = parse_images(raw)
    assert [image.name for image in found] == ["localhost/app:abc", "localhost/app:latest", "<none>"]
    assert found[0].created == _unix(1710000000)
    assert found[0].size == 400
    assert found[2].created is None
    assert found[2].size == 0


def test_parse_system_df():
    raw = '[{"Type":"Images","RawSize":2359570674,"RawReclaimable":1572938,"Size":"2.36GB"}]'
    assert parse_system_df(raw) == (2359570674, 1572938)
    # The Containers and Volumes rows are ignored; a missing Images row is None.
    assert parse_system_df('[{"Type":"Containers","RawSize":9},{"Type":"Volumes","RawSize":8}]') is None
    assert parse_system_df("") is None
    assert parse_system_df("[]") is None
    assert parse_system_df("not json") is None
    assert parse_system_df('{"Type":"Images","RawSize":1}') is None
    # Missing or non-numeric fields parse as zero.
    assert parse_system_df('[{"Type":"Images"}]') == (0, 0)


def test_parse_health():
    raw = (
        '[{"Name":"/heldso-app-1","Config":{"Healthcheck":{"Test":["CMD-SHELL","curl -fs localhost:8000"],'
        '"Interval":10000000000}},"State":{"Health":{"Status":"healthy"}}}]'
    )
    got = parse_health(raw)
    assert got["heldso-app-1"].test == ["CMD-SHELL", "curl -fs localhost:8000"]
    assert got["heldso-app-1"].interval == 10_000_000_000
    assert got["heldso-app-1"].status == "healthy"

    # No Healthcheck, or an OCI image that dropped it: empty test, no status.
    bare = parse_health('[{"Name":"heldso-app-1","Config":{},"State":{}}]')
    assert bare["heldso-app-1"].test == []
    assert bare["heldso-app-1"].interval == 0
    assert bare["heldso-app-1"].status == ""

    assert parse_health("[]") == {}
    assert parse_health("") == {}


def test_format_stamp():
    assert format_stamp(datetime(2026, 10, 5, 12, 0, 0, tzinfo=UTC)) == "2026-10-05T12:00:00Z"
    assert format_stamp(datetime(2026, 10, 5, 12, 0, 0, 120000, tzinfo=UTC)) == "2026-10-05T12:00:00.12Z"


def test_parse_ps_keeps_the_yard_labels_of_a_deploy_container():
    raw = """[
        {"Id":"d","Names":["deploy-heldso"],
         "Labels":{"yard.sha":"abc","yard.cause":"push","yard.error":"","other":"x"}},
        {"Id":"a","Names":["heldso-app-1"],"Labels":{"yard.sha":"ignored"}}
    ]"""
    got = parse_ps(raw)
    assert got["deploy-heldso"].labels == {"sha": "abc", "cause": "push"}
    assert got["heldso-app-1"].labels == {}


def test_parse_port():
    assert parse_port('[{"Config": {"ExposedPorts": {"3001/tcp": {}}}}]') == 3001
    assert parse_port('[{"Config": {"ExposedPorts": {"3001/tcp": {}, "80/tcp": {}}}}]') == 0
    assert parse_port("[]") == 0
