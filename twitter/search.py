import asyncio
import logging.config
import math
import platform
import random
import re
import time
from logging import Logger
from pathlib import Path
from typing import Tuple, Any

import orjson
from httpx import AsyncClient, Client

from .constants import *
from .login import login
from .util import get_headers, find_key, build_params, get_transaction_id, get_url_path

reset = '\x1b[0m'
colors = [f'\x1b[{i}m' for i in range(31, 37)]

try:
    if get_ipython().__class__.__name__ == 'ZMQInteractiveShell':
        import nest_asyncio

        nest_asyncio.apply()
except:
    ...

if platform.system() != 'Windows':
    try:
        import uvloop

        uvloop.install()
    except ImportError as e:
        ...


class NotCursorException(Exception):
    pass


class UnauthorizedError(Exception):
    pass


class NotFoundError(Exception):
    pass


class Search:
    def __init__(self, email: str = None, username: str = None, password: str = None, session: Client = None, **kwargs):
        self.save = kwargs.get('save', True)
        self.debug = kwargs.get('debug', 0)
        # self.logger = self._init_logger(**kwargs)
        self.logger = kwargs.get('logger') or self._init_logger(**kwargs)
        self.cookies: dict = kwargs.get('cookies')
        if not self.cookies:
            raise ValueError('Cookies not specified')
        self.session = self._validate_session(email, username, password, session, **kwargs)
        self.proxy = kwargs.get('proxy')
        self.out = Path(kwargs.get('out', 'data'))
        self.ct = get_transaction_id()


    def run(self, queries: list[dict], limit: int = math.inf, out: str = 'data/search_results', **kwargs):
        out = self.out / "search_results"
        out.mkdir(parents=True, exist_ok=True)
        return asyncio.run(self.process(queries, limit, out, **kwargs))

    async def process(self, queries: list[dict], limit: int, out: Path, **kwargs) -> tuple[Any]:
        async with AsyncClient(headers=get_headers(self.session), proxy=self.proxy) as s:
            return await asyncio.gather(*(self.paginate(s, q, limit, out, **kwargs) for q in queries))

    async def paginate(self, client: AsyncClient, query: dict, limit: int, out: Path, **kwargs) -> list[dict]:
        timeout = 10
        params = {
            'variables': {
                'count': 20,
                'querySource': 'recent_search_click',
                'rawQuery': query['query'],
                'product': query['category']
            },
            'features': Operation.default_features_for_search,
            # 'fieldToggles': {'withArticleRichContentState': False},
        }

        res = []
        cursor = ''
        total = set()
        while True:
            if cursor:
                params['variables']['cursor'] = cursor
            data, entries, cursor = await self.backoff(
                lambda: self.get(client, params),
                **kwargs
            )
            # data, entries, cursor = await self.get(client, params)
            res.extend(entries)
            total |= set(find_key(entries, 'entryId'))
            self.logger.debug(f"tweets {len(total)}")
            if len(entries) <= 2 or len(total) >= limit:  # just cursors
                if self.debug:
                    self.logger.debug(f'[{GREEN}success{RESET}] Returned {len(total)} search results for {query["query"]}')
                return res

            if not cursor:
                raise NotCursorException("Cursor not found")

            if self.debug:
                self.logger.debug(f'{query["query"]}')
            if self.save:
                (out / f'{time.time_ns()}.json').write_bytes(orjson.dumps(entries))
            self.logger.debug(f"sleep {timeout} seconds")
            time.sleep(timeout)

    async def get(self, client: AsyncClient, params: dict) -> tuple:
        _, qid, name = Operation.SearchTimeline
        url = f'https://x.com/i/api/graphql/{qid}/{name}'
        path = "/i/api/graphql/yiE17ccAAu3qwM34bPYZkQ/SearchTimeline"
        transaction_id = self.ct.generate_transaction_id(method='GET', path=path)
        r = await client.get(
            url,
            params=build_params(params),
            headers=get_search_header(
                self.cookies['ct0'],
                self.cookies['auth_token'],
                referer={
                    "q": params['variables']['rawQuery'],
                    "src": "typed_query",
                },
                x_client_transaction_id=transaction_id
            )
        )
        self.logger.debug(f"{r.status_code=} {r.request.url=}")
        if r.status_code == 401 or r.status_code == 403:
            raise UnauthorizedError("Access denied")

        if r.status_code == 404:
            raise NotFoundError(f"Not found")

        data = r.json()
        cursor = self.get_cursor(data)
        entries = [y for x in find_key(data, 'entries') for y in x if re.search(r'^(tweet|user)-', y['entryId'])]
        # add on query info
        for e in entries:
            e['query'] = params['variables']['rawQuery']
        return data, entries, cursor

    def get_cursor(self, data: list[dict]):
        for e in find_key(data, 'content'):
            if e.get('cursorType') == 'Bottom':
                return e['value']

    async def backoff(self, fn, **kwargs):
        retries = kwargs.get('retries', 3)
        for i in range(retries + 1):
            try:
                data, entries, cursor = await fn()
                if errors := data.get('errors'):
                    for e in errors:
                        if self.debug:
                            self.logger.warning(f'{YELLOW}{e.get("message")}{RESET}')
                        return [], [], ''
                ids = set(find_key(data, 'entryId'))
                if len(ids) >= 2:
                    return data, entries, cursor
            except UnauthorizedError as err:
                raise err
            except Exception as e:
                if i == retries:
                    if self.debug:
                        self.logger.debug(f'Max retries exceeded\n{e}')
                    raise e
                t = 2 ** i + random.random()
                if self.debug:
                    self.logger.debug(f'Retrying in {f"{t:.2f}"} seconds\t\t{e}')
                await asyncio.sleep(t)

    def _init_logger(self, **kwargs) -> Logger:
        # if kwargs.get('debug'):
        cfg = kwargs.get('log_config')
        logging.config.dictConfig(cfg or LOG_CONFIG)

        # only support one logger
        logger_name = list(LOG_CONFIG['loggers'].keys())[0]

        # set level of all other loggers to ERROR
        for name in logging.root.manager.loggerDict:
            if name != logger_name:
                logging.getLogger(name).setLevel(logging.ERROR)

        return logging.getLogger(logger_name)

    @staticmethod
    def _validate_session(*args, **kwargs):
        email, username, password, session = args

        # validate credentials
        if all((email, username, password)):
            return login(email, username, password, **kwargs)

        # invalid credentials, try validating session
        if session and all(session.cookies.get(c) for c in {'ct0', 'auth_token'}):
            return session

        # invalid credentials and session
        cookies = kwargs.get('cookies')

        # try validating cookies dict
        if isinstance(cookies, dict) and all(cookies.get(c) for c in {'ct0', 'auth_token'}):
            _session = Client(cookies=cookies, follow_redirects=True)
            _session.headers.update(get_headers(_session))
            return _session

        # try validating cookies from file
        if isinstance(cookies, str):
            _session = Client(cookies=orjson.loads(Path(cookies).read_bytes()), follow_redirects=True)
            _session.headers.update(get_headers(_session))
            return _session

        raise Exception('Session not authenticated. '
                        'Please use an authenticated session or remove the `session` argument and try again.')

    @property
    def id(self) -> int:
        """ Get User ID """
        return int(re.findall('"u=(\d+)"', self.session.cookies.get('twid'))[0])

    def save_cookies(self, fname: str = None):
        """ Save cookies to file """
        cookies = self.session.cookies
        Path(f'{fname or cookies.get("username")}.cookies').write_bytes(orjson.dumps(dict(cookies)))
