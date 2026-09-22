"""Collector parsers, capability handling and probe budgets.

Platform output is pinned as fixtures rather than read from the machine
running the tests. That is the only way to test the Windows parser on Linux
and the macOS parser on Windows, and a cross-platform agent whose parsers are
only exercised on the developer's own OS is a cross-platform agent in name
only.

Tests marked ``live`` do touch the real network and are deselected by
default.
"""

from __future__ import annotations

import time

import pytest

from netpulse.collectors import gateway, netinfo, path, wifi
from netpulse.collectors.base import Capability, Collector, CollectorResult, percentile
from netpulse.collectors.captive import detect_portal
from netpulse.collectors.dns import DnsCollector
from netpulse.collectors.gateway import GatewayCollector
from netpulse.collectors.https_probe import HttpsCollector, probe_url
from netpulse.collectors.registry import build_budget, build_collectors, capability_report
from netpulse.collectors.shell import run
from netpulse.collectors.system import _parse_netstat_tcp, read_tcp_retransmissions
from netpulse.core.ratelimit import ProbeBudget, TokenBucket

# ----------------------------------------------------------------- fixtures

NETSH_CONNECTED = """
There is 1 interface on the system:

    Name                   : Wi-Fi
    Description            : Intel(R) Wi-Fi 6 AX201
    GUID                   : 7ffb0418-85d2-4b48-a75b-a2eae63ee2c9
    Physical address       : d0:39:57:b0:5e:19
    State                  : connected
    SSID                   : HomeNet
    AP BSSID               : 72:7a:38:3b:7a:ba
    Band                   : 5 GHz
    Channel                : 44
    Radio type             : 802.11ax
    Receive rate (Mbps)    : 300.2
    Transmit rate (Mbps)   : 300.2
    Signal                 : 84%
    Rssi                   : -56
"""

NETSH_LEGACY_NO_RSSI = """
    Name                   : Wi-Fi
    State                  : connected
    SSID                   : HomeNet
    AP BSSID               : 72:7a:38:3b:7a:ba
    Channel                : 6
    Receive rate (Mbps)    : 72
    Signal                 : 40%
"""

NETSH_DISCONNECTED = """
    Name                   : Wi-Fi
    State                  : disconnected
"""

IW_LINK = """
Connected to 72:7a:38:3b:7a:ba (on wlp2s0)
        SSID: HomeNet
        freq: 5180
        RX: 91234 bytes (812 packets)
        TX: 44321 bytes (402 packets)
        signal: -47 dBm
        tx bitrate: 390.0 MBit/s VHT-MCS 9 80MHz short GI VHT-NSS 2
"""

IW_STATION = """
Station 72:7a:38:3b:7a:ba (on wlp2s0)
        rx packets:     12000
        tx packets:     4000
        tx retries:     600
        tx failed:      20
        signal:         -47 dBm
"""

PROC_WIRELESS = """Inter-| sta-|   Quality        |   Discarded packets               | Missed | WE
 face | tus | link level noise |  nwid  crypt   frag  retry   misc | beacon | 22
wlp2s0: 0000   56.  -54.  -95.       0      0      0      0      0        0
"""

AIRPORT_OUTPUT = """
     agrCtlRSSI: -51
     agrExtRSSI: 0
    agrCtlNoise: -92
     agrExtNoise: 0
           state: running
         op mode: station
      lastTxRate: 468
         maxRate: 1300
            BSSID: 72:7a:38:3b:7a:ba
            SSID: HomeNet
         channel: 44,80
"""

PING_WINDOWS = """
Pinging 192.168.1.1 with 32 bytes of data:
Reply from 192.168.1.1: bytes=32 time=3ms TTL=64
Reply from 192.168.1.1: bytes=32 time<1ms TTL=64
Reply from 192.168.1.1: bytes=32 time=5ms TTL=64
Request timed out.

Ping statistics for 192.168.1.1:
    Packets: Sent = 4, Received = 3, Lost = 1 (25% loss),
"""

