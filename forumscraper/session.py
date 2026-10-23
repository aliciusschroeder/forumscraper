# License: GNU GPLv3

import asyncio
import atexit
import base64
import contextlib
import json
import random
import re
import threading
import time
from urllib.parse import urldefrag

import zendriver
from zendriver import cdp
from zendriver.core.connection import ProtocolException
from zendriver.core.proxy import ProxyForwarder, UpstreamProxy

from .defs import reliq
from .utils import smarttrim
from .exceptions import RequestError, AlreadyVisitedError

transient_errors = [408, 429, 500, 502, 503, 504]

DOCUMENT = cdp.network.ResourceType.DOCUMENT


class Response:
    def __init__(self, url, status_code, headers, text, cookies):
        self.url = url
        self.status_code = status_code
        self.headers = headers
        self.text = text
        self.cookies = cookies

    def json(self):
        return json.loads(self.text)


class Tab:
    def __init__(self, tab):
        self.tab = tab
        self.lock = asyncio.Lock()  # one page at a time
        self.verify = True


class Load:
    """
    Collects documents loaded in the main frame of a tab. Handlers are coroutines
    so that zendriver runs them in order, in the thread of the event loop.
    """

    def __init__(self, frame):
        self.frame = frame
        self.redirects = 0
        self.loaders = {}
        self.responses = {}
        self.documents = asyncio.Queue()

    async def request(self, event):
        if event.frame_id != self.frame or event.type_ != DOCUMENT:
            return
        self.loaders[event.request_id] = event.loader_id
        if event.redirect_response is not None:
            self.redirects += 1

    async def response(self, event):
        if event.request_id in self.loaders:
            self.responses[event.request_id] = event.response

    async def finished(self, event):
        if event.request_id in self.loaders:
            self.documents.put_nowait((event.request_id, None))

    async def failed(self, event):
        if event.request_id in self.loaders:
            self.documents.put_nowait((event.request_id, event.error_text))


def header(headers, name):
    for key, value in headers.items():
        if key.lower() == name:
            return value


