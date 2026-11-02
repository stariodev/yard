"""The http-nu detector: a cross.stream (xs) app is one serve.nu, run by one static binary."""

from app.server.detect.base import DOWNLOADS, Plan, Repo, pin, plan_template
from app.shared import YardError

LANGUAGE = "nushell"
MARKERS = ("serve.nu",)


def detect(repo: Repo, name: str) -> Plan | None:
    if "{|" not in repo.text("serve.nu"):
        raise YardError(
            502,
            "detect",
            "found serve.nu but it has no handler closure",
            help_text="http-nu runs serve.nu; its last expression is {|req| ...}. Or add a Containerfile.",
        )
    download = DOWNLOADS["http-nu"]
    return plan_template(
        "yard:http-nu",
        "http-nu: serve.nu",
        name,
        template="http-nu",
        fetch=pin("alpine:3.22"),
        static=pin("gcr.io/distroless/static-debian12"),
        version=download["version"],
        sha_amd64=download["sha256"]["amd64"],
        sha_arm64=download["sha256"]["arm64"],
    )
