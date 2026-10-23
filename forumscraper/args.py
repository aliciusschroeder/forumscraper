import os
import sys
import argparse
import ast
import gzip
import bz2
import lzma
import inspect

from .utils import (
    url_valid,
    conv_curl_header_to_requests,
    conv_curl_cookie_to_requests,
)
from .defs import Outputs, __version__
from .extractors.extractor import *


def valid_directory(directory):
    try:
        return os.chdir(directory)
    except Exception:
        raise argparse.ArgumentTypeError(
            'couldn\'t change directory to "{}"'.format(directory)
        )


def valid_names(name):
    if name == "id":
        return Outputs.write_by_id
    elif name == "hash":
        return Outputs.write_by_hash
    else:
        raise KeyError(f'"{name}" is neither id nor hash')


def valid_type(type_name):
    if url_valid(type_name) is not None:
        return type_name

    ret = [Extractor, "guess"]

    forums = {
        "all": Extractor,
        "smf": smf,
        "smf1": smf1,
        "smf2": smf2,
        "phpbb": phpbb,
        "xenforo": xenforo,
        "xenforo1": xenforo1,
        "xenforo2": xenforo2,
        "invision": invision,
        "xmb": xmb,
        "hackernews": hackernews,
        "stackexchange": stackexchange,
        "vbulletin": vbulletin,
    }
    forums_identifiers = ["all", "smf", "xenforo"]
    funcs = ["guess", "identify", "findroot", "thread", "user", "forum", "tag", "board"]

    names = type_name.split(".")
    namesl = len(names)

    if (namesl == 1 and len(names[0]) == 0) or namesl > 2:
        raise KeyError(f"{type_name} is not a valid scraper type")

    if namesl == 2 and len(names[0]) == 0 and len(names[1]) == 0:
        return ret

    if len(names[0]) > 0:
        r = forums.get(names[0])
        if not r:
            raise KeyError(f'"{names[0]}" forum scraper does not exists')
        ret[0] = r

    if namesl == 2 and len(names[1]) > 0:
        if names[1] in funcs:
            if (
                names[1] == "identify"
                and len(names[0])
                and names[0] not in forums_identifiers
            ):
                raise (f'{names[0]} does not have function "identify"')
            if names[1] not in ["guess", "identify", "findroot"]:
                names[1] = "get_" + names[1]
            ret[1] = names[1]
        else:
            raise KeyError(f'"{names[1]}" type does not exists')
    return ret


def valid_compression_type(type_name):
    types = {
        "lzma": lzma.compress,
        "bzip2": bz2.compress,
        "gzip": gzip.compress,
        "none": None,
    }

    if type_name not in types.keys():
        raise KeyError(f"{type_name} is not a valid compression type")
    return types.get(type_name)


def valid_header(src):
    r = conv_curl_header_to_requests(src)
    if r is None:
        raise argparse.ArgumentTypeError('Invalid header "{}"'.format(src))
    return r


def valid_cookie(src):
    r = conv_curl_cookie_to_requests(src)
    if r is None:
        raise argparse.ArgumentTypeError('Invalid cookie "{}"'.format(src))
    return r


def valid_time(time):
    weights = {"s": 1, "m": 60, "h": 3600, "d": 24 * 3600}

    t = time.strip()
    weight = 1
    if t[-1:] in weights:
        weight = weights[t[-1]]
        t = t[:-1]

    try:
        num = float(t)
    except ValueError:
        num = -1
    if num < 0:
        raise argparse.ArgumentTypeError('incorrect time format "{}"'.format(time))
    return num * weight


def valid_timeout(time):
    if time.strip() == "-":
        return None
    return valid_time(time)


def valid_browser(name):
    try:
        import browser_cookie3
    except ImportError:
        raise argparse.ArgumentTypeError(
            "getting cookies from browser requires browser_cookie3 module"
        )

    func = getattr(browser_cookie3, name, None)
    if not inspect.isfunction(func) or func.__module__ != browser_cookie3.__name__:
        raise argparse.ArgumentTypeError('no such browser "{}"'.format(name))
    return func


