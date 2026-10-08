"""
tests/test_urlcheck.py — SSRF guard for provider base URLs. Pure logic.

Run from the project root:   python3 -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.urlcheck import is_local_url, validate_base_url


class ValidateBaseUrlTests(unittest.TestCase):
    def test_normal_urls_pass_and_are_normalised(self):
        for raw, want in [
            ("https://codecraftapi.com/v1", "https://codecraftapi.com/v1"),
            ("  https://api.openai.com/v1/  ", "https://api.openai.com/v1"),
            ("http://localhost:11434/v1", "http://localhost:11434/v1"),
            ("http://127.0.0.1:1234/v1", "http://127.0.0.1:1234/v1"),
            ("http://192.168.1.20:8000/v1", "http://192.168.1.20:8000/v1"),
            ("https://[2606:4700::1111]/v1", "https://[2606:4700::1111]/v1"),
            ("https://api.example.com", "https://api.example.com"),
        ]:
            self.assertEqual(validate_base_url(raw), want, raw)

    def test_rejects_non_http_schemes_and_junk(self):
        for bad in ["", "   ", "ftp://x.com/v1", "file:///etc/passwd", "javascript:alert(1)",
                    "gopher://x", "codecraftapi.com/v1", "//x.com/v1", "http://", "https:///v1",
                    "http://exa mple.com", "http://x.com/\nHost: evil", "http://x.com:notaport/v1",
                    "http://x.com:99999/v1", "x" * 3000, None]:
            with self.assertRaises(ValueError, msg=repr(bad)[:60]):
                validate_base_url(bad)

    def test_rejects_embedded_credentials(self):
        for bad in ["https://user:pass@api.example.com/v1", "https://user@api.example.com/v1",
                    "https://:pass@api.example.com/v1"]:
            with self.assertRaises(ValueError, msg=bad):
                validate_base_url(bad)

    def test_blocks_cloud_metadata_addresses(self):
        for bad in [
            "http://169.254.169.254/latest/meta-data",       # AWS / Oracle / Azure / GCP
            "http://169.254.169.254:80/opc/v2/instance",
            "http://169.254.0.1/", "http://169.254.255.255/",
            "http://100.100.100.200/",                        # Alibaba
            "http://[fd00:ec2::254]/",                        # AWS IPv6
            "http://[fe80::1]/", "http://[::ffff:169.254.169.254]/",
            "http://metadata.google.internal/computeMetadata/v1",
            "http://METADATA.GOOGLE.INTERNAL./x", "http://instance-data/latest",
            "http://0.0.0.0/", "http://[::]/",
        ]:
            with self.assertRaises(ValueError, msg=bad):
                validate_base_url(bad)

    def test_blocks_legacy_numeric_encodings_of_the_metadata_address(self):
        # All of these ARE 169.254.169.254 to a browser / libcurl.
        for bad in [
            "http://2852039166/",             # single 32-bit integer
            "http://0xa9fea9fe/",             # hex integer
            "http://0251.0376.0251.0376/",    # octal dotted
            "http://0xa9.0xfe.0xa9.0xfe/",    # hex dotted
            "http://169.254.43518/",          # a.b.c form (c is 16 bits)
            "http://169.16689662/",           # a.b form (b is 24 bits)
            "http://0xA9.254.169.254/",       # mixed
        ]:
            with self.assertRaises(ValueError, msg=bad):
                validate_base_url(bad)

    def test_similar_but_legitimate_addresses_are_not_blocked(self):
        for ok in ["http://169.253.1.1/v1", "http://170.254.1.1/v1", "http://168.254.169.254/v1",
                   "http://100.100.100.201/v1", "http://10.0.0.5:8000/v1", "http://172.16.0.9/v1"]:
            self.assertEqual(validate_base_url(ok), ok.rstrip("/"), ok)

    def test_out_of_range_numeric_forms_are_not_misread(self):
        # 256 isn't a valid octet: not an IP, so it's just an (odd) hostname, not a false block.
        self.assertEqual(validate_base_url("http://256.1.1.1/v1"), "http://256.1.1.1/v1")


class IsLocalUrlTests(unittest.TestCase):
    def test_local(self):
        for u in ["http://localhost:11434/v1", "http://127.0.0.1:1234", "http://[::1]:8000/v1",
                  "http://192.168.0.4/v1", "http://10.1.2.3/v1", "http://mybox.local:8000/v1"]:
            self.assertTrue(is_local_url(u), u)

    def test_remote(self):
        for u in ["https://api.openai.com/v1", "https://codecraftapi.com/v1", "http://8.8.8.8/v1", "nonsense"]:
            self.assertFalse(is_local_url(u), u)


if __name__ == "__main__":
    unittest.main(verbosity=2)
