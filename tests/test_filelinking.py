"""Tests for nemorosa.filelinking path-element sanitization."""

from nemorosa.filelinking import _sanitize_rel_path, sanitize_path_element

LRM = chr(0x200E)  # LEFT-TO-RIGHT MARK (category Cf)
ZWSP = chr(0x200B)  # ZERO WIDTH SPACE (category Cf)
BEL = chr(0x07)  # control character (category Cc)


class TestSanitizePathElement:
    """sanitize_path_element strips only Unicode control/format chars (Cc/Cf)."""

    def test_strips_left_to_right_mark(self) -> None:
        # LRM lands on disk from the source but isn't in the target .torrent name;
        # libtorrent-based clients strip it, so the link path must too.
        assert (
            sanitize_path_element(f"Artist {LRM}- Album [FLAC]")
            == "Artist - Album [FLAC]"
        )

    def test_leaves_accents_and_dashes_untouched(self) -> None:
        # Letters (ô, ç) and punctuation (en-dash) are NOT Cc/Cf — must be preserved.
        name = "Milton Nascimento, Lô Borges – Clube Da Esquina (2014) [CD-FLAC]"
        assert sanitize_path_element(name) == name

    def test_strips_zero_width_and_control_chars(self) -> None:
        assert sanitize_path_element(f"a{ZWSP}{BEL}b") == "ab"

    def test_rel_path_sanitizes_each_component(self) -> None:
        assert (
            _sanitize_rel_path(f"Disc {LRM}1/01 - Song.flac") == "Disc 1/01 - Song.flac"
        )
