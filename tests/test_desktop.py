import threading
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import pytest
from desktop.serve import DesktopHandler
from server import SERVER_CLASS


def test_sheet_routes_are_allowlisted_and_same_origin():
    server=SERVER_CLASS(('127.0.0.1',0),DesktopHandler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base='http://127.0.0.1:'+str(server.server_address[1])
    try:
        for path in ('/', '/sprite.js', '/pipeline.js', '/pets/catalog.json'):
            with urlopen(base+path) as response:
                assert response.status==200
                assert "default-src 'self'" in response.headers['Content-Security-Policy']
        for path in ('/pets/../../server.py','/main.cjs','/serve.py','/avatar3d/'):
            with pytest.raises(HTTPError) as err: urlopen(base+path)
            assert err.value.code==404
        with pytest.raises(HTTPError) as err:
            urlopen(Request(base+'/',headers={'Origin':'https://evil.example'}))
        assert err.value.code==403
    finally:
        server.shutdown();server.server_close();thread.join()


def test_dropped_sheet_folders_join_the_catalog(tmp_path, monkeypatch):
    from desktop import serve

    def sheet(folder, width, height, manifest=None):
        folder.mkdir()
        head = bytearray(30)
        head[0:4], head[8:16] = b'RIFF', b'WEBPVP8X'
        head[24:27], head[27:30] = (width - 1).to_bytes(3, 'little'), (height - 1).to_bytes(3, 'little')
        (folder / 'spritesheet.webp').write_bytes(bytes(head))
        if manifest is not None:
            (folder / 'pet.json').write_text(manifest, encoding='utf-8')

    imported = tmp_path / 'imported'
    imported.mkdir()
    sheet(imported / 'new-cat', 1536, 2288, '{"displayName": "새 고양이", "author": "me"}')
    sheet(imported / 'bare', 1536, 1872)                 # no pet.json: named after its folder
    sheet(imported / 'broken', 1536, 1872, '{not json')  # unreadable pet.json: same
    sheet(imported / 'odd-size', 100, 100)               # unsupported layout: skipped
    sheet(imported / 'Bad Name', 1536, 2288)             # not a servable id: skipped
    (imported / 'empty').mkdir()                         # no sheet: skipped
    monkeypatch.setenv('PHRO_PETS_DIR', str(imported))
    pets = {p['id']: p for p in serve.catalog()}
    assert pets['new-cat'] == {'id': 'new-cat', 'name': '새 고양이', 'version': 2, 'author': 'me', 'source': 'local',
                               'sheet': 'pets/new-cat/spritesheet.webp'}
    assert (pets['bare']['name'], pets['bare']['version']) == ('bare', 1) and pets['broken']['name'] == 'broken'
    assert not {'odd-size', 'Bad Name', 'empty'} & pets.keys()
    # Catalogued characters keep their entries; a folder never duplicates one.
    builtin = [p['id'] for p in serve.catalog() if p.get('source') != 'local']
    assert builtin and len(serve.catalog()) == len(pets)
