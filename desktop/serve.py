"""Character-sheet frontend, layered on the existing conversation API."""
from pathlib import Path
import json
import os
import re
import sys
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import server as api


def pet_dirs():
    """Built-in catalogue first, then sheets the user imported (PHRO_PETS_DIR, set by Electron)."""
    imported = os.getenv('PHRO_PETS_DIR')
    return [ROOT / 'desktop' / 'pets'] + ([Path(imported)] if imported else [])


def catalog():
    pets = json.loads((ROOT / 'desktop' / 'pets' / 'catalog.json').read_text(encoding='utf-8'))
    known = {p['id'] for p in pets}
    if len(pet_dirs()) > 1:
        try:
            imported = json.loads((pet_dirs()[1] / 'catalog.json').read_text(encoding='utf-8'))
        except FileNotFoundError:
            imported = []
        except ValueError:
            # A damaged import list must not take the built-in characters down with it.
            print('ignoring unreadable imported pet catalog', file=sys.stderr, flush=True)
            imported = []
        pets += [p for p in imported if isinstance(p, dict) and p.get('id') not in known]
    known = {p.get('id') for p in pets if isinstance(p, dict)}
    for folder in pet_dirs():
        for pet in discovered(folder, known):
            known.add(pet['id'])
            pets.append(pet)
    return pets


PET_ID = r'[a-z0-9][a-z0-9_-]{0,79}'
SIZES = {(1536, 2288): 2, (1536, 1872): 1}  # sprite.js validSheet()


def webp_size(head):
    """(width, height) from a WebP header (lossy VP8, lossless VP8L or extended VP8X); desktop/pets.cjs webpSize."""
    if len(head) < 30 or head[:4] != b'RIFF' or head[8:12] != b'WEBP':
        raise ValueError('not a WebP image')
    chunk = head[12:16]
    if chunk == b'VP8 ':
        return int.from_bytes(head[26:28], 'little') & 0x3fff, int.from_bytes(head[28:30], 'little') & 0x3fff
    if chunk == b'VP8L':
        bits = int.from_bytes(head[21:25], 'little')
        return (bits & 0x3fff) + 1, ((bits >> 14) & 0x3fff) + 1
    if chunk == b'VP8X':
        return int.from_bytes(head[24:27], 'little') + 1, int.from_bytes(head[27:30], 'little') + 1
    raise ValueError('unsupported WebP layout')


def discovered(folder, known):
    """Sheet folders dropped into a pets folder without a catalogue entry: <id>/spritesheet.webp, the folder
    name being the id the sheet is served under. pet.json (optional) gives the display name and author.
    Folders whose sheet is not a supported layout are skipped and reported."""
    if not folder.is_dir():
        return
    for path in sorted(folder.iterdir()):
        sheet = path / 'spritesheet.webp'
        if path.name in known or not re.fullmatch(PET_ID, path.name) or not sheet.is_file():
            continue
        try:
            with open(sheet, 'rb') as f:
                version = SIZES.get(webp_size(f.read(30)))
            if not version:
                raise ValueError('expected 1536x2288 (V2) or 1536x1872 (V1)')
        except ValueError as exc:
            print(f'skipping pet folder {path.name}: {exc}', file=sys.stderr, flush=True)
            continue
        try:
            manifest = json.loads((path / 'pet.json').read_text(encoding='utf-8')[:65536])
        except (OSError, ValueError):
            manifest = {}
        if not isinstance(manifest, dict):
            manifest = {}
        yield {'id': path.name, 'name': str(manifest.get('displayName') or path.name)[:80],
               'version': version, 'author': str(manifest.get('author') or '')[:80], 'source': 'local',
               'sheet': f'pets/{path.name}/spritesheet.webp'}


class DesktopHandler(api.Handler):
    def do_GET(self):
        if not self._local_request():
            return
        path = unquote(urlsplit(self.path).path)
        files = {'/': 'desktop/index.html', '/app.js': 'desktop/app.js', '/manage.js': 'desktop/manage.js',
                 '/sprite.js': 'desktop/sprite.js', '/style.css': 'desktop/style.css',
                 '/overlay.html': 'desktop/overlay.html', '/overlay.js': 'desktop/overlay.js',
                 '/overlay.css': 'desktop/overlay.css',
                 '/trace.html': 'desktop/trace.html', '/trace.js': 'desktop/trace.js', '/trace.css': 'desktop/trace.css',
                 '/pipeline.js': 'desktop/pipeline.js'}
        target = ROOT / files[path] if path in files else None
        if path.startswith('/pets/') and path.endswith('/spritesheet.webp'):
            name = path.split('/')
            if len(name) == 4 and re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,79}', name[2]):
                # Built-in sheets win; imported ones never shadow them (desktop/main.cjs refuses such ids).
                for folder in pet_dirs():
                    candidate = (folder / name[2] / 'spritesheet.webp').resolve()
                    if candidate.is_relative_to(folder.resolve()) and candidate.is_file():
                        target = candidate
                        break
                else:
                    return self._send(404, {'error': 'asset not installed'})
        if path == '/pets/catalog.json':
            blob, suffix = json.dumps(catalog(), ensure_ascii=False).encode(), '.json'
        elif target is None:
            return super().do_GET()
        elif not target.is_file():
            return self._send(404, {'error': 'asset not installed'})
        else:
            blob, suffix = target.read_bytes(), target.suffix
        self.send_response(200)
        self.send_header('Content-Type', {'.html':'text/html; charset=utf-8', '.js':'text/javascript',
            '.css':'text/css', '.json':'application/json', '.webp':'image/webp'}[suffix])
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.send_header('Content-Length', str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)


if __name__ == '__main__':
    api.PORT = int(os.getenv('PHRO_DESKTOP_PORT', '8771'))
    api.main(DesktopHandler)
