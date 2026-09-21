"""The remediation catalogue.

PRD 11.2 sets the rules these have to follow: imperative, short, at most
three per card, and never more certain than the evidence. A suggestion that
changes system state is described, never performed: PRD 10.3 rules out
auto-remediation that touches DNS or VPN settings without consent, so every
entry here is something the user does.

``deep_link`` points at the OS settings page where one exists. The UI turns
it into a button; the CLI prints it.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Remediation:
    id: str
    text: str
    #: Settings URI per platform, keyed by sys.platform prefix.
    deep_links: tuple[tuple[str, str], ...] = ()
    #: True when the action interrupts connectivity while it happens.
    disruptive: bool = False

    def deep_link(self) -> str | None:
        for platform, uri in self.deep_links:
            if sys.platform.startswith(platform):
                return uri
        return None

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "text": self.text,
            "deep_link": self.deep_link(),
            "disruptive": self.disruptive,
        }


CATALOGUE: tuple[Remediation, ...] = (
    Remediation(
        "move_closer",
        "Move closer to the access point, or remove what is between you and it.",
    ),
    Remediation(
        "switch_band",
        "Switch to the 5 GHz network if your access point offers one.",
        (
            ("win32", "ms-settings:network-wifi"),
            ("darwin", "x-apple.systempreferences:"),
        ),
    ),
    Remediation(
        "change_channel",
        "Change your access point's Wi-Fi channel; the current one is crowded.",
    ),
    Remediation(
        "use_ethernet",
        "Plug in an ethernet cable for anything important in the next few minutes.",
    ),
    Remediation(
        "forget_rejoin",
        "Disconnect and rejoin the network to force a clean association.",
        (),
        disruptive=True,
    ),
    Remediation(
        "flush_dns",
        "Flush the DNS cache on this device.",
    ),
    Remediation(
        "change_resolver",
        "Try a different DNS resolver in your network settings.",
        (
            ("win32", "ms-settings:network"),
            ("darwin", "x-apple.systempreferences:"),
        ),
    ),
    Remediation(
        "sign_in_portal",
        "Open a browser and complete the network sign-in page.",
    ),
    Remediation(
        "restart_gateway",
        "Power cycle your router: unplug it for 30 seconds, then plug it back in.",
        (),
        disruptive=True,
    ),
    Remediation(
        "check_gateway_load",
        "Check whether something on your network is saturating the connection, "
        "such as a large upload or a backup.",
    ),
    Remediation(
        "check_cables",
        "Check the cable between your router and the wall socket.",
    ),
    Remediation(
        "wait_and_watch",
        "Wait a few minutes; this often clears on its own.",
    ),
    Remediation(
        "contact_isp",
        "If this keeps happening, export a diagnostic bundle and send it to your "
        "internet provider.",
    ),
    Remediation(
        "test_without_vpn",
        "Disconnect the VPN briefly to see whether the problem follows it.",
        (),
        disruptive=True,
    ),
    Remediation(
        "check_vpn_server",
        "Try a different VPN server or region.",
    ),
    Remediation(
        "close_heavy_apps",
        "Close whatever is using the most CPU or bandwidth on this device.",
    ),
    Remediation(
        "restart_device_network",
        "Restart this device's network adapter.",
        (("win32", "ms-settings:network-status"),),
        disruptive=True,
    ),
    Remediation(
        "try_other_service",
        "Check whether other sites are fine; if they are, the problem is at the "
        "far end and not with your connection.",
    ),
    Remediation(
        "check_service_status",
        "Check the service's own status page.",
    ),
    Remediation(
        "update_adapter_driver",
        "Check for an update to this device's network adapter driver.",
    ),
)

BY_ID: dict[str, Remediation] = {item.id: item for item in CATALOGUE}


def resolve(ids: list[str] | tuple[str, ...]) -> list[dict[str, object]]:
    """Turn remediation ids into renderable entries, keeping at most three."""
    out: list[dict[str, object]] = []
    for identifier in ids:
        item = BY_ID.get(identifier)
        if item is not None:
            out.append(item.to_dict())
        if len(out) == 3:
            break
    return out


def texts(ids: list[str] | tuple[str, ...]) -> list[str]:
    return [entry["text"] for entry in resolve(ids)]  # type: ignore[misc]
