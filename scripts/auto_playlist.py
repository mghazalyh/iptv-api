#!/usr/bin/env python3
"""Build a public IPTV playlist from maintained M3U feeds without a server.

This script downloads current source playlists, preserves their channel metadata,
removes duplicate stream URLs and publishes full and sports-only M3U files.
Stream health is determined by the upstream provider, not rechecked here.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

LOG = logging.getLogger("auto_playlist")
SOURCE_FILE = Path("config/subscribe.txt")
OUTPUT_DIRECTORY = Path("output")
MAX_DOWNLOAD_BYTES = 40 * 1024 * 1024
EPG_URL = "https://dearbulut.github.io/iptv/epg/guide.xml.gz"
GROUP_RE = re.compile(r'group-title="([^"]*)"', re.IGNORECASE)
SPORTS_RE = re.compile(
    r"\b(sport|sports|football|soccer|cricket|tennis|racing|formula\s*1|"
    r"motorsport|basketball|baseball|rugby|golf|boxing|mma|nba|nfl|nhl|"
    r"mlb|f1|espn|eurosport|bein)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Channel:
    name: str
    group: str
    extinf: str
    url: str
    options: tuple[str, ...]

    @property
    def is_sports(self) -> bool:
        return bool(SPORTS_RE.search(self.group) or SPORTS_RE.search(self.name))


def is_http_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return parsed.scheme.lower() in ("http", "https") and bool(parsed.netloc)
    except (ValueError, TypeError):
        return False


def read_sources(path: Path) -> list[str]:
    if not path.is_file():
        raise FileNotFoundError(f"Source list not found: {path}")

    urls: list[str] = []
    seen: set[str] = set()
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        value = raw.strip()
        if value.upper() == "[WHITELIST]":
            break
        if not value or value.startswith(("#", ";")):
            continue
        # Allow the upstream project's optional UA= suffix.
        value = re.split(r'\s+UA=', value, maxsplit=1, flags=re.IGNORECASE)[0]
        if not is_http_url(value):
            LOG.warning("Skipping invalid playlist source: %r", raw)
            continue
        if value not in seen:
            seen.add(value)
            urls.append(value)

    if not urls:
        raise ValueError(f"No valid subscription URLs configured in {path}")
    return urls


def fetch_playlist(url: str) -> str:
    request = Request(url, headers={"User-Agent": "IPTV-Playlist-Builder/1.0", "Accept": "text/plain, */*"})
    last_error: Exception | None = None
    for attempt in range(1, 3):
        try:
            with urlopen(request, timeout=35) as response:
                payload = response.read(MAX_DOWNLOAD_BYTES + 1)
            if len(payload) > MAX_DOWNLOAD_BYTES:
                raise ValueError(f"Playlist exceeds {MAX_DOWNLOAD_BYTES} bytes")
            content = payload.decode("utf-8-sig", errors="replace")
            if "#EXTINF:" not in content:
                raise ValueError("Response does not contain M3U channels")
            LOG.info("Fetched %s (%d bytes)", url, len(payload))
            return content
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            last_error = exc
            LOG.warning("Fetch attempt %d failed for %s: %s", attempt, url, exc)
            if attempt < 2:
                time.sleep(2)
    raise RuntimeError(f"Could not fetch {url}: {last_error}")


def parse_channels(content: str) -> list[Channel]:
    channels: list[Channel] = []
    extinf: str | None = None
    options: list[str] = []

    for raw in content.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            extinf, options = line, []
            continue
        if line.startswith(("#EXTVLCOPT:", "#KODIPROP:")) and extinf:
            options.append(line)
            continue
        if line.startswith("#"):
            continue
        if extinf and is_http_url(line):
            name = extinf.partition(",")[2].strip() or "Unnamed channel"
            match = GROUP_RE.search(extinf)
            group = match.group(1).strip() if match else "Other"
            channels.append(Channel(name, group, extinf, line, tuple(options)))
        extinf, options = None, []

    return channels


def write_m3u(path: Path, channels: list[Channel]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(f'#EXTM3U x-tvg-url="{EPG_URL}"\n')
        for channel in channels:
            handle.write(channel.extinf + "\n")
            for option in channel.options:
                handle.write(option + "\n")
            handle.write(channel.url + "\n")


def write_txt(path: Path, channels: list[Channel]) -> None:
    by_group: dict[str, list[Channel]] = defaultdict(list)
    for channel in channels:
        group = channel.group.replace(",", " ").strip() or "Other"
        by_group[group].append(channel)

    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for group, items in by_group.items():
            handle.write(f"{group},#genre#\n")
            for channel in items:
                name = channel.name.replace(",", " ").strip()
                handle.write(f"{name},{channel.url}\n")
            handle.write("\n")


def build() -> tuple[int, int, int]:
    sources = read_sources(SOURCE_FILE)
    LOG.info("Loading %d playlist sources", len(sources))
    combined: list[Channel] = []
    seen_urls: set[str] = set()
    sources_loaded = 0

    for source in sources:
        try:
            parsed = parse_channels(fetch_playlist(source))
            if not parsed:
                raise ValueError("No valid channel entries in source")
            accepted = 0
            for channel in parsed:
                if channel.url in seen_urls:
                    continue
                seen_urls.add(channel.url)
                combined.append(channel)
                accepted += 1
            sources_loaded += 1
            LOG.info("Source %s: %d parsed, %d unique streams", source, len(parsed), accepted)
        except (RuntimeError, ValueError, OSError) as exc:
            LOG.error("Source skipped: %s", exc)

    if not combined:
        raise RuntimeError("All IPTV sources failed or returned no valid channels; refusing to publish empty files")

    # Make sports easy to find in players while retaining original group metadata.
    combined.sort(key=lambda channel: (not channel.is_sports, channel.group.casefold(), channel.name.casefold()))
    sports = [channel for channel in combined if channel.is_sports]

    OUTPUT_DIRECTORY.mkdir(parents=True, exist_ok=True)
    write_m3u(OUTPUT_DIRECTORY / "result.m3u", combined)
    write_m3u(OUTPUT_DIRECTORY / "sports.m3u", sports)
    write_txt(OUTPUT_DIRECTORY / "result.txt", combined)

    LOG.info("Generated %d streams (%d sports) from %d feeds", len(combined), len(sports), sources_loaded)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(
                "### Automatic IPTV source aggregation\n\n"
                f"Fetched **{sources_loaded}/{len(sources)}** feeds, "
                f"collected **{len(combined):,}** unique stream URLs, "
                f"including **{len(sports):,}** sports entries.\n\n"
                "Playlist URLs may still be restricted by location or provider.\n\n"
            )
    return sources_loaded, len(combined), len(sports)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    try:
        build()
        return 0
    except Exception:
        LOG.exception("Automatic playlist generation failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
