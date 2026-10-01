"""The logging contract: stderr only, structured fields, bound context.

stdout is the MCP server's protocol channel, so the most important property
here is that a configured logger never writes a byte to it.

    pytest tests/test_log.py -v
"""

import json
import logging

import pytest

from tributary import log


@pytest.fixture
def reset_logging():
    yield
    for name in (log.ROOT, "typesafe_sdk"):
        lg = logging.getLogger(name)
        for h in [h for h in lg.handlers if not isinstance(h, logging.NullHandler)]:
            lg.removeHandler(h)
            h.close()
        lg.propagate = True
    log._configured = False
    log._defaults.clear()


def test_logs_go_to_stderr_never_stdout(capfd, reset_logging):
    log.setup(level="DEBUG", fmt="text", force=True)
    log.get_logger("memory").info("learn", action="inserted")
    out, err = capfd.readouterr()
    assert out == ""
    assert "learn action=inserted" in err


def test_json_lines_carry_context_and_fields(capfd, reset_logging):
    log.setup(level="INFO", fmt="json", force=True)
    log.set_defaults(agent="agent-a")
    with log.context(op="learn-1234abcd"):
        log.get_logger("memory").info("learn", action="superseded", skipped=None)
    line = json.loads(capfd.readouterr().err.strip())
    assert line["msg"] == "learn" and line["logger"] == "tributary.memory"
    assert line["agent"] == "agent-a" and line["op"] == "learn-1234abcd"
    assert line["action"] == "superseded"
    assert "skipped" not in line  # None-valued fields are dropped


def test_context_is_scoped_to_the_block(capfd, reset_logging):
    log.setup(level="INFO", fmt="json", force=True)
    with log.context(op="inner"):
        pass
    log.get_logger("db").info("after")
    assert "op" not in json.loads(capfd.readouterr().err.strip())


def test_level_filters_and_does_not_propagate_to_root(capfd, reset_logging):
    log.setup(level="WARNING", fmt="text", force=True)
    lg = log.get_logger("llm")
    lg.info("quiet")
    lg.warning("loud")
    err = capfd.readouterr().err
    assert "loud" in err and "quiet" not in err
    assert logging.getLogger(log.ROOT).propagate is False  # no double lines under Lambda


def test_text_output_truncates_long_untrusted_values(capfd, reset_logging):
    log.setup(level="INFO", fmt="text", force=True)
    log.get_logger("memory").info("learn", content="x" * 5000)
    err = capfd.readouterr().err
    assert len(err) < 600


def test_file_sink_writes_json(tmp_path, capfd, reset_logging):
    path = tmp_path / "tributary.log"
    log.setup(level="INFO", fmt="text", file=str(path), force=True)
    log.get_logger("jev").info("jev verdict", relation="novel")
    for h in logging.getLogger(log.ROOT).handlers:
        h.flush()
    record = json.loads(path.read_text(encoding="utf-8").strip())
    assert record["relation"] == "novel"