def request_settings(args):
    names = [
        "wait",
        "wait_random",
        "retry",
        "retry_delay",
        "retry_all_errors",
        "timeout",
        "verify",
        "allow_redirects",
        "max_redirects",
        "user_agent",
        "browser",
        "browser_executable_path",
        "user_data_dir",
        "headless",
        "sandbox",
    ]
    ret = {i: getattr(args, i) for i in names if hasattr(args, i)}

    headers = {}
    cookies = {}
    for i in args.header:
        for name, value in i.items():
            if name.lower() != "cookie":
                headers[name.lower()] = value
                continue
            for j in value.split(";"):
                if (r := conv_curl_cookie_to_requests(j)) is not None:
                    cookies.update(r)
    for i in args.cookie:
        cookies.update(i)

    if len(headers) > 0:
        ret["headers"] = headers
    if len(cookies) > 0:
        ret["cookies"] = cookies
    if hasattr(args, "proxy"):
        ret["proxies"] = {"http": args.proxy, "https": args.proxy}
    return ret


def args_section(parser):
    request = parser.add_argument_group("Request settings")
    request.add_argument(
        "-w",
        "--wait",
        metavar="TIME",
        type=valid_time,
        help="Set waiting time for each request",
        default=argparse.SUPPRESS,
    )
    request.add_argument(
        "-W",
        "--wait-random",
        metavar="TIME",
        type=valid_time,
        help="Set random waiting time for each request to be from 0 to TIME",
        default=argparse.SUPPRESS,
    )
    request.add_argument(
        "-r",
        "--retry",
        metavar="NUM",
        type=int,
        help="Set number of retries for failed request to NUM",
        default=argparse.SUPPRESS,
    )
    request.add_argument(
        "--retry-delay",
        metavar="TIME",
        type=valid_time,
        help="Set interval between each retry",
        default=argparse.SUPPRESS,
    )
    request.add_argument(
        "--retry-all-errors",
        action="store_true",
        help="Retry no matter the error",
        default=argparse.SUPPRESS,
    )
    request.add_argument(
        "-m",
        "--timeout",
        metavar="TIME",
        type=valid_timeout,
        help="Set time limit for loading a page, if set to '-' timeout is disabled",
        default=argparse.SUPPRESS,
    )
    request.add_argument(
        "-k",
        "--insecure",
        dest="verify",
        action="store_false",
        help="Ignore ssl errors",
        default=argparse.SUPPRESS,
    )
    request.add_argument(
        "-L",
        "--location",
        dest="allow_redirects",
        action="store_true",
        help="Send headers also to pages the browser is redirected to (it always follows redirections), can be dangerous if credentials are passed in headers",
        default=argparse.SUPPRESS,
    )
    request.add_argument(
        "--max-redirs",
        dest="max_redirects",
        metavar="NUM",
        type=int,
        help="Set the maximum number of redirections to follow",
        default=argparse.SUPPRESS,
    )
    request.add_argument(
        "-A",
        "--user-agent",
        metavar="UA",
        type=str,
        help="Set custom user agent of the browser",
        default=argparse.SUPPRESS,
    )
    request.add_argument(
        "-x",
        "--proxy",
        metavar="PROXY",
        type=str,
        help="Use the specified proxy for all requests e.g. http://user:pass@host:port or socks5://host:port",
        default=argparse.SUPPRESS,
    )
    request.add_argument(
        "-H",
        "--header",
        metavar="HEADER",
        type=valid_header,
        action="append",
        help="Set curl style header, can be used multiple times e.g. -H 'User: Admin' -H 'Pass: 12345'",
        default=[],
    )
    request.add_argument(
        "-b",
        "--cookie",
        metavar="COOKIE",
        type=valid_cookie,
        action="append",
        help="Set curl style cookie, can be used multiple times e.g. -b 'auth=8f82ab' -b 'PHPSESSID=qw3r8an829'",
        default=[],
    )
    request.add_argument(
        "-B",
        "--browser",
        metavar="BROWSER",
        type=valid_browser,
        help="Get cookies from specified browser e.g. -B firefox",
        default=argparse.SUPPRESS,
    )

    browser = parser.add_argument_group("Browser")
    browser.add_argument(
        "--browser-path",
        dest="browser_executable_path",
        metavar="PATH",
        type=str,
        help="Use browser executable from PATH instead of the one found automatically",
        default=argparse.SUPPRESS,
    )
    browser.add_argument(
        "--user-data-dir",
        metavar="DIR",
        type=str,
        help="Use browser profile from DIR, e.g. to be logged in, instead of a new temporary one",
        default=argparse.SUPPRESS,
    )
    browser.add_argument(
        "--no-headless",
        dest="headless",
        action="store_false",
        help="Show browser window",
        default=argparse.SUPPRESS,
    )
    browser.add_argument(
        "--no-sandbox",
        dest="sandbox",
        action="store_false",
        help="Disable sandbox of the browser, may be needed in containers",
        default=argparse.SUPPRESS,
    )

    return request