PING_POSIX = """
PING 192.168.1.1 (192.168.1.1) 56(84) bytes of data.
64 bytes from 192.168.1.1: icmp_seq=1 ttl=64 time=2.41 ms
64 bytes from 192.168.1.1: icmp_seq=2 ttl=64 time=3.02 ms
64 bytes from 192.168.1.1: icmp_seq=3 ttl=64 time=2.77 ms

--- 192.168.1.1 ping statistics ---
4 packets transmitted, 3 received, 25% packet loss
"""

TRACERT_WINDOWS = """
Tracing route to 1.1.1.1 over a maximum of 12 hops

  1     2 ms     1 ms     1 ms  192.168.1.1
  2     9 ms     8 ms     8 ms  10.50.0.1
  3     *        *        *     Request timed out.
  4    14 ms    13 ms    13 ms  1.1.1.1

Trace complete.
"""

TRACEROUTE_POSIX = """
traceroute to 1.1.1.1 (1.1.1.1), 12 hops max, 60 byte packets
 1  192.168.1.1  1.204 ms
 2  10.50.0.1  8.441 ms
 3  *
 4  1.1.1.1  13.882 ms
"""

NETSTAT_MACOS = """
tcp:
        981234 packets sent
                874123 data packets (1234567 bytes)
                4210 data packets (98765 bytes) retransmitted
        875432 packets received
"""


# --------------------------------------------------------------------- Wi-Fi


def test_netsh_parser_reads_rssi_directly():
    fields = wifi._parse_netsh(NETSH_CONNECTED)
    assert fields["wifi_rssi_dbm"] == -56.0
    assert fields["wifi_signal_pct"] == 84.0
    assert fields["wifi_link_mbps"] == pytest.approx(300.2)
    assert fields["wifi_channel"] == 44.0
    assert fields["wifi_band_ghz"] == 5.0
    assert "bssid_hash" in fields
    assert "HomeNet" not in str(fields), "the SSID must never survive parsing"


def test_netsh_parser_derives_rssi_on_older_builds():
    fields = wifi._parse_netsh(NETSH_LEGACY_NO_RSSI)
    assert "wifi_rssi_dbm" not in fields
    assert fields["wifi_signal_pct"] == 40.0
    assert wifi.signal_pct_to_dbm(40.0) == -80.0


def test_netsh_parser_ignores_a_disconnected_radio():
    assert wifi._parse_netsh(NETSH_DISCONNECTED) == {}


def test_iw_link_parser():
    fields = wifi._parse_iw_link(IW_LINK)
    assert fields["wifi_rssi_dbm"] == -47.0
    assert fields["wifi_link_mbps"] == pytest.approx(390.0)
    assert fields["wifi_band_ghz"] == 5.0
    assert fields["wifi_channel"] == 36.0


def test_iw_link_parser_handles_disconnected():
    assert wifi._parse_iw_link("Not connected.") == {}


def test_iw_station_parser_computes_retry_rate():
    fields = wifi._parse_iw_station(IW_STATION)
    assert fields["wifi_tx_retry_rate"] == pytest.approx((600 + 20) / 4000)


def test_proc_wireless_fallback(monkeypatch, tmp_path):
    target = tmp_path / "wireless"
    target.write_text(PROC_WIRELESS, encoding="utf-8")
    real_open = open

    def fake_open(path, *args, **kwargs):
        if str(path) == "/proc/net/wireless":
            return real_open(target, *args, **kwargs)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fake_open)
    fields = wifi._parse_proc_wireless()
    assert fields["wifi_rssi_dbm"] == -54.0
    assert fields["wifi_noise_dbm"] == -95.0


def test_airport_parser():
    fields = wifi._parse_airport(AIRPORT_OUTPUT)
    assert fields["wifi_rssi_dbm"] == -51.0
    assert fields["wifi_noise_dbm"] == -92.0
    assert fields["wifi_link_mbps"] == 468.0
    assert fields["wifi_channel"] == 44.0


@pytest.mark.parametrize("channel,band", [(1, 2.4), (11, 2.4), (36, 5.0), (149, 5.0), (200, 6.0)])
def test_band_for_channel(channel, band):
    assert wifi.band_for_channel(channel) == band


@pytest.mark.parametrize("mhz,channel", [(2412, 1), (2437, 6), (2484, 14), (5180, 36), (5955, 1)])
def test_channel_for_frequency(mhz, channel):
    assert wifi.channel_for_frequency(mhz) == channel


