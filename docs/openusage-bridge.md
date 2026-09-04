# OpenUsage Canvas bridge

`dot-openusage-bridge` reads Claude and Codex usage from OpenUsage on
`127.0.0.1` and updates two existing Dot Canvas API items. Each item shows one
provider. The device loop rotates them at its configured interval, which cannot
be shorter than 60 seconds. The bridge sends only provider
names, whole usage percentages, reset labels, and stale state to Dot.

The bridge requires macOS, OpenUsage with its Local HTTP API enabled, Python
3.10 or newer, and two existing Canvas API items in the device loop.

## Set up

Install the repository package with `uv tool install .` or `pipx install .`.
Both installers normally put `dot-openusage-bridge` in `~/.local/bin`, which is
the path used by the sample LaunchAgent.

Create the local configuration:

```bash
install -d -m 700 "$HOME/Library/Application Support/Dot OpenUsage Bridge"
umask 077
$EDITOR "$HOME/Library/Application Support/Dot OpenUsage Bridge/config.json"
```

Use this shape:

```json
{
  "device_id": "your-device-id",
  "task_keys": {
    "claude": "your-claude-canvas-task-key",
    "codex": "your-codex-canvas-task-key"
  }
}
```

Store the full `dot_app_...` key in macOS Keychain. Keeping `-w` last makes the
`security` command prompt without putting the key in shell history:

```bash
security add-generic-password -U \
  -s tech.mindreset.dot.openusage-bridge \
  -a api-key \
  -w
```

Verify non-interactive access without printing the key:

```bash
security find-generic-password \
  -s tech.mindreset.dot.openusage-bridge \
  -a api-key \
  -w >/dev/null
```

Validate live OpenUsage data and the Canvas payload without reading the Dot key
or changing the device:

```bash
dot-openusage-bridge --dry-run
```

## Schedule it

Inspect the sample at
`examples/tech.mindreset.dot.openusage-bridge.plist`. If the installed command
is not in `~/.local/bin`, change its path before copying the file.

When ready to activate it:

```bash
install -m 600 examples/tech.mindreset.dot.openusage-bridge.plist \
  "$HOME/Library/LaunchAgents/tech.mindreset.dot.openusage-bridge.plist"
launchctl bootstrap "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/tech.mindreset.dot.openusage-bridge.plist"
```

The LaunchAgent runs once after login and then every ten minutes. It does not
control Canvas rotation and does not retry rapidly. Failed sources retain
last-good values and mark only the affected provider stale. Failed Dot writes
retry at the next scheduled run.

State is stored with mode `0600` under Application Support. It contains only
prepared display values, source timestamps, stale state, and hashes of the last
successfully delivered payloads. The API key and raw OpenUsage responses are
never stored there.
