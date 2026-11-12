"""Security checks for dlus that run in CI (python3 -m unittest discover tests). Standard library only."""
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DLM_HOME", tempfile.mkdtemp(prefix="dlus-test-"))
sys.argv = ["dlus"]
spec = importlib.util.spec_from_file_location("dlus", os.path.join(ROOT, "dlus.py"))
dlus = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dlus)
SRC = open(os.path.join(ROOT, "dlus.py"), encoding="utf-8").read()
GW = open(os.path.join(ROOT, "gateway", "dlus-gw"), encoding="utf-8").read()
INSTALL = open(os.path.join(ROOT, "gateway", "install.sh"), encoding="utf-8").read()


class FileNames(unittest.TestCase):
    def test_no_path_escape(self):
        for bad in ("../../etc/passwd", "/etc/passwd", "..\\..\\x", ".hidden", "a/../../b"):
            n = dlus.safe_name(bad)
            self.assertNotIn("/", n)
            self.assertFalse(n.startswith("."), n)

    def test_no_control_or_direction_characters(self):
        self.assertEqual(dlus.safe_name("photo\u202egpj.exe"), "photogpj.exe")  # right-to-left override trick
        self.assertEqual(dlus.safe_name("a\nb\r\x00c.iso"), "abc.iso")

    def test_never_empty(self):
        self.assertEqual(dlus.safe_name("..."), "download")


class Checksums(unittest.TestCase):
    H = "ab" * 32

    def test_formats(self):
        for t in ("sha256:" + self.H, "SHA-256: " + self.H.upper(), self.H, "SHA256 (f.iso) = " + self.H, self.H + "  f.iso"):
            self.assertEqual(dlus.parse_checksum(t, ("f.iso",)), ("sha256", self.H), t)

    def test_other_files_line_is_ignored(self):
        self.assertIsNone(dlus.parse_checksum(self.H + "  other.iso", ("f.iso",), strict=True))
        sums = "%s  a.iso\n%s *f.iso\n" % ("cd" * 32, self.H)
        self.assertEqual(dlus.parse_checksum(sums, ("f.iso",), strict=True), ("sha256", self.H))

    def test_ambiguous_list_is_refused(self):
        self.assertIsNone(dlus.parse_checksum("%s\n%s" % (self.H, "cd" * 32), ("f.iso",), strict=True))

    def test_algorithm_must_match_length(self):
        self.assertIsNone(dlus.parse_checksum("md5:" + self.H))
        self.assertIsNone(dlus.parse_checksum("not a checksum"))

    def test_only_web_links_are_fetched(self):
        for u in ("file:///etc/passwd", "ftp://x/y", "javascript:alert(1)"):
            with self.assertRaises(ValueError):
                dlus.fetch_small(u)


class SharedFolders(unittest.TestCase):
    def test_private_and_system_folders_cannot_be_shared(self):
        home = os.path.realpath(os.path.expanduser("~"))
        for f in ("/", "/Users", "/etc", "/private/etc", "/System", "/Library", home + "/.ssh", home + "/Library",
                  os.path.realpath(dlus.STATE_DIR), os.path.dirname(os.path.realpath(dlus.STATE_DIR))):
            self.assertFalse(dlus.share_ok(f), f)
        self.assertTrue(dlus.share_ok("/Volumes/Media/Films"))

    def test_hidden_files_are_never_served(self):
        self.assertTrue(dlus.hidden_inside("/m", "/m/a/.ssh/id_ed25519"))
        self.assertTrue(dlus.hidden_inside("/m", "/m/.git/config"))
        self.assertFalse(dlus.hidden_inside("/m", "/m/a/film.mkv"))


class Network(unittest.TestCase):
    def test_curl_is_locked_down(self):
        c = dlus.CURL
        self.assertEqual(c[0], "/usr/bin/curl")
        self.assertEqual(c[1], "-q")  # ignores ~/.curlrc
        self.assertIn("--proto", c)
        self.assertNotIn("-k", c)
        self.assertNotIn("--insecure", c)
        self.assertIn('["--proto-redir", "=https"]', SRC)  # https links never downgrade to http

    def test_tls_verification_only_off_for_the_pinned_tv(self):
        # the LG TV's remote-control port has a self-signed certificate: the one exception, pinned at pairing instead
        self.assertEqual(SRC.count("ssl.CERT_NONE"), 1)
        self.assertNotRegex(SRC, r"_create_unverified_context|verify\s*=\s*False")
        self.assertIn("getpeercert(binary_form=True)", SRC)
        self.assertIn("raise TVCertChanged(", SRC)

    def test_no_shell_execution(self):
        self.assertNotRegex(SRC, r"shell\s*=\s*True|os\.system\(|os\.popen\(")

    def test_ui_server_only_on_localhost(self):
        self.assertIn('ThreadingHTTPServer(("127.0.0.1", args.port)', SRC)

    def test_secrets_compared_in_constant_time(self):
        self.assertGreaterEqual(SRC.count("secrets.compare_digest("), 6)

    def test_downloads_are_quarantined(self):
        self.assertIn("com.apple.quarantine", SRC)
        self.assertRegex(SRC, r"quarantine\(self\.out\)")


class RootHelper(unittest.TestCase):
    def test_no_route_to(self):
        rules = re.findall(r'echo "([^"]*)"', GW)
        self.assertFalse([r for r in rules if "route-to" in r], "pf route-to panics macOS 26 with utun interfaces")

    def test_strict_inputs(self):
        for re_name in ("IP_RE", "CIDR_RE", "IF_RE"):
            self.assertRegex(GW, r"%s='\^" % re_name)
        self.assertIn("PATH=/usr/sbin:/usr/bin:/sbin:/bin", GW)

    def test_unlisted_devices_cannot_route(self):
        self.assertIn("block drop in quick on $lan inet from <dlus_lan> to ! <dlus_local>", GW)

    def test_installs_into_root_only_folder(self):
        self.assertIn("DEST_DIR=/Library/PrivilegedHelperTools", INSTALL)
        self.assertIn("not root-only", INSTALL)
        self.assertIn("visudo -cf", INSTALL)

    def test_shell_scripts_parse(self):
        for f in ("gateway/dlus-gw", "gateway/install.sh", "gateway/uninstall.sh", "macos/setup.sh"):
            subprocess.run(["bash", "-n", os.path.join(ROOT, f)], check=True)


if __name__ == "__main__":
    unittest.main()