def test_signal_percent_mapping_is_bounded():
    assert wifi.signal_pct_to_dbm(100.0) == -50.0
    assert wifi.signal_pct_to_dbm(0.0) == -100.0
    assert -100.0 <= wifi.signal_pct_to_dbm(55.0) <= -50.0


# ------------------------------------------------------------------ gateway


def test_ping_parser_windows(monkeypatch):
    monkeypatch.setattr(gateway.sys, "platform", "win32")
    times = gateway.parse_ping_times(PING_WINDOWS)
    assert times == [3.0, 0.5, 5.0], "sub-millisecond replies map to 0.5, not 0"


def test_ping_parser_posix(monkeypatch):
    monkeypatch.setattr(gateway.sys, "platform", "linux")
    assert gateway.parse_ping_times(PING_POSIX) == [2.41, 3.02, 2.77]


@pytest.mark.parametrize(
    "platform,expected_flag", [("win32", "-n"), ("darwin", "-c"), ("linux", "-c")]
)
def test_ping_command_per_platform(monkeypatch, platform, expected_flag):
    monkeypatch.setattr(gateway.sys, "platform", platform)
    command = gateway.ping_command("192.168.1.1", 4, 5.0)
    assert command[0] == "ping"
    assert expected_flag in command
    assert command[-1] == "192.168.1.1"


def test_gateway_reports_loss_when_a_working_gateway_goes_quiet(monkeypatch, config):
    """Total loss is only an outage once the gateway has answered at least once.

    This test used to send silence from the very first round and assert 100%
    loss, which is what made the agent declare "your router is not
    responding" on an iPhone hotspot while DNS and HTTPS were both fine.
    """
    monkeypatch.setattr(
        netinfo, "default_route", lambda **_: netinfo.RouteInfo(gateway="192.168.1.1")
    )
    replies = iter(["Reply from 192.168.1.1: bytes=32 time=2ms TTL=64\n", "", "", ""])
    monkeypatch.setattr(gateway, "run", lambda *a, **k: _fake_command(next(replies, "")))
    collector = GatewayCollector(config, build_budget(config))
    collector._capability = Capability(True)

    assert collector.collect().values["gw_rtt_ms"] == 2.0

    result = collector.collect()
    assert result.values["gw_loss_rate"] == 1.0
    assert result.values["gw_rtt_ms"] == config.probes.timeout_s * 1000.0


def test_gateway_skips_without_a_default_route(monkeypatch, config):
    monkeypatch.setattr(netinfo, "default_route", lambda **_: netinfo.RouteInfo(gateway=None))
    collector = GatewayCollector(config, build_budget(config))
    collector._capability = Capability(True)
    assert "gateway" in collector.collect().skipped_reason


# --------------------------------------------------------------------- path


def test_tracert_parser():
    hops = path.parse_traceroute(TRACERT_WINDOWS)
    assert [hop.index for hop in hops] == [1, 2, 3, 4]
    assert hops[0].rtt_ms == 1.0, "the minimum of the three probes is used"
    assert hops[2].rtt_ms is None
    assert hops[3].address == "1.1.1.1"


def test_traceroute_posix_parser():
    hops = path.parse_traceroute(TRACEROUTE_POSIX)
    assert len(hops) == 4
    assert hops[1].rtt_ms == pytest.approx(8.441)


def test_path_signature_detects_a_reroute():
    first = path.parse_traceroute(TRACERT_WINDOWS)
    rerouted = path.parse_traceroute(TRACERT_WINDOWS.replace("10.50.0.1", "10.60.0.1"))
    assert path.path_signature(first) != path.path_signature(rerouted)
    assert path.path_signature(first) == path.path_signature(path.parse_traceroute(TRACERT_WINDOWS))


def test_path_signature_is_a_number_not_an_address():
    signature = path.path_signature(path.parse_traceroute(TRACERT_WINDOWS))
    assert isinstance(signature, float)
    assert "192.168" not in str(signature)


@pytest.mark.parametrize("binary,flag", [("tracert", "-d"), ("traceroute", "-n")])
def test_traceroute_command_suppresses_name_lookup(binary, flag):
    assert flag in path.traceroute_command(binary, "1.1.1.1")


# -------------------------------------------------------------------- system


