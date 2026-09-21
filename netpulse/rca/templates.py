"""Plain-language incident templates.

PRD 10.1 asks for at least twelve templates covering Wi-Fi, DNS, gateway,
ISP path, remote service and VPN; this module ships nineteen. PRD 11.2 sets
the voice:

* no jargon without an expansion in the same sentence
* "likely" rather than a claim of certainty, and never a claim about the ISP
  that the path evidence does not support
* at most three remediations, imperative and short

Templates are selected by layer and then by the most specific matching
condition, so "your signal is fine but the air is busy" wins over the
generic Wi-Fi card whenever the evidence supports it. Every template is
offline: the MVP must work with no network and no model download, which
rules out generated prose.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..features.pipeline import FeatureFrame
from ..features.schema import HUMAN_LAYER_NAMES


@dataclass(slots=True)
class TemplateContext:
    """Everything a template condition may look at."""

    frame: FeatureFrame
    layer: str
    secondary: str | None
    severity: str
    rule_ids: frozenset[str] = frozenset()
    risk_15m: float = 0.0

    def value(self, name: str, default: float = 0.0) -> float:
        return self.frame.values.get(name, self.frame.derived.get(name, default))

    def agg(self, name: str, default: float = 0.0) -> float:
        return self.frame.aggregates.get(name, default)

    def rising(self, feature: str, percent_per_minute: float = 0.05) -> bool:
        level = abs(self.agg(f"{feature}_5m_mean"))
        if level <= 1e-6:
            return False
        return self.agg(f"{feature}_5m_slope") / level >= percent_per_minute


@dataclass(frozen=True, slots=True)
class Template:
    id: str
    layer: str
    title: str
    body: str
    remediation: tuple[str, ...]
    condition: Callable[[TemplateContext], bool] = field(default=lambda ctx: True)
    #: Higher wins when several templates match the same layer.
    specificity: int = 0

    def render(self, ctx: TemplateContext) -> dict[str, Any]:
        return {
            "template_id": self.id,
            "title": self.title,
            "summary": self.body.format(**_render_values(ctx)),
            "remediation": self.remediation,
        }


def _render_values(ctx: TemplateContext) -> dict[str, str]:
    """Human-readable substitutions available to every template body."""
    return {
        "layer": HUMAN_LAYER_NAMES.get(ctx.layer, ctx.layer),
        "secondary": HUMAN_LAYER_NAMES.get(ctx.secondary or "", ""),
        "rssi": f"{ctx.value('wifi_rssi_dbm', 0.0):.0f}",
        "retries": f"{ctx.value('wifi_tx_retry_rate', 0.0) * 100:.0f}",
        "link": f"{ctx.value('wifi_link_mbps', 0.0):.0f}",
        "band": f"{ctx.value('wifi_band_ghz', 0.0):.1f}",
        "gw_rtt": f"{ctx.value('gw_rtt_ms', 0.0):.0f}",
        "gw_loss": f"{ctx.value('gw_loss_rate', 0.0) * 100:.0f}",
        "gw_jitter": f"{ctx.value('gw_jitter_ms', 0.0):.0f}",
        "dns_p50": f"{ctx.value('dns_p50_ms', 0.0):.0f}",
        "dns_fail": f"{ctx.value('dns_fail_rate', 0.0) * 100:.0f}",
        "ttfb": f"{ctx.value('https_ttfb_ms', 0.0):.0f}",
        "tls": f"{ctx.value('https_tls_ms', 0.0):.0f}",
        "https_fail": f"{ctx.value('https_fail_rate', 0.0) * 100:.0f}",
        "path_rtt": f"{ctx.value('path_rtt_ms', 0.0):.0f}",
        "path_extra": f"{ctx.value('x_path_minus_gateway_ms', 0.0):.0f}",
        "remote_extra": f"{ctx.value('x_remote_minus_path_ms', 0.0):.0f}",
        "cpu": f"{ctx.value('os_cpu_pct', 0.0):.0f}",
        "retrans": f"{ctx.value('os_retrans_rate', 0.0) * 100:.1f}",
        "risk": f"{ctx.risk_15m * 100:.0f}",
    }


TEMPLATES: tuple[Template, ...] = (
    # ------------------------------------------------------------------ Wi-Fi
    Template(
        "wifi.signal_floor",
        "wifi",
        "Wi-Fi signal is very weak",
        "Your Wi-Fi signal is down to {rssi} dBm, which is the level where video "
        "calls usually start breaking up. The link is negotiating {link} Mbit/s.",
        ("move_closer", "switch_band", "use_ethernet"),
        lambda ctx: ctx.value("wifi_rssi_dbm", 0.0) <= -75.0,
        specificity=3,
    ),
    Template(
        "wifi.interference",
        "wifi",
        "Wi-Fi is busy or noisy",
        "Your signal strength is fine at {rssi} dBm, but {retries}% of Wi-Fi frames "
        "are being sent more than once. That usually means the channel is crowded "
        "rather than that you are too far away.",
        ("switch_band", "change_channel", "use_ethernet"),
        lambda ctx: (
            ctx.value("wifi_tx_retry_rate", 0.0) >= 0.15
            and ctx.value("wifi_rssi_dbm", -100.0) > -70.0
        ),
        specificity=4,
    ),
    Template(
        "wifi.crowded_24",
        "wifi",
        "The 2.4 GHz band is struggling",
        "You are on the 2.4 GHz band, which is shared with microwaves, baby "
        "monitors and every neighbouring network. {retries}% of frames are being "
        "retried.",
        ("switch_band", "change_channel", "move_closer"),
        lambda ctx: (
            ctx.value("wifi_band_ghz", 5.0) < 3.0 and ctx.value("wifi_tx_retry_rate", 0.0) >= 0.12
        ),
        specificity=5,
    ),
    Template(
        "wifi.roaming",
        "wifi",
        "Your device keeps switching access points",
        "This device has moved between access points recently. Each switch drops "
        "packets for a moment, which is enough to stutter a call.",
        ("move_closer", "use_ethernet", "forget_rejoin"),
        lambda ctx: ctx.value("wifi_roam_count", 0.0) >= 1.0,
        specificity=4,
    ),
    Template(
        "wifi.rate_collapse",
        "wifi",
        "Wi-Fi speed has dropped sharply",
        "The Wi-Fi link has fallen to {link} Mbit/s. Signal is {rssi} dBm.",
        ("move_closer", "switch_band", "forget_rejoin"),
        lambda ctx: 0 < ctx.value("wifi_link_mbps", 0.0) <= 54.0,
        specificity=2,
    ),
    Template(
        "wifi.generic",
        "wifi",
        "Wi-Fi quality is dropping",
        "Wi-Fi is the layer that looks worst right now: signal {rssi} dBm, "
        "{retries}% of frames retried.",
        ("move_closer", "switch_band", "use_ethernet"),
        specificity=0,
    ),
    # -------------------------------------------------------------------- DNS
    Template(
        "dns.captive_portal",
        "dns",
        "A sign-in page is intercepting this network",
        "Something on this network is answering for every address, which is what "
        "a hotel or cafe sign-in page does. Nothing else can be measured until "
        "you sign in.",
        ("sign_in_portal", "wait_and_watch"),
        lambda ctx: ctx.value("captive_portal", 0.0) >= 1.0 or "l0.captive_portal" in ctx.rule_ids,
        specificity=9,
    ),
    Template(
        "dns.failing",
        "dns",
        "Name lookups are failing",
        "{dns_fail}% of name lookups are failing. DNS is the internet's phone "
        "book, so when it stops answering, sites fail to load even though the "
        "connection itself is up.",
        ("flush_dns", "change_resolver", "restart_gateway"),
        lambda ctx: ctx.value("dns_fail_rate", 0.0) >= 0.2,
        specificity=5,
    ),
    Template(
        "dns.slow",
        "dns",
        "Name lookups are slow",
        "Looking up addresses is taking {dns_p50} ms, well above normal for this "
        "network. DNS is the internet's phone book: when it is slow, pages feel "
        "slow to start even once they load fine.",
        ("flush_dns", "change_resolver", "wait_and_watch"),
        specificity=1,
    ),
    # ---------------------------------------------------------------- gateway
    Template(
        "gateway.unreachable",
        "gateway",
        "Your router is not responding",
        "Your router has stopped answering entirely. Nothing beyond it can be "
        "reached or measured until it comes back.",
        ("check_cables", "restart_gateway", "wait_and_watch"),
        lambda ctx: ctx.value("gw_loss_rate", 0.0) >= 0.99,
        specificity=9,
    ),
    Template(
        "gateway.loss",
        "gateway",
        "Your router is dropping packets",
        "{gw_loss}% of probes to your router are getting no reply. That is inside "
        "your home or office, before the internet connection.",
        ("restart_gateway", "check_cables", "check_gateway_load"),
        lambda ctx: ctx.value("gw_loss_rate", 0.0) >= 0.1,
        specificity=5,
    ),
    Template(
        "gateway.congestion",
        "gateway",
        "Your router is slow to respond",
        "Round trips to your own router are taking {gw_rtt} ms with {gw_jitter} ms "
        "of variation. On a healthy home network this is a couple of "
        "milliseconds and steady, so something local is loading it up.",
        ("check_gateway_load", "restart_gateway", "use_ethernet"),
        specificity=1,
    ),
    # ------------------------------------------------------------------- path
    Template(
        "path.reroute",
        "path",
        "Your route to the internet changed",
        "The path out to the internet took a different route than before, and "
        "latency past your router went up by about {path_extra} ms. This is "
        "usually your provider rerouting traffic.",
        ("wait_and_watch", "contact_isp", "try_other_service"),
        lambda ctx: ctx.value("path_changed", 0.0) >= 1.0,
        specificity=5,
    ),
    Template(
        "path.isp_congestion",
        "path",
        "Congestion past your router",
        "Your own network looks fine: your router answers in {gw_rtt} ms. Past "
        "it, the path out is adding about {path_extra} ms. That points to your "
        "internet connection or your provider rather than anything in your home.",
        ("wait_and_watch", "contact_isp", "use_ethernet"),
        specificity=1,
    ),
    # ----------------------------------------------------------- remote HTTPS
    Template(
        "remote.unreachable",
        "remote_https",
        "Test connections to the internet are failing",
        "{https_fail}% of test connections are failing while your router still "
        "answers normally. The break is somewhere past your network.",
        ("try_other_service", "check_service_status", "contact_isp"),
        lambda ctx: ctx.value("https_fail_rate", 0.0) >= 0.5,
        specificity=6,
    ),
    Template(
        "remote.tls_slow",
        "remote_https",
        "Secure connections are slow to set up",
        "The encrypted handshake at the start of each connection is taking {tls} "
        "ms. Pages will feel slow to start even though they transfer normally "
        "once open.",
        ("try_other_service", "check_service_status", "wait_and_watch"),
        lambda ctx: ctx.value("https_tls_ms", 0.0) >= 250.0,
        specificity=4,
    ),
    Template(
        "remote.service_slow",
        "remote_https",
        "The far end is slow to answer",
        "Your router and the path out to the internet both look normal, but the "
        "services we test are taking {ttfb} ms to send their first byte, about "
        "{remote_extra} ms more than the network itself explains.",
        ("try_other_service", "check_service_status", "wait_and_watch"),
        specificity=1,
    ),
    # -------------------------------------------------------------------- VPN
    Template(
        "vpn.overload",
        "vpn",
        "Your VPN looks like the bottleneck",
        "A VPN is active, and the extra time is appearing inside the tunnel "
        "rather than on the path to it: {retrans}% of connections are being "
        "retransmitted and the far end is taking {ttfb} ms to answer.",
        ("test_without_vpn", "check_vpn_server", "wait_and_watch"),
        specificity=1,
    ),
    # ------------------------------------------------------------------ device
    Template(
        "os.retransmissions",
        "os",
        "This device is resending a lot of traffic",
        "{retrans}% of the connections from this device are being retransmitted. "
        "That is measured on the machine itself, before anything leaves it.",
        ("restart_device_network", "update_adapter_driver", "close_heavy_apps"),
        lambda ctx: ctx.value("os_retrans_rate", 0.0) >= 0.05,
        specificity=4,
    ),
    Template(
        "os.resource_pressure",
        "os",
        "This device is under load",
        "CPU is at {cpu}%. When the machine is this busy, network traffic waits "
        "for the processor, and calls stutter even on a healthy connection.",
        ("close_heavy_apps", "wait_and_watch"),
        lambda ctx: ctx.value("os_cpu_pct", 0.0) >= 75.0,
        specificity=4,
    ),
    Template(
        "os.generic",
        "os",
        "This device looks like the problem",
        "The signals that changed are on this machine rather than on the network "
        "around it: CPU {cpu}%, {retrans}% of connections retransmitted.",
        ("close_heavy_apps", "restart_device_network", "update_adapter_driver"),
        specificity=0,
    ),
)

BY_LAYER: dict[str, list[Template]] = {}
for _template in TEMPLATES:
    BY_LAYER.setdefault(_template.layer, []).append(_template)
for _templates in BY_LAYER.values():
    _templates.sort(key=lambda item: -item.specificity)

BY_ID: dict[str, Template] = {template.id: template for template in TEMPLATES}


def select(ctx: TemplateContext) -> Template:
    """Pick the most specific template whose condition holds for this layer."""
    candidates = BY_LAYER.get(ctx.layer, [])
    for template in candidates:
        try:
            if template.condition(ctx):
                return template
        except (KeyError, TypeError, ValueError):
            continue
    if candidates:
        return candidates[-1]
    return FALLBACK


FALLBACK = Template(
    "generic.degradation",
    "os",
    "Network quality is dropping",
    "Several signals are moving at once and no single layer stands out yet. "
    "There is roughly a {risk}% chance of noticeable trouble in the next 15 "
    "minutes.",
    ("wait_and_watch", "use_ethernet", "try_other_service"),
)


def coverage_report() -> dict[str, int]:
    """Template count per layer, asserted by the test suite against PRD 10.1."""
    return {layer: len(items) for layer, items in sorted(BY_LAYER.items())}
