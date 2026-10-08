"""Manage confirmed memory through a running local server (server 8770, desktop 8771)."""
import argparse
import json
import sys
from urllib.parse import urlsplit
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def call(server, path, body=None):
    req = Request(server.rstrip('/')+'/'+path, data=None if body is None else json.dumps(body).encode(),
                  headers={'Content-Type':'application/json'})
    with urlopen(req,timeout=30) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['search','forget','list','show','restore','purge'])
    parser.add_argument('values',nargs='*')
    parser.add_argument('--reason',default='')
    parser.add_argument('--message',action='store_true')
    parser.add_argument('--yes',action='store_true')
    parser.add_argument('--server',default='http://127.0.0.1:8770',
                        help='server base URL; the desktop app uses http://127.0.0.1:8771')
    args=parser.parse_args()
    # The server only answers loopback requests anyway; refuse anything else before sending memory IDs.
    target = urlsplit(args.server)
    if target.scheme!='http' or target.hostname not in ('127.0.0.1','localhost','::1'):
        parser.error('--server must be a local http:// address')
    routes={'search':'forget_search','list':'forget_batches','show':'forget_batch'}
    body={}
    if args.action=='search': body={'query':' '.join(args.values)}
    if args.action=='forget': body={'message_ids' if args.message else 'memory_ids':list(map(int,args.values)), 'reason':args.reason}
    if args.action in ('show','restore','purge'):
        if len(args.values)!=1: parser.error('one batch ID required')
        body={'batch':int(args.values[0])}
    if args.action=='purge':
        if not args.yes: parser.error('purge requires --yes')
        body['confirm']=True
    if args.action in ('forget','restore','purge'):
        # Changes go to whichever DB that server owns; say which, so a test DB is not mistaken for the real one.
        print('server', args.server, 'db', call(args.server,'health')['db'], file=sys.stderr)
    try:
        result = call(args.server,routes.get(args.action,args.action),body)
    except HTTPError as exc:
        sys.exit(f'server refused: {exc.read().decode(errors="replace")}')
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__': main()
