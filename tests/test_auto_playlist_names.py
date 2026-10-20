"""Offline regression tests for name and language filtering in the M3U exporter."""

import unittest

from scripts.auto_playlist import (
    Channel,
    is_us_uk_named_channel,
    is_news_or_music,
    read_name_allowlist,
    NAME_ALLOWLIST_FILE,
)


def channel(name, language="eng", group="Entertainment"):
    return Channel(
        name=name,
        group=group,
        extinf=(
            f'#EXTINF:-1 tvg-name="{name}" tvg-language="{language}" '
            f'group-title="{group}",{name}'
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

    def test_remove_news_and_music_categories(self):
        for group in ("News", "Music", "Business", "Weather", "News & Current Affairs", "Music & Radio"):
            with self.subTest(group=group):
                self.assertTrue(is_news_or_music(channel("BBC Entertainment", group=group)))

    def test_remove_news_even_with_wrong_category(self):
        for name in ("BBC News", "CNN Originals", "Fox Sports News", "CNBC Asia", "Newsmax TV"):
            with self.subTest(name=name):
                self.assertTrue(is_news_or_music(channel(name, group="Entertainment")))

    def test_remove_music_even_with_wrong_category(self):
        for name in ("MTV", "MTV Global", "CMTV", "VH1 Classic", "Vevo Pop"):
            with self.subTest(name=name):
                self.assertTrue(is_news_or_music(channel(name, group="Entertainment")))

    def test_keep_show_chans_and_regular_networks(self):
        for name in ("BBC One", "HBO Hits", "Sabrina The Teenage Witch", "MTV Jersey Shore", "VH1 Queens of Reality"):
            with self.subTest(name=name):
                self.assertFalse(is_news_or_music(channel(name)))

    def test_keep_non_news_documentary(self):
        self.assertFalse(is_news_or_music(channel("Bloomberg Originals", group="Documentary")))

    def test_keep_strict_language_restriction(self):
        self.assertFalse(channel("BBC One", "eng;spa").is_preferred_language)
        self.assertTrue(channel("BBC One", "eng").is_preferred_language)
        self.assertTrue(channel("BBC One", "msa").is_preferred_language)


if __name__ == "__main__":
    unittest.main()
