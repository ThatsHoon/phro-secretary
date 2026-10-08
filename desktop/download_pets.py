"""Fetch the explicitly catalogued community image kits, never execute package contents."""
from pathlib import Path
from urllib.request import Request, urlopen
import hashlib
import io
import json
import re
import sys
import zipfile


def main():
    """Optional argument: destination folder (the installed app passes its per-user pets folder)."""
    root=Path(__file__).resolve().parent/'pets'
    dest=Path(sys.argv[1]) if len(sys.argv)>1 else root
    for pet in json.loads((root/'catalog.json').read_text(encoding='utf-8')):
        if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,79}',pet['id']):
            raise ValueError('invalid pet id')
        # The site answers 403 to urllib's default User-Agent; identify the app instead.
        with urlopen(Request(pet['download'],headers={'User-Agent':'phro-secretary/0.1 pet-catalog'}),timeout=60) as response:
            data=response.read(21*1024*1024)
        if len(data)>20*1024*1024:
            raise ValueError('kit too large')
        with zipfile.ZipFile(io.BytesIO(data)) as kit:
            for name,limit in [('spritesheet.webp',20*1024*1024),('pet.json',65536)]:
                if kit.getinfo(name).file_size>limit:
                    raise ValueError('kit entry too large')
            sheet=kit.read('spritesheet.webp')
            metadata=kit.read('pet.json')
        if hashlib.sha256(sheet).hexdigest()!=pet['sha256']:
            raise ValueError('sheet changed; review source before updating catalog')
        if json.loads(metadata)['id']!=pet['id']:
            raise ValueError('manifest identity mismatch')
        folder=dest/pet['id'];folder.mkdir(parents=True,exist_ok=True)
        (folder/'spritesheet.webp').write_bytes(sheet)
        (folder/'pet.json').write_bytes(metadata)
        print('Installed',pet['id'])


if __name__=='__main__':
    main()