def test_netstat_macos_parser():
    assert _parse_netstat_tcp(NETSTAT_MACOS) == (981234, 4210)


def test_netstat_parser_returns_none_on_junk():
    assert _parse_netstat_tcp("nothing useful here") is None


def test_retransmission_counters_are_monotonic_or_none():
    counters = read_tcp_retransmissions()
    if counters is not None:
        out_segs, retrans = counters
        assert out_segs >= 0 and retrans >= 0
        assert retrans <= out_segs


# --------------------------------------------------------------- route logic


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Wi-Fi", netinfo.IFACE_WIFI),
        ("wlp3s0", netinfo.IFACE_WIFI),
        ("Ethernet", netinfo.IFACE_ETHERNET),
        ("enp0s31f6", netinfo.IFACE_ETHERNET),
        ("tun0", netinfo.IFACE_OTHER),
        (None, netinfo.IFACE_OTHER),
    ],
)
def test_interface_classification(name, expected):
    assert netinfo.classify_interface(name) == expected


@pytest.mark.parametrize(
    "name,is_vpn",
    [("tun0", True), ("wg0", True), ("utun3", True), ("NordLynx", True), ("Wi-Fi", False)],
)
def test_vpn_interface_detection(name, is_vpn):
    assert netinfo.is_vpn_interface(name) is is_vpn


def test_linux_route_parser(monkeypatch, tmp_path):
    table = (
        "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
        "wlp2s0\t00000000\t0101A8C0\t0003\t0\t0\t600\t00000000\t0\t0\t0\n"
        "wlp2s0\t0001A8C0\t00000000\t0001\t0\t0\t600\t00FFFFFF\t0\t0\t0\n"
    )
    target = tmp_path / "route"
    target.write_text(table, encoding="utf-8")
    real_open = open
    monkeypatch.setattr(
        "builtins.open",
        lambda p, *a, **k: (
            real_open(target, *a, **k) if str(p) == "/proc/net/route" else real_open(p, *a, **k)
        ),
    )
    assert netinfo._linux_default_route() == ("192.168.1.1", "wlp2s0")


# ------------------------------------------------------------------- budget


def test_token_bucket_limits_and_refills():
    bucket = TokenBucket(capacity=3, refill_per_second=1.0)
    now = 1000.0
    assert [bucket.try_acquire(now=now) for _ in range(4)] == [True, True, True, False]
    assert bucket.try_acquire(now=now + 1.5) is True
    assert bucket.denied == 1


def test_budget_pause_blocks_every_kind():
    budget = ProbeBudget(
        https_per_minute=10, dns_per_minute=10, icmp_per_minute=10, traceroute_per_hour=10
    )
    assert budget.acquire("https")
    budget.pause()
    assert not budget.acquire("https")
    assert not budget.acquire("icmp")
    assert not budget.acquire("traceroute")
    budget.resume()
    assert budget.acquire("https")


def test_budget_stats_report_denials():
    budget = ProbeBudget(
        https_per_minute=1, dns_per_minute=1, icmp_per_minute=1, traceroute_per_hour=1
    )
    budget.acquire("https")
    budget.acquire("https")
    assert budget.stats()["total_denied"] == 1


def test_paused_agent_emits_nothing(config):
    """PRD Appendix B: pause must stop all probes, verified by the counter."""
    budget = build_budget(config)
    budget.pause()
    collectors = build_collectors(config, budget)
    for collector in collectors:
        if not collector.active:
            continue
        collector._capability = Capability(True)
        result = collector.collect()
        assert result.values == {}, f"{collector.name} probed while paused"
        assert result.probes == []
    assert budget.stats()["total_granted"] == 0


def test_dns_collector_respects_its_budget(config):
    budget = ProbeBudget(
        https_per_minute=1, dns_per_minute=1, icmp_per_minute=1, traceroute_per_hour=1
    )
    collector = DnsCollector(config, budget)
    collector._capability = Capability(True)
    result = collector.collect()
    denied = [probe for probe in result.probes if "budget" in probe.detail]
    assert denied, "later targets must be refused once the bucket empties"


def test_dns_labels_are_hashed_when_configured(config):
    collector = DnsCollector(config, build_budget(config))
    label = collector._label("example.com")
    assert "example.com" not in label
    assert label.startswith("dns:")


