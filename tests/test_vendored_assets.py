"""Vendored-asset integrity guards (weakness audit W10, 2026-07-12).

"Zero cloud dependency" and "zero telemetry - grep it" are launch-post
claims. Before this fix, ui.html loaded marked, CodeMirror, Monaco, and
Vega from cdn.jsdelivr.net at page load - every page view sent IP + UA
to a third party and the app broke offline. All third-party frontend
assets now live in desktop_app/vendor/ (pinned versions + SHA-256 in
vendor/MANIFEST.md) and are served by the /vendor/<path> Flask route.

These tests pin three facts:
1. ui.html performs ZERO external loads (no script/link/css-url pointed
   at an http(s) host).
2. Every /vendor/ path ui.html references exists on disk, so a typo'd
   rewrite cannot silently regress to the loader fallbacks.
3. The /vendor route serves assets and rejects path traversal.

If a new frontend library is ever added: vendor it, extend MANIFEST.md,
and reference it via /vendor/ - never a CDN URL.
"""

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
UI_HTML = PROJECT_ROOT / "desktop_app" / "ui.html"
VENDOR_DIR = PROJECT_ROOT / "desktop_app" / "vendor"

# Hostnames that must never appear in ui.html in any load-bearing form.
FORBIDDEN_HOSTS = (
    "cdn.jsdelivr.net",
    "unpkg.com",
    "cdnjs.cloudflare.com",
    "fonts.googleapis.com",
    "fonts.gstatic.com",
)


def _ui_text():
    return UI_HTML.read_text(encoding="utf-8")


class TestNoExternalPageLoads:
    def test_no_cdn_hostnames_anywhere(self):
        text = _ui_text()
        for host in FORBIDDEN_HOSTS:
            assert host not in text, (
                f"ui.html references {host} - vendor the asset into "
                f"desktop_app/vendor/ instead (W10)"
            )

    def test_no_script_src_to_external_host(self):
        text = _ui_text()
        externals = re.findall(r"<script[^>]+src=[\"'](https?:)?//[^\"']+", text)
        assert externals == [], (
            f"External <script src> found in ui.html: {externals} - page "
            f"load must make zero external requests (W10)"
        )

    def test_no_link_href_to_external_host(self):
        text = _ui_text()
        externals = re.findall(r"<link[^>]+href=[\"'](https?:)?//[^\"']+", text)
        assert externals == [], (
            f"External <link href> found in ui.html: {externals} (W10)"
        )

    def test_no_css_url_to_external_host(self):
        text = _ui_text()
        externals = re.findall(r"url\([\"']?(https?:)?//[^)\"']+", text)
        assert externals == [], (
            f"External CSS url() found in ui.html: {externals} (W10)"
        )

    def test_no_cdn_hostnames_in_server_side_html_builders(self):
        # server.py builds standalone HTML (notebook export) - it must
        # inline the vendored renderer, never bootstrap from a CDN.
        server_text = (
            PROJECT_ROOT / "desktop_app" / "server.py"
        ).read_text(encoding="utf-8")
        for host in FORBIDDEN_HOSTS:
            assert host not in server_text, (
                f"desktop_app/server.py references {host} - generated "
                f"HTML must inline vendored assets instead (W10)"
            )

    def test_lazy_loader_bases_are_local(self):
        text = _ui_text()
        monaco_base = re.search(r"const MONACO_BASE = '([^']+)'", text)
        vega_base = re.search(r"const VEGA_BASE = '([^']+)'", text)
        assert monaco_base and monaco_base.group(1).startswith("/vendor/"), (
            "MONACO_BASE must point at the vendored tree, not a CDN"
        )
        assert vega_base and vega_base.group(1).startswith("/vendor/"), (
            "VEGA_BASE must point at the vendored tree, not a CDN"
        )


class TestVendoredFilesExist:
    def test_manifest_exists_and_pins_versions(self):
        manifest = (VENDOR_DIR / "MANIFEST.md").read_text(encoding="utf-8")
        for package in ("marked", "codemirror", "monaco-editor", "vega-embed"):
            assert package in manifest, f"vendor/MANIFEST.md missing {package}"
        assert "sha256" in manifest.lower(), (
            "vendor/MANIFEST.md must carry file hashes for auditability"
        )

    def test_every_vendor_reference_in_ui_html_exists_on_disk(self):
        text = _ui_text()
        refs = set(re.findall(r"[\"']/vendor/([^\"']+\.(?:js|css))[\"']", text))
        assert refs, "expected ui.html to reference /vendor/ assets"
        missing = [ref for ref in refs if not (VENDOR_DIR / ref).is_file()]
        assert missing == [], (
            f"ui.html references vendored files that do not exist: {missing}"
        )

    def test_monaco_loader_and_editor_main_present(self):
        # The Monaco AMD loader pulls these lazily; a pruned vendor tree
        # would pass the static-reference test above but break at runtime.
        for rel in (
            "monaco-editor/min/vs/loader.js",
            "monaco-editor/min/vs/editor/editor.main.js",
            "monaco-editor/min/vs/basic-languages/python/python.js",
            "monaco-editor/min/vs/basic-languages/yaml/yaml.js",
            "monaco-editor/min/vs/basic-languages/markdown/markdown.js",
            "monaco-editor/min/vs/language/json/jsonMode.js",
        ):
            assert (VENDOR_DIR / rel).is_file(), (
                f"vendored Monaco tree is missing {rel} - the notebook "
                f"editor loads it lazily at runtime"
            )


class TestVendorRoute:
    def test_vendor_route_serves_asset(self, client):
        response = client.get("/vendor/marked/marked.min.js")
        assert response.status_code == 200
        assert b"marked" in response.data[:4096].lower() or len(response.data) > 1000

    def test_vendor_route_sets_cache_header(self, client):
        response = client.get("/vendor/marked/marked.min.js")
        assert "max-age" in response.headers.get("Cache-Control", "")

    def test_vendor_route_rejects_path_traversal(self, client):
        response = client.get("/vendor/..%2Fserver.py")
        assert response.status_code in (400, 404)

    def test_vendor_route_404_for_missing_file(self, client):
        response = client.get("/vendor/nope/missing.js")
        assert response.status_code == 404
