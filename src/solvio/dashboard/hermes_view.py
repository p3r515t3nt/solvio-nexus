"""Pinned native Hermes message renderer; no Hermes API or session startup."""
from pathlib import Path
from aiohttp import web

ROOT = Path(__file__).with_name('assets') / 'hermes'
SHELL_ASSETS = {'solvio-workspace.js': ROOT.parent / 'hermes-workspace.js',
                'solvio-workspace.css': ROOT.parent / 'hermes-workspace.css'}
TYPES = {'.html':'text/html','.js':'text/javascript','.css':'text/css',
         '.woff2':'font/woff2','.woff':'font/woff','.ttf':'font/ttf','.txt':'text/plain'}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "connect-src 'self'; font-src 'self'; img-src 'self'; media-src 'self' blob:; frame-ancestors 'self'; "
       "base-uri 'none'; form-action 'none'; object-src 'none'")


def attach(app):
    async def asset(request):
        name = request.match_info['name']
        parts = name.split('/')
        if (not request.secure or any(p in {'', '.', '..'} for p in parts)
                or len(parts)>2 or '\\' in name):
            raise web.HTTPNotFound()
        path = SHELL_ASSETS.get(name, ROOT.joinpath(*parts))
        if path.is_symlink() or not path.is_file() or path.suffix not in TYPES:
            raise web.HTTPNotFound()
        if path.suffix=='.html' and name!='solvio-view.html':
            raise web.HTTPNotFound()
        if path.suffix=='.txt' and name not in {'LICENSE-Hermes.txt', 'LICENSE-JetBrainsMono-OFL.txt', 'THIRD-PARTY-LICENSES.txt'}:
            raise web.HTTPNotFound()
        body = path.read_bytes()
        if name == 'solvio-view.html':
            # The generated package is pinned to original Desktop sources.
            # Only this shell reads Core projections; no native session starts.
            body = body.replace(b'</head>', b'<link rel="stylesheet" href="/dashboard/hermes/solvio-workspace.css">'
                b'<script type="module" src="/dashboard/hermes/solvio-workspace.js"></script></head>')
            body = body.replace(b'<div id="root"></div>',
                b'<section id="solvio-workspace" aria-label="Hermes-Arbeitsraum"></section><div id="root" hidden></div>')
        return web.Response(body=body,content_type=TYPES[path.suffix])
    app.router.add_get('/dashboard/hermes/{name:.+}',asset)
