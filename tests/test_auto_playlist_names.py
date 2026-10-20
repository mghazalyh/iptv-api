"""Offline regression tests for name and language filtering in the M3U exporter."""

import unittest

from scripts.auto_playlist import (
    Channel,
    is_us_uk_named_channel,
    read_name_allowlist,
    NAME_ALLOWLIST_FILE,
)


def channel(name, language="eng"):
    return Channel(
        name=name,
        group="Entertainment",
        extinf=(
            f'#EXTINF:-1 tvg-name="{name}" tvg-language="{language}" '
            f'group-title="Entertainment",{name}'
        ),
        url="https://example.org/stream.m3u8",
        options=(),
    )


class NameFilteringTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.patterns = read_name_allowlist(NAME_ALLOWLIST_FILE)

    def test_allow_recognisable_british_network(self):
        self.assertTrue(is_us_uk_named_channel(channel("BBC One (720p)"), self.patterns))

    def test_allow_american_named_show(self):
        self.assertTrue(is_us_uk_named_channel(channel("Charmed"), self.patterns))

    def test_allow_non_us_uk_country_when_brand_is_known(self):
        self.assertTrue(is_us_uk_named_channel(channel("HBO Hits Asia"), self.patterns))

    def test_reject_english_tagged_unrelated_foreign_channel(self):
        self.assertFalse(is_us_uk_named_channel(channel("NDTV 24x7"), self.patterns))

    def test_reject_explicit_other_language_title(self):
        self.assertFalse(is_us_uk_named_channel(channel("South Park En Francais"), self.patterns))

    def test_reject_foreign_network_variant(self):
        self.assertFalse(is_us_uk_named_channel(channel("CNN News 18"), self.patterns))

    def test_reject_generic_english_sounding_name(self):
        self.assertFalse(is_us_uk_named_channel(channel("Great One Channel"), self.patterns))

    def test_keep_strict_language_restriction(self):
        self.assertFalse(channel("BBC One", "eng;spa").is_preferred_language)
        self.assertTrue(channel("BBC One", "eng").is_preferred_language)
        self.assertTrue(channel("BBC One", "msa").is_preferred_language)


if __name__ == "__main__":
    unittest.main()
