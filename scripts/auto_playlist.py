#!/usr/bin/env python3
"""Build a public IPTV playlist from maintained M3U feeds without a server.

This script downloads current source playlists, preserves their channel metadata,
removes duplicate stream URLs and publishes full and sports-only M3U files.
Only English/Malay language-tagged streams with recognised American/British
network brands or show titles are published. Name based, not geo based.
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
NAME_ALLOWLIST_FILE = Path("config/us_uk_name_allowlist.txt")
GROUP_RE = re.compile(r'group-title="([^"]*)"', re.IGNORECASE)
LANGUAGE_RE = re.compile(
    r"""(?:^|\s)(?:tvg-language|language)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s,]+))""",
    re.IGNORECASE,
)
ALLOWED_LANGUAGES = frozenset({
    "en", "eng", "english",
    "ms", "msa", "may", "zsm", "malay", "bahasa melayu",
})
# In particular, 'mal' is the language code for Malayalam, NOT Malay.
# Providers sometimes mark foreign-language shows as "eng"; reject names
# that explicitly mention another broadcast language regardless of metadata.
NON_ENGLISH_TITLE_RE = re.compile(
    r"\b(?:en\s+espa(?:n|ñ)ol|en\s+fran(?:c|ç)ais|spanish|polska|polish|"
    r"bollywood|k[-\s]?pop|latin[oa]?|hindi|urdu|arabic|tamil|"
    r"telugu|bengali|filipino|korean|russian|news\s?18|tv18|guyana)\b",
    re.IGNORECASE,
)
QUALITY_SUFFIX_RE = re.compile(r"\s*\(\s*(?:\d{3,4}[pi]|4k)\s*\)\s*$", re.IGNORECASE)
TVG_NAME_RE = re.compile(r"""(?:^|\s)tvg-name="([^"]*)"""", re.IGNORECASE)
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

    @property
    def languages(self) -> tuple[str, ...]:
        match = LANGUAGE_RE.search(self.extinf)
        if not match:
            return ()
        raw = next((value for value in match.groups() if value is not None), "")
        return tuple(
            code.strip().casefold().replace("_", "-")
            for code in re.split(r"[;,|/]+", raw)
            if code.strip()
        )

    @property
    def is_preferred_language(self) -> bool:
        # Require EVERY advertised language to be English or Malay. The
        # previous any(...) logic wrongly retained multilingual broadcasts,
        # e.g. eng;spa, ara;eng and hin;eng.
        # Missing language metadata is not sufficient evidence to include.
        languages = self.languages
        if not languages:
            return False
        for language in languages:
            canonical = re.sub(r"\s*\([^)]*\)\s*$", "", language).strip()
            if canonical in ALLOWED_LANGUAGES:
                continue
            # Accept locale suffixes, e.g. en-US or ms-MY.
            if canonical.split("-", 1)[0] in ALLOWED_LANGUAGES:
                continue
            return False
        return True


def read_name_allowlist(path: Path) -> tuple[str, ...]:
    """Read conservative approved network and show name patterns.

    Only a trailing '*' wildcard is supported, meaning a known brand's variants.
    Unlike a fuzzy classifier, unfamiliar names are excluded by default.
    """
    if not path.is_file():
        raise FileNotFoundError(f"Channel name allowlist is missing: {path}")
    patterns: list[str] = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        pattern = raw.strip().casefold()
        if not pattern or pattern.startswith("#"):
            continue
        if not pattern.strip("*") or pattern.count("*") > 1 or ("*" in pattern and not pattern.endswith("*")):
            raise ValueError(f"Invalid name allowlist pattern: {pattern!r}")
        patterns.append(pattern)
    if not patterns:
        raise ValueError("The US/UK name allowlist must not be empty")
    return tuple(dict.fromkeys(patterns))


def is_us_uk_named_channel(channel: Channel, patterns: tuple[str, ...]) -> bool:
    """Select by recognizable American/British channel/show branding, not country.

    A title alone does not verify the programmes themselves; this is deliberately
    conservative and supports exact entries plus trailing brand wildcards.
    """
    match = TVG_NAME_RE.search(channel.extinf)
    labels = (channel.name, match.group(1) if match else channel.name)
    normalized = tuple(QUALITY_SUFFIX_RE.sub("", label).strip().casefold() for label in labels)
    if any(NON_ENGLISH_TITLE_RE.search(label) for label in normalized):
        return False

    for label in normalized:
        for pattern in patterns:
            if pattern.endswith("*") and label.startswith(pattern[:-1]):
                return True
            if label == pattern:
                return True
    return False


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
    name_allowlist = read_name_allowlist(NAME_ALLOWLIST_FILE)
    LOG.info("US/UK name allowlist loaded: %d patterns (source country is ignored)", len(name_allowlist))
    LOG.info("Loading %d playlist sources", len(sources))
    combined: list[Channel] = []
    seen_urls: set[str] = set()
    sources_loaded = 0
    language_filtered_out = 0
    missing_language_metadata = 0
    excluded_mixed_language = 0
    name_filtered_out = 0

    for source in sources:
        try:
            parsed = parse_channels(fetch_playlist(source))
            if not parsed:
                raise ValueError("No valid channel entries in source")
            accepted = 0
            for channel in parsed:
                if not channel.is_preferred_language:
                    language_filtered_out += 1
                    if not channel.languages:
                        missing_language_metadata += 1
                    else:
                        excluded_mixed_language += 1
                    continue
                if not is_us_uk_named_channel(channel, name_allowlist):
                    name_filtered_out += 1
                    continue
                if channel.url in seen_urls:
                    continue
                seen_urls.add(channel.url)
                combined.append(channel)
                accepted += 1
            sources_loaded += 1
            LOG.info(
                "Source %s: %d parsed, %d English/Malay and US/UK-named unique streams",
                source,
                len(parsed),
                accepted,
            )
        except (RuntimeError, ValueError, OSError) as exc:
            LOG.error("Source skipped: %s", exc)

    if not combined:
        raise RuntimeError(
            "No matching English/Malay language-tagged and US/UK-named streams found; refusing to publish empty files"
        )

    # Make sports easy to find in players while retaining original group metadata.
    combined.sort(key=lambda channel: (not channel.is_sports, channel.group.casefold(), channel.name.casefold()))
    sports = [channel for channel in combined if channel.is_sports]

    OUTPUT_DIRECTORY.mkdir(parents=True, exist_ok=True)
    write_m3u(OUTPUT_DIRECTORY / "result.m3u", combined)
    write_m3u(OUTPUT_DIRECTORY / "sports.m3u", sports)
    write_txt(OUTPUT_DIRECTORY / "result.txt", combined)

    LOG.info(
        "Generated %d English/Malay US/UK-named streams (%d sports) from %d feeds; "
        "excluded %d other/unknown-language entries (%d multilingual, %d missing language metadata), "
        "plus %d entries without recognised US/UK network or show names",
        len(combined),
        len(sports),
        sources_loaded,
        language_filtered_out,
        excluded_mixed_language,
        missing_language_metadata,
        name_filtered_out,
    )
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(
                "### Automatic IPTV source aggregation\n\n"
                f"Fetched **{sources_loaded}/{len(sources)}** feeds, "
                f"collected **{len(combined):,}** unique stream URLs, "
                f"including **{len(sports):,}** sports entries.\n\n"
                f"Excluded **{language_filtered_out:,}** foreign or unknown-language entries "
                f"({excluded_mixed_language:,} advertising other languages; "
                f"{missing_language_metadata:,} without language tags).\n\n"
                f"Excluded **{name_filtered_out:,}** additional channels without recognized "
                "American/British network or show names.\n\n"
                "Only English/Malay-tagged channels matching the curated US/UK name allowlist are included. "
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