def argparser():
    parser = argparse.ArgumentParser(
        description="Forum scraper that aims to be universal, automatic and extensive.",
        add_help=False,
    )

    parser.add_argument(
        "urls",
        metavar="URL|TYPE",
        type=valid_type,
        nargs="*",
        help="url pointing to source. Specifying type changes scraper for all next urls. Type consists of forum name which can be all, invision, phpbb, smf, smf1, smf2, xenforo, xenforo1, xenforo2, xmb, stackexchange, hackernews and it can by followed by a '.' and function name which can be guess, thread, forum, tag, board, user, identify, findroot. By default set to all.guess equivalent to .guess and .",
    )

    general = parser.add_argument_group("General")
    general.add_argument(
        "-h",
        "--help",
        action="help",
        help="Show this help message and exit",
    )
    general.add_argument(
        "-v",
        "--version",
        action="version",
        version=__version__,
        help="Print program version and exit",
    )
    general.add_argument(
        "-t",
        "--threads",
        metavar="NUM",
        type=int,
        help="download NUM threads at once, each in its own browser tab",
        default=1,
    )
    general.add_argument(
        "-p",
        "--pedantic",
        action="store_true",
        help="Exit if anything fails",
    )

    files = parser.add_argument_group("Files")
    files.add_argument(
        "-d",
        "--directory",
        metavar="DIR",
        type=valid_directory,
        help="Change directory to DIR",
    )
    files.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="forcefully overwrite files, may cause redownloading if found urls are not unique",
    )
    files.add_argument(
        "--names",
        metavar="NAME",
        type=valid_names,
        help="Change naming convention of created files to NAME, which can be either id or hash",
        default=Outputs.write_by_id,
    )
    files.add_argument(
        "--compression",
        metavar="ALGO",
        type=valid_compression_type,
        help="Compress created files using set algorithm, that can be none, gzip, bzip2, lzma",
        default=None,
    )
    files.add_argument(
        "-l",
        "--log",
        metavar="FILE",
        type=lambda x: open(x, "w"),
        help="log results to FILE (by default set to stdout)",
        default=sys.stdout,
    )
    files.add_argument(
        "-F",
        "--failed",
        metavar="FILE",
        type=lambda x: open(x, "w"),
        help="log failures to FILE (by default set to stderr)",
        default=sys.stderr,
    )
    files.add_argument(
        "-o",
        "--output",
        metavar="FILE",
        type=lambda x: open(x, "w"),
        help="store results to FILE (by default set to stdout), work only for identify and findroot functions and --only-urls options",
        default=sys.stdout,
    )

    settings = parser.add_argument_group("Settings")
    settings.add_argument(
        "--html",
        action="store_true",
        help="save html files along side json output with .html extension",
    )
    settings.add_argument(
        "--nothreads",
        action="store_true",
        help="do not download threads (works only when page passed is not a thread)",
    )
    settings.add_argument(
        "--users",
        action="store_true",
        help="download users",
    )
    settings.add_argument(
        "--reactions",
        action="store_true",
        help="download reactions",
    )
    settings.add_argument(
        "--boards",
        action="store_true",
        help="create boards files",
    )
    settings.add_argument(
        "--tags",
        action="store_true",
        help="create tags files",
    )
    settings.add_argument(
        "--forums",
        action="store_true",
        help="create forums files",
    )
    settings.add_argument(
        "--only-urls-forums",
        action="store_true",
        help="output urls to found forums to output file without scraping",
    )
    settings.add_argument(
        "--only-urls-threads",
        action="store_true",
        help="output urls to found threads to output file without scraping",
    )
    settings.add_argument(
        "--thread-pages-max",
        metavar="NUM",
        type=int,
        help="set max number of pages traversed in threads",
    )
    settings.add_argument(
        "--pages-max",
        metavar="NUM",
        type=int,
        help="set max number of pages traversed in pages amassing threads e.g. forums, tags, boards",
    )
    settings.add_argument(
        "--pages-max-depth",
        metavar="NUM",
        type=int,
        help="set max recursion depth",
    )
    settings.add_argument(
        "--pages-forums-max",
        metavar="NUM",
        type=int,
        help="set max number of forums to be processed in every page",
    )
    settings.add_argument(
        "--pages-threads-max",
        metavar="NUM",
        type=int,
        help="set max number of threads to be processed in every page",
    )

    args_section(parser)

    return parser
