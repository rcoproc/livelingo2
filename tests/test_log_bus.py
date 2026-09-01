"""Unit tests for localhost log bus (secondary panel viewers)."""

from __future__ import annotations

import threading
import time

import pytest

from livelingo.log_bus import (
    LogBusServer,
    decode_event,
    encode_event,
    format_view_markup,
    iter_events,
    normalize_view_panel,
    panel_matches,
)


def test_normalize_view_panel_aliases():
    assert normalize_view_panel("coach") == "coach"
    assert normalize_view_panel("entrevista") == "coach"
    assert normalize_view_panel("lc") == "lc"
    assert normalize_view_panel("voz") == "main"
    assert normalize_view_panel("tradução") == "main"
    with pytest.raises(ValueError):
        normalize_view_panel("webcam")


def test_panel_matches():
    assert panel_matches("coach", "coach")
    assert panel_matches("entrevista", "coach")
    assert panel_matches("main", "voz")
    assert panel_matches("lc", "lc")
    assert not panel_matches("coach", "lc")
    assert not panel_matches("main", "coach")


def test_encode_decode_roundtrip():
    raw = encode_event("rich", "Hello [bold]x[/]", "coach", t=1.5)
    assert raw.endswith(b"\n")
    ev = decode_event(raw)
    assert ev is not None
    assert ev["kind"] == "rich"
    assert ev["panel"] == "coach"
    assert "Hello" in ev["text"]
    assert ev["t"] == 1.5
    assert decode_event(b"not-json") is None
    assert decode_event("") is None


def test_format_view_markup_clear_and_rich():
    assert format_view_markup("clear", "x") is None
    assert format_view_markup("rich", "[bold]hi[/]") == "[bold]hi[/]"
    out = format_view_markup("info", "hello")
    assert out is not None and "hello" in out and "[cyan]" in out


def test_log_bus_server_publish_to_client():
    server = LogBusServer(host="127.0.0.1", port=0, max_buf=50)
    host, port = server.start()
    assert port > 0
    stop = threading.Event()
    got: list[dict] = []

    def _reader():
        for ev in iter_events(
            host, port, stop_event=stop, reconnect=False, reconnect_sleep_s=0.1
        ):
            got.append(ev)
            if len(got) >= 2:
                stop.set()
                break

    th = threading.Thread(target=_reader, daemon=True)
    th.start()
    # Wait until at least one client is accepted
    deadline = time.time() + 2.0
    while server.client_count() < 1 and time.time() < deadline:
        time.sleep(0.05)
    server.publish("info", "one", "coach")
    server.publish("rich", "two", "lc")
    th.join(timeout=2.0)
    stop.set()
    server.stop()
    assert len(got) >= 1
    panels = {e["panel"] for e in got}
    assert "coach" in panels or "lc" in panels


def test_log_bus_drop_oldest_does_not_block():
    server = LogBusServer(host="127.0.0.1", port=0, max_buf=8)
    host, port = server.start()
    # Connect a client that never reads → writer buffer fills → drop-oldest
    import socket

    sock = socket.create_connection((host, port), timeout=2.0)
    try:
        deadline = time.time() + 2.0
        while server.client_count() < 1 and time.time() < deadline:
            time.sleep(0.02)
        assert server.client_count() >= 1
        t0 = time.perf_counter()
        for i in range(40):
            server.publish("info", f"line-{i}", "main")
        # Must return quickly (no pipeline stall)
        assert (time.perf_counter() - t0) < 1.0
    finally:
        sock.close()
        server.stop()


def test_ui_tee_publishes_when_bus_set():
    from livelingo import ui

    server = LogBusServer(host="127.0.0.1", port=0, max_buf=50)
    host, port = server.start()
    stop = threading.Event()
    got: list[dict] = []

    def _reader():
        for ev in iter_events(host, port, stop_event=stop, reconnect=False):
            got.append(ev)
            if any(e.get("panel") == "coach" for e in got):
                stop.set()
                break

    th = threading.Thread(target=_reader, daemon=True)
    th.start()
    deadline = time.time() + 2.0
    while server.client_count() < 1 and time.time() < deadline:
        time.sleep(0.05)

    prev_sink = ui.get_log_sink()
    prev_bus = ui.get_log_bus()
    try:
        ui.set_log_sink(lambda *a, **k: None)
        ui.set_log_bus(server)
        ui.info("coach hello", panel="coach")
    finally:
        ui.set_log_bus(prev_bus)
        ui.set_log_sink(prev_sink)
        stop.set()
        th.join(timeout=2.0)
        server.stop()

    assert any(
        e.get("panel") == "coach" and "coach hello" in (e.get("text") or "")
        for e in got
    ), got
