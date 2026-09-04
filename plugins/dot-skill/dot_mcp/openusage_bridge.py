"""Send privacy-filtered OpenUsage dashboards to two Dot Canvas items."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import math
import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from .canvas import validate_canvas_payload
from .client import DotClient, DotError


OPENUSAGE_HOST = "127.0.0.1"
OPENUSAGE_PORT = 6736
OPENUSAGE_SCHEMA = "openusage.limits.v1"
OPENUSAGE_TIMEOUT_SECONDS = 5
MAX_SOURCE_BYTES = 512 * 1024
STATE_VERSION = 2
KEYCHAIN_SERVICE = "tech.mindreset.dot.openusage-bridge"
KEYCHAIN_ACCOUNT = "api-key"
APP_DIR = Path.home() / "Library" / "Application Support" / "Dot OpenUsage Bridge"
CONFIG_PATH = APP_DIR / "config.json"
STATE_PATH = APP_DIR / "state.json"
PROVIDERS = {
    "claude": ("Claude", (("session", "Session"), ("weekly", "Weekly"))),
    "codex": ("Codex", (("session", "Session"), ("weekly", "Weekly"))),
}
CLAUDE_ICON_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAABgAAAAYAQAAAADIDABVAAAAVElEQVQI12P4//8/Awz/"
    "qwZiayDbej7DZ2F5ho+T7Bl+NNQz/GMAijEcZ3jYwM/wgKGe4X8DO1DsOcOfA/UM"
    "P7fYM/xXuc/wLw2o7jlQ7j3CPCAGALwUNLDD5O1RAAAAAElFTkSuQmCC"
)
CODEX_ICON_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAABgAAAAYAQAAAADIDABVAAAAX0lEQVQI12P4//8/Axif"
    "B2Lm/wz/KuwZ/jyez/Dz/HmGp3PeM7zdpM9wtrmf4ezO8wxn96QzvLM1Z3huac7w"
    "8aA5wxNfdYbvlvwMnz/LA9UD9cnVM/w/ADTnM9TM//8BAH00fBFPCVgAAAAASUVO"
    "RK5CYII="
)
PROVIDER_ICON_DATA_URLS = {
    "claude": CLAUDE_ICON_DATA_URL,
    "codex": CODEX_ICON_DATA_URL,
}


class BridgeError(Exception):
    """A safe-to-categorize bridge failure."""


class SourceDataError(BridgeError):
    """OpenUsage returned data outside its documented contract."""


def _diagnose(scope: str, category: str) -> None:
    print(f"{scope}: {category}", file=sys.stderr)


def _reset_label(value: Any, *, now: datetime | None = None) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + ("+00:00" if value.endswith("Z") else ""))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    current = now or datetime.now(parsed.tzinfo)
    if current.tzinfo is None:
        return None
    minutes = max(
        0,
        math.ceil((parsed - current.astimezone(parsed.tzinfo)).total_seconds() / 60),
    )
    if minutes == 0:
        return "now"
    days, remaining = divmod(minutes, 24 * 60)
    hours, minutes = divmod(remaining, 60)
    if days:
        return f"in {days}d {hours}h"
    if hours:
        return f"in {hours}h {minutes}m"
    return f"in {minutes}m"


def _metric(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise SourceDataError
    if raw.get("kind") != "consumption" or raw.get("unit") != "percent":
        raise SourceDataError
    utilization = raw.get("utilization")
    if isinstance(utilization, bool) or not isinstance(utilization, (int, float)):
        raise SourceDataError
    utilization = float(utilization)
    if not math.isfinite(utilization):
        raise SourceDataError
    percent = math.floor(max(0.0, min(1.0, utilization)) * 100 + 0.5)
    metric = {"percent": percent}
    reset = _reset_label(raw.get("resetsAt"))
    if reset:
        metric["reset"] = reset
    return metric


def _clean_cached(provider_id: str, value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    metrics: dict[str, Any] = {}
    for metric_id, _ in PROVIDERS[provider_id][1]:
        raw = value.get("metrics", {}).get(metric_id) if isinstance(value.get("metrics"), Mapping) else None
        if not isinstance(raw, Mapping):
            continue
        percent = raw.get("percent")
        if isinstance(percent, bool) or not isinstance(percent, int) or not 0 <= percent <= 100:
            continue
        metric = {"percent": percent}
        reset = raw.get("reset")
        if isinstance(reset, str) and 0 < len(reset) <= 64:
            metric["reset"] = reset
        metrics[metric_id] = metric
    if not metrics:
        return None
    fetched_at = value.get("fetched_at")
    return {
        "metrics": metrics,
        "stale": value.get("stale") is True,
        "fetched_at": fetched_at if isinstance(fetched_at, str) and len(fetched_at) <= 64 else None,
    }


def stale_provider(provider_id: str, previous: Any) -> dict[str, Any] | None:
    cached = _clean_cached(provider_id, previous)
    if cached:
        cached["stale"] = True
    return cached


def prepare_provider(
    provider_id: str, envelope: Any, previous: Any = None
) -> tuple[dict[str, Any] | None, bool]:
    """Map one family response, retaining cached metrics when current data is invalid."""

    if not isinstance(envelope, Mapping) or envelope.get("schema") != OPENUSAGE_SCHEMA:
        raise SourceDataError
    providers = envelope.get("providers")
    errors = envelope.get("errors", [])
    if not isinstance(providers, Mapping) or len(providers) != 1 or not isinstance(errors, list):
        raise SourceDataError
    snapshot = next(iter(providers.values()))
    if not isinstance(snapshot, Mapping) or not isinstance(snapshot.get("resources"), Mapping):
        raise SourceDataError

    cached = _clean_cached(provider_id, previous)
    resources = snapshot["resources"]
    metrics: dict[str, Any] = {}
    stale = snapshot.get("stale") is not False or bool(errors)
    invalid_metric = False

    for metric_id, _ in PROVIDERS[provider_id][1]:
        raw = resources.get(metric_id)
        if raw is None:
            if cached and metric_id in cached["metrics"]:
                metrics[metric_id] = cached["metrics"][metric_id]
                stale = True
            continue
        try:
            metrics[metric_id] = _metric(raw)
        except SourceDataError:
            invalid_metric = True
            stale = True
            if cached and metric_id in cached["metrics"]:
                metrics[metric_id] = cached["metrics"][metric_id]

    if not metrics:
        return stale_provider(provider_id, previous), invalid_metric

    fetched_at = snapshot.get("fetchedAt")
    if not isinstance(fetched_at, str) or len(fetched_at) > 64:
        fetched_at = cached.get("fetched_at") if cached else None
        stale = True
    return {
        "metrics": metrics,
        "stale": stale,
        "fetched_at": fetched_at,
    }, invalid_metric


def fetch_limits(provider_id: str) -> Any:
    connection = http.client.HTTPConnection(
        OPENUSAGE_HOST, OPENUSAGE_PORT, timeout=OPENUSAGE_TIMEOUT_SECONDS
    )
    try:
        connection.request(
            "GET", f"/v1/limits/{provider_id}", headers={"Accept": "application/json"}
        )
        response = connection.getresponse()
        if response.status != 200:
            raise SourceDataError
        body = response.read(MAX_SOURCE_BYTES + 1)
        if len(body) > MAX_SOURCE_BYTES:
            raise SourceDataError
        return json.loads(body.decode("utf-8"))
    except (http.client.HTTPException, OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SourceDataError from error
    finally:
        connection.close()


def _get(path: str, default: str = "") -> str:
    return f'{{{{get inputData "{path}" default="{default}"}}}}'


def _metric_card(
    provider_id: str,
    metric_id: str,
    label: str,
    metric: Mapping[str, Any],
    *,
    stale: bool,
) -> dict[str, Any]:
    key = f"{provider_id}_{metric_id}"
    name = PROVIDERS[provider_id][0]
    title_text = f"{label}{' STALE' if stale else ''}"
    title = {
        "type": "div",
        "props": {
            "tw": "flex flex-row items-center shrink-0 gap-[8px]",
            "children": [
                {
                    "type": "img",
                    "props": {
                        "tw": "w-[24px] h-[24px] shrink-0 img-dither-none img-levels-2",
                        "style": {"objectFit": "contain"},
                        "src": PROVIDER_ICON_DATA_URLS[provider_id],
                        "alt": name,
                    },
                },
                {
                    "type": "span",
                    "props": {
                        "tw": "text-18-chillduansans font-bold shrink-0",
                        "children": title_text,
                    },
                },
            ],
        },
    }

    children: list[dict[str, Any]] = [title]
    if metric.get("reset"):
        children.append(
            {
                "type": "span",
                "props": {
                    "tw": "text-14-chillduansans min-w-0 overflow-hidden shrink-0",
                    "style": {
                        "lineHeight": "20px",
                        "whiteSpace": "nowrap",
                        "textOverflow": "ellipsis",
                    },
                    "children": _get(f"{key}_reset"),
                },
            }
        )
    children.extend(
        [
        {
            "type": "span",
            "props": {
                "tw": "text-36-chillduansans font-bold shrink-0",
                "style": {"lineHeight": "40px", "marginTop": -4},
                "children": _get(f"{key}_percent", "-"),
            },
        },
        {
            "type": "div",
            "props": {
                "tw": "w-full h-[10px] rounded-full overflow-hidden shrink-0",
                "style": {"backgroundColor": "#D1D5DB"},
                "children": {
                    "type": "div",
                    "props": {
                        "tw": "h-full rounded-full bg-black",
                        "style": {"width": _get(f"{key}_width", "0%")},
                    },
                },
            },
        },
        ]
    )
    return {
        "type": "div",
        "props": {
            "tw": "flex flex-col flex-1 min-w-0 border border-black rounded-[10px] px-[12px] py-[10px] gap-[4px] overflow-hidden",
            "children": children,
        },
    }


def _provider_layout(provider_id: str, provider: Mapping[str, Any]) -> list[dict[str, Any]]:
    _, metric_specs = PROVIDERS[provider_id]
    cards = [
        _metric_card(
            provider_id,
            metric_id,
            label,
            provider["metrics"][metric_id],
            stale=provider.get("stale") is True,
        )
        for metric_id, label in metric_specs
        if metric_id in provider["metrics"]
    ]
    return [
        {
            "type": "div",
            "props": {
                "tw": "flex flex-row flex-1 min-h-0 gap-[10px] p-[10px] bg-white overflow-hidden",
                "children": cards,
            },
        },
    ]


def build_canvas_payload(providers: Mapping[str, Any], task_key: str | None = None) -> dict[str, Any]:
    visible = [(provider_id, providers[provider_id]) for provider_id in PROVIDERS if provider_id in providers]
    if len(visible) != 1:
        raise ValueError("Each Canvas payload must contain exactly one provider")
    data: dict[str, Any] = {}
    provider_id, provider = visible[0]
    for metric_id, metric in provider["metrics"].items():
        key = f"{provider_id}_{metric_id}"
        data[f"{key}_percent"] = f"{metric['percent']}%"
        data[f"{key}_width"] = f"{metric['percent']}%"
        if metric.get("reset"):
            data[f"{key}_reset"] = f"Reset {metric['reset']}"

    payload: dict[str, Any] = {
        "refreshNow": True,
        "data": data,
        "windowData": {
            "default": [
                {
                    "type": "div",
                    "props": {
                        "tw": "flex flex-col w-full h-full bg-white text-black min-h-0 overflow-hidden",
                        "children": _provider_layout(provider_id, provider),
                    },
                }
            ]
        },
        "layoutFull": {"tw": "p-0 bg-white", "style": {"padding": 0}},
        "border": 0,
    }
    if task_key is not None:
        payload["taskKey"] = task_key
    return payload


def _load_json(path: Path, max_bytes: int) -> Any:
    try:
        if path.stat().st_size > max_bytes:
            raise BridgeError
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BridgeError from error


def load_config() -> dict[str, Any]:
    value = _load_json(CONFIG_PATH, 16 * 1024)
    if not isinstance(value, Mapping) or set(value) != {"device_id", "task_keys"}:
        raise BridgeError
    device_id = value["device_id"]
    task_keys = value["task_keys"]
    if not isinstance(device_id, str) or not device_id.strip() or len(device_id) > 256:
        raise BridgeError
    if not isinstance(task_keys, Mapping) or set(task_keys) != set(PROVIDERS):
        raise BridgeError
    if any(
        not isinstance(task_keys[key], str)
        or not task_keys[key].strip()
        or len(task_keys[key]) > 256
        for key in PROVIDERS
    ):
        raise BridgeError
    return {
        "device_id": device_id.strip(),
        "task_keys": {key: task_keys[key].strip() for key in PROVIDERS},
    }


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {
            "version": STATE_VERSION,
            "providers": {},
            "delivered_fingerprints": {},
        }
    value = _load_json(STATE_PATH, 64 * 1024)
    if (
        not isinstance(value, Mapping)
        or value.get("version") != STATE_VERSION
        or not isinstance(value.get("providers"), Mapping)
        or not isinstance(value.get("delivered_fingerprints"), Mapping)
    ):
        raise BridgeError
    fingerprints = value["delivered_fingerprints"]
    if set(fingerprints) - set(PROVIDERS) or any(
        not isinstance(fingerprint, str) or len(fingerprint) != 64
        for fingerprint in fingerprints.values()
    ):
        raise BridgeError
    return {
        "version": STATE_VERSION,
        "providers": dict(value["providers"]),
        "delivered_fingerprints": dict(fingerprints),
    }


def save_state(state: Mapping[str, Any]) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    APP_DIR.chmod(0o700)
    fd, temporary_name = tempfile.mkstemp(prefix="state.", dir=APP_DIR)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(state, file, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(STATE_PATH)
    finally:
        temporary.unlink(missing_ok=True)


def read_keychain_api_key() -> str:
    try:
        result = subprocess.run(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                KEYCHAIN_ACCOUNT,
                "-w",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise BridgeError from error
    api_key = result.stdout.strip()
    if not api_key.startswith("dot_app_") or len(api_key) > 512:
        raise BridgeError
    return api_key


def _fingerprint(device_id: str, payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        {"device_id": device_id, "payload": payload},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def run_bridge(*, dry_run: bool = False) -> int:
    try:
        state = load_state()
    except BridgeError:
        _diagnose("state", "unavailable")
        return 1

    providers: dict[str, Any] = {}
    for provider_id in PROVIDERS:
        previous = state["providers"].get(provider_id)
        try:
            envelope = fetch_limits(provider_id)
            provider, invalid_metric = prepare_provider(provider_id, envelope, previous)
            if invalid_metric:
                _diagnose(provider_id, "invalid source data")
        except SourceDataError:
            _diagnose(provider_id, "source unavailable")
            provider = stale_provider(provider_id, previous)
        if provider:
            providers[provider_id] = provider

    if not providers:
        _diagnose("dashboard", "no usable data")
        return 1

    if dry_run:
        results = {
            provider_id: validate_canvas_payload(
                build_canvas_payload({provider_id: provider})
            )["nodeCount"]
            for provider_id, provider in providers.items()
        }
        summary = ", ".join(
            f"{provider_id} {nodes} nodes" for provider_id, nodes in results.items()
        )
        print(f"Canvas payloads valid: {summary}")
        return 0

    try:
        config = load_config()
    except BridgeError:
        _diagnose("config", "unavailable")
        return 1

    fingerprints = dict(state["delivered_fingerprints"])
    updates: list[tuple[str, dict[str, Any], str]] = []
    for provider_id, provider in providers.items():
        payload = build_canvas_payload(
            {provider_id: provider}, config["task_keys"][provider_id]
        )
        try:
            validate_canvas_payload(payload)
        except ValueError:
            _diagnose(provider_id, "Canvas validation failed")
            return 1
        fingerprint = _fingerprint(config["device_id"], payload)
        if fingerprint != fingerprints.get(provider_id):
            updates.append((provider_id, payload, fingerprint))

    next_state = {
        "version": STATE_VERSION,
        "providers": providers,
        "delivered_fingerprints": fingerprints,
    }
    if not updates:
        try:
            save_state(next_state)
        except OSError:
            _diagnose("state", "write failed")
            return 1
        return 0

    try:
        client = DotClient(read_keychain_api_key())
    except BridgeError:
        _diagnose("keychain", "unavailable")
        try:
            save_state(next_state)
        except OSError:
            _diagnose("state", "write failed")
        return 1

    failed = False
    for provider_id, payload, fingerprint in updates:
        try:
            client.send_canvas(config["device_id"], payload)
        except DotError:
            _diagnose(provider_id, "Dot delivery failed")
            failed = True
        else:
            fingerprints[provider_id] = fingerprint

    next_state["delivered_fingerprints"] = fingerprints
    try:
        save_state(next_state)
    except OSError:
        _diagnose("state", "write failed")
        return 1
    return int(failed)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate live localhost data without reading the Dot key or changing the device",
    )
    args = parser.parse_args()
    raise SystemExit(run_bridge(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
