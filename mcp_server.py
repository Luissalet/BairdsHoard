"""Stdio MCP bridge for Baird's Hoard.

It never opens the capture device itself: every tool call is proxied to the running app (`POST /api/agent/call`)
with the Bearer token from `<DATA_DIR>/mcp-token`; the tool list comes from `GET /api/agent/tools`. Images
(`screen_look`, `media_look`) arrive as MCP image content. When nothing answers, the bridge starts the app
(`python -m baird_hoard`) and waits for it; BAIRD_BRIDGE_AUTOSTART=0 turns that off."""

from __future__ import annotations

import sys

from baird_hoard.hoard_link.bridge import CatalogBridge


def main() -> int:
    CatalogBridge(app="baird", service="bairds-hoard", package="baird_hoard", default_port=5232, data_dir_env="BAIRD_DATA_DIR",
                  title="Baird's Hoard", root=__file__, default_timeout=120.0, image_content=True).run_bridge()
    return 0


if __name__ == "__main__":
    sys.exit(main())
