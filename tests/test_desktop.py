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