def decode_body(body, base64_encoded, headers):
    if not base64_encoded:
        return body

    data = base64.b64decode(body)
    charset = re.search(
        r"charset=[\"']?([^\"';\s]+)", header(headers, "content-type") or ""
    )
    try:
        return data.decode(charset[1] if charset else "utf-8", errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def get_proxy(proxies):
    proxy = set(i for i in proxies.values() if i)
    if len(proxy) > 1:
        raise ValueError("browser can use only one proxy for all requests")
    if len(proxy) == 0:
        return
    return proxy.pop()


def browser_cookies(browser):
    if isinstance(browser, str):
        import browser_cookie3

        browser = getattr(browser_cookie3, browser)

    return [
        cdp.network.CookieParam(
            name=i.name,
            value=i.value,
            domain=i.domain,
            path=i.path,
            secure=bool(i.secure),
            expires=cdp.network.TimeSinceEpoch(i.expires) if i.expires else None,
        )
        for i in browser()
    ]


async def solve_challenge(page):
    # chrome passes most challenges by itself, interactive ones need their checkbox
    # clicked. it can be found before it has a size, so clicks are tried again
    while True:
        await asyncio.sleep(1)
        with contextlib.suppress(Exception):
            await page.verify_cf()


class Session:
    """
    Makes all requests by loading pages in tabs of a zendriver browser,
    which runs in its own thread and is started by the first request.
    """

    def __init__(self, **settings):
        self._settings = {
            "timeout": 30,
            "verify": True,
            "allow_redirects": False,
            "max_redirects": 30,
            "retry": 2,
            "retry_delay": 5,
            "retry_all_errors": False,
            "wait": 0,
            "wait_random": 0,
            "trim": False,
            "visited": False,
            "logger": None,
            # used when browser is started
            "proxies": {},
            "user_agent": None,
            "browser": None,
            "headless": True,
            "browser_executable_path": None,
            "browser_args": [],
            "user_data_dir": None,
            "sandbox": True,
            "new_window": True,
            "rotate_focus": None,
        }
        self.headers = {}
        self.cookies = {}
        self.visited = set()

        self._lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._local = threading.local()
        self._loop = None
        self._thread = None
        self._browser = None
        self._forwarder = None
        self._rotator = None
        self._main = None
        self._tabs = []

        self.set_settings(settings)

    def __getitem__(self, key):
        return self._settings[key]

    def __setitem__(self, key, value):
        self.set_settings({key: value})

    def set_settings(self, settings):
        for key, value in settings.items():
            if key == "headers":
                self.headers.update(value)
            elif key == "cookies":
                self.cookies.update(value)
            elif key in self._settings:
                self._settings[key] = value
            else:
                raise TypeError('unknown request setting "{}"'.format(key))

    def get_settings(self, settings):
        ret = dict(self._settings)
        ret["headers"] = dict(self.headers)
        ret["cookies"] = dict(self.cookies)

        for key, value in settings.items():
            if key not in ret:
                raise TypeError('unknown request setting "{}"'.format(key))
            if key == "headers" or key == "cookies":
                ret[key].update(value)
            else:
                ret[key] = value
        return ret

    def _run(self, coro):
        with self._lock:
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
                self._thread = threading.Thread(
                    target=self._loop.run_forever, daemon=True
                )
                self._thread.start()
            loop = self._loop

        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result()
        except BaseException:
            future.cancel()
            raise

    async def _start(self, headless, rotate_focus):
        settings = self._settings
        args = list(settings["browser_args"])

        # set for the whole browser, since tabs of browser contexts with their
        # own proxy don't use the profile, e.g. its logins
        proxy = get_proxy(settings["proxies"])
        if proxy is not None:
            upstream = UpstreamProxy.from_url(proxy)
            if upstream is not None:
                # chrome can't pass proxy credentials by itself
                self._forwarder = ProxyForwarder(upstream)
                await self._forwarder.start()
                proxy = self._forwarder.proxy_server
            args.append("--proxy-server=" + proxy)

        try:
            browser = await zendriver.start(
                headless=headless,
                browser_executable_path=settings["browser_executable_path"],
                browser_args=args,
                user_data_dir=settings["user_data_dir"],
                sandbox=settings["sandbox"],
                user_agent=settings["user_agent"],
                # starting with a new profile or a window can take longer than
                # the default of 10 tries every 0.25s
                browser_connection_max_tries=60,
            )
            try:
                page = browser.main_tab
                await browser.connection.send(cdp.browser.set_download_behavior("deny"))
                if settings["browser"] is not None:
                    await page.send(
                        cdp.network.set_cookies(browser_cookies(settings["browser"]))
                    )
            except BaseException:
                await browser.stop()
                raise
        except BaseException:
            if self._forwarder is not None:
                await self._forwarder.close()
                self._forwarder = None
            raise

        self._browser = browser
        main = Tab(page)
        self._tabs = [main]
        if rotate_focus:
            self._rotator = asyncio.ensure_future(self._rotate_focus(rotate_focus))
        return main

    async def _stop(self):
        if self._rotator is not None:
            self._rotator.cancel()
            self._rotator = None
        with contextlib.suppress(Exception):
            await self._browser.stop()
        if self._forwarder is not None:
            await self._forwarder.close()
            self._forwarder = None
        self._browser = None
        self._tabs = []

    async def _rotate_focus(self, interval):
        i = 0
        while True:
            await asyncio.sleep(interval)
            if len(self._tabs) < 2:
                continue
            i = (i + 1) % len(self._tabs)
            # tabs are closed meanwhile
            with contextlib.suppress(Exception):
                await self._tabs[i].tab.activate()

    def _main_tab(self):
        with self._start_lock:
            if self._main is None:
                self._main = self._run(
                    self._start(self["headless"], self["rotate_focus"])
                )
                atexit.register(self.close)
            return self._main

    async def _new_tab(self):
        browser = self._browser
        try:
            # by default in its own window, since only the front tab of a window
            # is visible and pages in the others are throttled, e.g. never pass
            # challenges
            target_id = await browser.connection.send(
                cdp.target.create_target("about:blank", new_window=self["new_window"])
            )
            for _ in range(2):
                for i in browser.tabs:
                    if i.target_id == target_id:
                        tab = Tab(i)
                        self._tabs.append(tab)
                        return tab
                await browser.update_targets()
        except ProtocolException as e:
            raise RequestError("failed with {}".format(e.message.split("\n")[0]))
        raise RequestError("failed with missing tab")

    async def _close_tab(self, tab):
        self._tabs.remove(tab)
        await tab.tab.close()

    @contextlib.contextmanager
    def tab(self):
        """
        Requests of the calling thread made inside of the block go through
        a new tab, that is opened by the first of them and closed at the end.
        """
        tabs = getattr(self._local, "tabs", None)
        if tabs is None:
            tabs = self._local.tabs = []

        tabs.append(None)
        try:
            yield
        finally:
            tab = tabs.pop()
            if tab is not None:
                # a tab that can't be closed belongs to a browser that's already gone
                with contextlib.suppress(Exception):
                    self._run(self._close_tab(tab))

    def _current_tab(self):
        main = self._main_tab()
        tabs = getattr(self._local, "tabs", None)
        if not tabs:
            return main
        if tabs[-1] is None:
            tabs[-1] = self._run(self._new_tab())
        return tabs[-1]

    async def _load(self, tab, url, settings):
        async with tab.lock:
            try:
                return await asyncio.wait_for(
                    self._navigate(tab, url, settings), settings["timeout"]
                )
            except asyncio.TimeoutError:
                raise RequestError("failed with timeout")
            except ProtocolException as e:
                raise RequestError("failed with {}".format(e.message.split("\n")[0]))

    async def _navigate(self, tab, url, settings):
        page = tab.tab

        if tab.verify != settings["verify"]:
            await page.send(
                cdp.security.set_ignore_certificate_errors(not settings["verify"])
            )
            tab.verify = settings["verify"]

        if len(settings["cookies"]) > 0:
            await page.send(
                cdp.network.set_cookies(
                    [
                        cdp.network.CookieParam(
                            name=name, value=value, url=url, path="/"
                        )
                        for name, value in settings["cookies"].items()
                    ]
                )
            )

        load = Load(page.target_id)
        handlers = [
            (cdp.network.RequestWillBeSent, load.request),
            (cdp.network.ResponseReceived, load.response),
            (cdp.network.LoadingFinished, load.finished),
            (cdp.network.LoadingFailed, load.failed),
        ]

        headers = {i.lower(): j for i, j in settings["headers"].items()}
        if len(headers) > 0:
            # headers are added only to requests of documents loaded in the main frame,
            # requests following redirects get them only if redirects are allowed

            async def add_headers(event):
                h = None
                if event.frame_id == load.frame and (
                    event.redirected_request_id is None or settings["allow_redirects"]
                ):
                    h = {i.lower(): j for i, j in event.request.headers.items()}
                    h.update(headers)
                    h = [cdp.fetch.HeaderEntry(name=i, value=j) for i, j in h.items()]

                with contextlib.suppress(ProtocolException):
                    await page.send(
                        cdp.fetch.continue_request(event.request_id, headers=h)
                    )

            await page.send(
                cdp.fetch.enable([cdp.fetch.RequestPattern(resource_type=DOCUMENT)])
            )
            handlers.append((cdp.fetch.RequestPaused, add_headers))

        for event, handler in handlers:
            page.add_handler(event, handler)

        solver = None
        try:
            _, loader, error_text, *_ = await page.send(
                cdp.page.navigate(urldefrag(url)[0])
            )
            if load.redirects > settings["max_redirects"]:
                raise RequestError("failed with too_many_redirects")
            if (
                error_text is not None
                and error_text != "net::ERR_HTTP_RESPONSE_CODE_FAILURE"
            ):
                raise RequestError("failed with {}".format(error_text))

            empty = set()
            challenged = False
            while True:
                request_id, error = await load.documents.get()
                if load.loaders[request_id] != loader and not challenged:
                    continue  # document of a navigation started before this one

                if error == "net::ERR_HTTP_RESPONSE_CODE_FAILURE":
                    empty.add(request_id)  # chrome loads its own error page instead
                    continue
                if error is not None:
                    raise RequestError("failed with {}".format(error))

                response = load.responses.get(request_id)
                if response is None:
                    raise RequestError("failed with missing response")

                text = ""
                if request_id not in empty:
                    body, base64_encoded = await page.send(
                        cdp.network.get_response_body(request_id)
                    )
                    text = decode_body(body, base64_encoded, response.headers)

                # pages of challenges are loaded again once they're passed
                if header(response.headers, "cf-mitigated") == "challenge":
                    challenged = True
                    if solver is None:
                        solver = asyncio.ensure_future(solve_challenge(page))
                    continue
                if 'id="anubis_challenge"' in text:
                    challenged = True  # its proof of work is done by the page
                    continue
                break

            if load.redirects > settings["max_redirects"]:
                raise RequestError("failed with too_many_redirects")

            cookies = await page.send(cdp.network.get_cookies([response.url]))

            return Response(
                response.url,
                response.status,
                dict(response.headers),
                text,
                {i.name: i.value for i in cookies},
            )
        finally:
            if solver is not None:
                solver.cancel()
            for event, handler in handlers:
                page.remove_handlers(event, handler)
            if len(headers) > 0:
                with contextlib.suppress(Exception):
                    await page.send(cdp.fetch.disable())

    def request(self, url, **settings):
        settings = self.get_settings(settings)

        if settings["visited"]:
            with self._lock:
                if url in self.visited:
                    raise AlreadyVisitedError(url)
                self.visited.add(url)

        if settings["wait"] != 0:
            time.sleep(settings["wait"])
        if settings["wait_random"] != 0:
            time.sleep(random.random() * settings["wait_random"])

        tab = self._current_tab()
        tries = settings["retry"]
        i = 0
        while True:
            if settings["logger"] is not None:
                settings["logger"](url)

            try:
                resp = self._run(self._load(tab, url, settings))
            except RequestError:
                if i >= tries:
                    raise
            else:
                code = resp.status_code
                if code < 400:
                    return resp
                if i >= tries or (
                    not settings["retry_all_errors"] and code not in transient_errors
                ):
                    raise RequestError("failed with {}".format(code))

            i += 1
            if settings["retry_delay"] != 0:
                time.sleep(settings["retry_delay"])

    def get_html(self, url, response=False, **settings):
        resp = self.request(url, **settings)

        text = resp.text
        trim = settings.get("trim")
        if (trim is None and self["trim"]) or trim is True:
            text = smarttrim(text)

        r = reliq(text, ref=resp.url)
        return (r, resp) if response else r

    def get_json(self, url, response=False, **settings):
        resp = self.request(url, **settings)

        r = resp.json()
        return (r, resp) if response else r

    async def _wait_closed(self, urls):
        browser = self._browser
        # without waiting for pages, which may never load
        for i, url in enumerate(urls):
            if i == 0:
                await self._main.tab.send(cdp.page.navigate(url))
            else:
                await browser.connection.send(cdp.target.create_target(url))

        while True:
            await asyncio.sleep(1)
            try:
                targets = await browser.connection.send(cdp.target.get_targets())
            except Exception:
                return  # browser exited
            if not any(i.type_ == "page" for i in targets):
                return

    def open_browser(self, urls=()):
        """
        Opens the browser with its window and urls, set up as for requests,
        e.g. to log in with user_data_dir, and waits until it's closed.
        """
        with self._start_lock:
            if self._main is not None:
                raise RuntimeError("browser is already started")
            self._main = self._run(self._start(False, None))
        try:
            self._run(self._wait_closed(urls))
        finally:
            self.close()

    def close(self):
        """Stops the browser, next request starts a new one"""
        with self._start_lock:
            browser = self._browser
            loop = self._loop
            self._main = None
            atexit.unregister(self.close)

            if browser is not None:
                self._run(self._stop())

            with self._lock:
                self._loop = None
            if loop is not None:
                loop.call_soon_threadsafe(loop.stop)
                self._thread.join()
                loop.close()