def test_dns_labels_are_plain_when_hashing_is_off():
    from netpulse.config import NetPulseConfig

    config = NetPulseConfig.model_validate({"privacy": {"hash_dns_names": False}})
    collector = DnsCollector(config, build_budget(config))
    assert collector._label("example.com") == "dns:example.com"


# ------------------------------------------------------------------ framework


def test_collector_errors_are_contained(config):
    class Exploding(Collector):
        name = "boom"
        layer = "os"

        def interval_s(self) -> float:
            return 10.0

        def enabled(self) -> bool:
            return True

        def _collect(self) -> CollectorResult:
            raise RuntimeError("driver fell over")

    collector = Exploding(config)
    result = collector.collect()
    assert result.ok is False
    assert "driver fell over" in result.error
    assert collector.consecutive_failures == 1


def test_disabled_collector_returns_a_reason(quiet_config):
    collector = DnsCollector(quiet_config, build_budget(quiet_config))
    assert "disabled" in collector.collect().skipped_reason


def test_unsupported_collector_degrades_without_failing(config):
    collector = HttpsCollector(config, build_budget(config))
    collector._capability = Capability(False, "no TLS stack here")
    result = collector.collect()
    assert result.ok is True
    assert result.skipped_reason == "no TLS stack here"


def test_result_rejects_unknown_features():
    result = CollectorResult()
    with pytest.raises(KeyError):
        result.set("not_a_real_feature", 1.0)


def test_result_drops_non_finite_values():
    result = CollectorResult()
    result.set("gw_rtt_ms", float("nan"))
    result.set("gw_rtt_ms", float("inf"))
    assert result.values == {}


def test_result_clips_to_schema_bounds():
    result = CollectorResult()
    result.set("dns_fail_rate", 4.0)
    assert result.values["dns_fail_rate"] == 1.0


def test_percentile_helper():
    assert percentile([5.0], 0.5) == 5.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.5) in (2.0, 3.0)
    assert percentile([1.0, 2.0, 3.0, 100.0], 0.95) == 100.0


def test_capability_report_covers_every_collector(config):
    report = capability_report(config)
    assert {item["name"] for item in report} == {
        "system",
        "wifi",
        "gateway",
        "dns",
        "https",
        "captive",
        "path",
    }
    for item in report:
        assert "supported" in item["capability"]


def test_shell_run_reports_a_missing_binary():
    result = run(["definitely-not-a-real-binary-xyz"], timeout=2.0)
    assert result.missing is True
    assert result.ok is False


def test_probe_url_rejects_unsafe_targets():
    timings = probe_url("file:///etc/passwd")
    assert timings.ok is False
    assert "http" in timings.error


def test_captive_probe_rejects_unsafe_targets():
    intercepted, detail, _ = detect_portal("file:///etc/passwd")
    assert intercepted is None
    assert "http" in detail


def _fake_command(text: str):
    from netpulse.collectors.shell import CommandResult

    return CommandResult(ok=True, returncode=0, stdout=text, stderr="")


# ---------------------------------------------------------------------- live


@pytest.mark.live
def test_live_collectors_produce_features(config):
    budget = build_budget(config)
    produced = 0
    for collector in build_collectors(config, budget):
        result = collector.collect()
        assert result.ok or result.skipped_reason, collector.name
        produced += len(result.values)
    assert produced > 5, "a connected machine should yield several features"


@pytest.mark.live
def test_live_https_probe_splits_phases(config):
    timings = probe_url(config.probes.https_targets[0])
    if not timings.ok:
        pytest.skip(f"no internet: {timings.error}")
    assert timings.tcp_ms is not None and timings.tcp_ms >= 0
    assert timings.tls_ms is not None and timings.tls_ms >= 0
    assert timings.ttfb_ms is not None
    assert timings.total_ms >= timings.tcp_ms


# ----------------------------------------------------------- captive portal


def test_captive_classifies_a_clean_response():
    from netpulse.collectors.captive import classify_response

    raw = b"HTTP/1.1 200 OK\r\nContent-Length: 8\r\n\r\nsuccess\n"
    assert classify_response(raw) == (False, "no portal detected")


