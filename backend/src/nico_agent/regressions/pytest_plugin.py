"""Fail a selected regression run when pytest skips a registered case."""

from __future__ import annotations

from typing import Any

import pytest


class FailOnSkippedRegressions:
    """Turn skipped registered cases into a failing regression run."""

    def __init__(self) -> None:
        self.skipped_nodeids: set[str] = set()

    def pytest_runtest_logreport(self, report: Any) -> None:
        if report.skipped:
            self.skipped_nodeids.add(report.nodeid)

    def pytest_sessionfinish(
        self,
        session: pytest.Session,
        exitstatus: int | pytest.ExitCode,
    ) -> None:
        if self.skipped_nodeids and exitstatus == pytest.ExitCode.OK:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED

    def pytest_terminal_summary(self, terminalreporter: Any) -> None:
        if self.skipped_nodeids:
            terminalreporter.write_sep(
                "!",
                "registered regressions skipped: " + ", ".join(sorted(self.skipped_nodeids)),
                red=True,
            )


def pytest_configure(config: pytest.Config) -> None:
    config.pluginmanager.register(
        FailOnSkippedRegressions(),
        "nico-fail-on-skipped-regressions",
    )
