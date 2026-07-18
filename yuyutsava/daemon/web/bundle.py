"""Bundle-relative asset serving for multi-file artifacts.

An interactive HTML artifact is often not one file. A Claude "distributable
component" (``*.dc.html``), for instance, is a template plus a sibling
``support.js`` runtime next to it on disk, and it pulls React off a CDN at
runtime. Serving only the one attached file — which is all
``GET /todos/{card}/attachments/{id}`` does — hands the browser a document whose
``<script src="./support.js">`` has nothing to resolve against, so the runtime
never boots and the template's ``{{ … }}`` bindings render literally.

So the bytes routes get a ``…/bundle/{rel_path}`` sibling that serves any file
from the *directory the artifact lives in*, keyed off the record's own path. The
frame then loads the primary file at ``…/bundle/<basename>`` and every relative
reference in it resolves against a real URL, exactly as it would from a folder on
disk. Both bytes routes (card attachments and general artifacts) share this
module so the traversal guard below has exactly one implementation.

## Security posture

The directory is exposed, so the guard is the whole point: the resolved target
must sit inside the resolved base. ``.resolve()`` runs first, which also defeats
a symlink inside the bundle pointing out of it, and an absolute ``rel_path``
(``/etc/passwd``) resolves outside the base and is rejected the same way.

Nothing here authenticates: loopback binds are unauthenticated by design (see
``web/app.py``) — an iframe ``src`` navigation cannot carry a bearer header
anyway, which is why the existing ``<img src>`` attachment previews work. The
framed document is contained by the *renderer* instead, which loads it under
``sandbox="allow-scripts"`` WITHOUT ``allow-same-origin``: the document gets an
opaque origin, so it cannot script the app, read its storage, or borrow the
daemon's origin. It CAN reach the network (that is the deliberate trade — an
artifact that needs a CDN should work), but it cannot usefully call the local API:
an opaque origin sends ``Origin: null``, which fails the CORS loopback regex, and
every mutating endpoint is JSON/DELETE and therefore preflighted.

No CSP is injected on the way out. The artifact is meant to render exactly as it
would in a browser; a hand-rolled policy here would have to enumerate the
daemon's own origin (``'self'`` matches nothing from an opaque origin) and would
silently break the next artifact that loads something we failed to predict —
which is the very bug this module exists to fix.

One thing IS injected: a tiny error-guard ``<script>`` at the top of ``<head>``
in HTML entry documents. It only adds observability — passive ``error`` /
``unhandledrejection`` listeners that paint a banner when an uncaught script
error fires (e.g. a button wired to an undefined handler). Without it, a runtime
like DC's ``support.js`` can tear down the document body on failure and the
frame just goes blank, with no way for the renderer to see why (the opaque
origin blocks all introspection). The guard never calls ``preventDefault`` and
never touches page content, so rendering stays exactly as-in-browser.
"""

from __future__ import annotations

import mimetypes
import re
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse, HTMLResponse

_HEAD_RE = re.compile(r"<head[^>]*>", re.IGNORECASE)
_HTML_RE = re.compile(r"<html[^>]*>", re.IGNORECASE)

# Passive listeners (never window.onerror assignment, never preventDefault) so
# whatever handlers the artifact's own runtime installs keep working. Bubble
# phase only: resource-load errors don't bubble to window, so a missing favicon
# or optional CDN asset can't false-positive — only uncaught script exceptions
# surface. The banner hangs off documentElement, not body, so it survives a
# runtime that wipes the body on failure.
_ERROR_GUARD = """<script>/* injected by yuyutsava bundle serving — see bundle.py */
(function () {
  function show(msg) {
    var el = document.getElementById('__yy_artifact_err');
    if (!el) {
      el = document.createElement('div');
      el.id = '__yy_artifact_err';
      el.style.cssText = 'position:fixed;left:0;right:0;bottom:0;z-index:2147483647;' +
        'background:#2a1420;color:#ff8fa8;font:12px/1.5 ui-monospace,Menlo,monospace;' +
        'padding:8px 12px;border-top:1px solid #ff5577;white-space:pre-wrap;word-break:break-word;';
      document.documentElement.appendChild(el);
    }
    el.textContent = 'artifact script error: ' + msg;
  }
  window.addEventListener('error', function (e) { show((e && e.message) || 'unknown error'); });
  window.addEventListener('unhandledrejection', function (e) {
    var r = e && e.reason;
    show((r && (r.message || String(r))) || 'unhandled promise rejection');
  });
})();
</script>"""


def _inject_error_guard(html: str) -> str:
    """*html* with the error-guard script spliced in ahead of any page script."""
    m = _HEAD_RE.search(html)
    if m:
        return html[: m.end()] + _ERROR_GUARD + html[m.end():]
    m = _HTML_RE.search(html)
    if m:
        return html[: m.end()] + "<head>" + _ERROR_GUARD + "</head>" + html[m.end():]
    return _ERROR_GUARD + html


def resolve_bundle_asset(primary_path: str | None, rel_path: str) -> Path:
    """The file *rel_path* names inside the directory holding *primary_path*.

    Raises 404 when the record has no on-disk file or the target does not exist,
    and 403 when *rel_path* escapes the bundle directory.
    """
    if not primary_path:
        raise HTTPException(status_code=404, detail="artifact has no servable file")

    base = Path(primary_path).resolve().parent
    # An absolute or ".."-laden rel_path resolves outside base and is caught by
    # the containment check below, so no pre-sanitising of the string is needed.
    target = (base / rel_path).resolve()

    if target != base and base not in target.parents:
        raise HTTPException(
            status_code=403, detail=f"{rel_path!r} escapes the artifact's bundle directory"
        )
    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"no bundle file {rel_path!r}")
    return target


def bundle_asset_response(primary_path: str | None, rel_path: str) -> FileResponse | HTMLResponse:
    """Serve a bundle-relative asset with a guessed content type.

    The type is guessed from the target's own name rather than inherited from the
    record's mime — a bundle mixes html/js/css/images and the record only
    describes its primary file. ``Face Rig.dc.html`` guesses off the trailing
    ``.html``, so DC documents serve as ``text/html``. HTML documents get the
    error-guard script spliced in (see module docstring); everything else streams
    verbatim.
    """
    target = resolve_bundle_asset(primary_path, rel_path)
    media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    if media_type == "text/html":
        try:
            html = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return FileResponse(target, media_type=media_type)
        return HTMLResponse(_inject_error_guard(html))
    return FileResponse(target, media_type=media_type)


__all__ = ["bundle_asset_response", "resolve_bundle_asset"]