def test_captive_flags_a_redirect():
    from netpulse.collectors.captive import classify_response

    raw = b"HTTP/1.1 302 Found\r\nLocation: http://login.hotel\r\n\r\n"
    intercepted, detail = classify_response(raw)
    assert intercepted is True
    assert "redirect" in detail


def test_captive_flags_a_substituted_body():
    from netpulse.collectors.captive import classify_response

    raw = b"HTTP/1.1 200 OK\r\n\r\n<html>Please sign in to continue</html>"
    intercepted, _ = classify_response(raw)
    assert intercepted is True


def test_captive_does_not_guess_when_only_headers_arrived():
    """The bug this guards: one recv returns headers, the body follows later.

    Treating the missing body as a mismatch reported a captive portal on a
    healthy network, with every other layer green.
    """
    from netpulse.collectors.captive import classify_response

    headers_only = b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 8\r\n\r\n"
    intercepted, detail = classify_response(headers_only)
    assert intercepted is None, detail
    assert "did not arrive" in detail


def test_captive_says_nothing_on_an_empty_read():
    from netpulse.collectors.captive import classify_response

    assert classify_response(b"")[0] is None


def test_captive_reader_reassembles_a_split_response():
    """Headers and body in separate segments must be joined before judging."""
    from netpulse.collectors.captive import _read_response, classify_response

    class SplitSocket:
        def __init__(self, parts):
            self.parts = list(parts)

        def settimeout(self, _value):
            pass

        def recv(self, _size):
            return self.parts.pop(0) if self.parts else b""

    sock = SplitSocket([b"HTTP/1.1 200 OK\r\nContent-Length: 8\r\n\r\n", b"success\n"])
    raw = _read_response(sock, deadline=time.perf_counter() + 5)
    assert b"success" in raw
    assert classify_response(raw) == (False, "no portal detected")


# ------------------------------------------------------------- gateway ICMP


def _gateway_collector(config, monkeypatch, replies: list[str]):
    """A gateway collector whose ping output is scripted."""
    from netpulse.collectors import gateway as gw

    calls = {"n": 0}

    class Fake:
        def __init__(self, text):
            self.text = text

    def fake_run(_cmd, **_kw):
        text = replies[min(calls["n"], len(replies) - 1)]
        calls["n"] += 1
        return Fake(text)

    monkeypatch.setattr(gw, "run", fake_run)
    monkeypatch.setattr(
        gw.netinfo, "default_route", lambda **_k: gw.netinfo.RouteInfo(gateway="10.0.0.1")
    )
    return gw.GatewayCollector(config)


REPLY = "Reply from 10.0.0.1: bytes=32 time=3ms TTL=64\n" * 4
SILENCE = "Request timed out.\n" * 4


def test_gateway_that_never_answers_is_reported_unmeasurable(config, monkeypatch):
    """An iPhone hotspot filters ICMP. That is not a router outage.

    Reporting 100% loss would claim an outage that is not happening and feed
    a permanent 5000 ms round trip into the baselines, pinning health near
    zero forever on a perfectly working network.
    """
    collector = _gateway_collector(config, monkeypatch, [SILENCE])
    result = collector.collect()
    assert result.values == {}, "a filtered gateway must not emit measurements"
    assert "cannot be measured" in result.skipped_reason
    assert "filter ICMP" in result.probes[0].detail


def test_gateway_that_answered_then_stopped_is_a_real_outage(config, monkeypatch):
    """Once it has answered, silence means something actually broke."""
    collector = _gateway_collector(config, monkeypatch, [REPLY, SILENCE])

    first = collector.collect()
    assert first.values["gw_loss_rate"] == 0.0
    assert first.values["gw_rtt_ms"] == 3.0

    second = collector.collect()
    assert second.values["gw_loss_rate"] == 1.0
    assert second.values["gw_rtt_ms"] > 1000, "the timeout ceiling feeds the L0 rule"


def test_gateway_partial_loss_is_still_measured(config, monkeypatch):
    partial = "Reply from 10.0.0.1: bytes=32 time=3ms TTL=64\nRequest timed out.\n"
    collector = _gateway_collector(config, monkeypatch, [partial])
    result = collector.collect()
    assert result.values["gw_loss_rate"] == 0.75
