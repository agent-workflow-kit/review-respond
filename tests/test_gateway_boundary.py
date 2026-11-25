"""Only ``gateway.py`` may spawn a process or open a network connection.

Time is injectable rather than banned: the gateway's ``Clock`` covers its process waits and
timeouts,
and duration parsing takes ``now=`` (its default reads the system clock).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "review_respond"

BANNED_MODULES = {
    "subprocess", "socket", "ssl", "http", "urllib.request", "urllib3", "requests", "httpx",
    "aiohttp", "websockets", "websocket", "ftplib", "smtplib", "telnetlib", "poplib", "imaplib",
    "xmlrpc", "asyncio.subprocess", "multiprocessing", "pty", "ctypes", "pexpect", "sh", "plumbum",
    "posix", "_posixsubprocess", "_socket", "socketserver", "selectors", "runpy", "builtins",
    "code", "codeop", "webbrowser", "concurrent.futures.process", "_winapi", "nt",
}  # fmt: skip
# Importing the package itself (not its harmless submodules such as urllib.parse) is refused too.
BANNED_EXACT_MODULES = {"urllib"}
BANNED_OS_CALLS = {
    "system", "popen", "fork", "forkpty", "execl", "execle", "execlp", "execlpe", "execv",
    "execve", "execvp", "execvpe", "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv",
    "spawnve", "spawnvp", "spawnvpe", "posix_spawn", "posix_spawnp", "startfile",
}  # fmt: skip
BANNED_ASYNCIO = {"create_subprocess_exec", "create_subprocess_shell"}
# Methods of an asyncio event loop (or anything else) that start a process.
BANNED_METHODS = {"subprocess_exec", "subprocess_shell"} | BANNED_ASYNCIO
BANNED_BUILTINS = {"__import__", "eval", "exec"}
BANNED_DYNAMIC_IMPORT = {"import_module"}
BANNED_NAMES = {"ProcessPoolExecutor"}
# A star import from these would hide a banned name from the by-name checks.
STAR_UNSAFE = {"os", "asyncio", "importlib", "sys"}
TEXT_IMPORT = re.compile(rf"^\s*(?:import|from)\s+(?:{'|'.join(sorted(BANNED_MODULES))})\b", re.M)


def _banned_module(name: str) -> bool:
    return name in BANNED_EXACT_MODULES or any(
        name == m or name.startswith(m + ".") for m in BANNED_MODULES
    )


def _aliases(tree: ast.AST) -> dict[str, str]:
    """Names bound to modules by ``import x``, ``import x.y as z`` (so ``o`` can mean ``os``)."""
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    names[alias.asname] = alias.name
                else:
                    names[alias.name.split(".")[0]] = alias.name.split(".")[0]
    return names


def _literal(node: ast.expr) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def find_violations(source: str, filename: str = "<memory>") -> list[str]:
    """Every way the source reaches a process, the network or a dynamic import."""
    found: list[str] = []
    tree = ast.parse(source, filename)
    aliases = _aliases(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [f"import {a.name}" for a in node.names if _banned_module(a.name)]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if _banned_module(module):
                found.append(f"from {module} import ...")
            for alias in node.names:
                full = f"{module}.{alias.name}"
                if _banned_module(full):
                    found.append(f"from {module} import {alias.name}")
                if alias.name == "*" and (module in STAR_UNSAFE or _banned_module(module)):
                    found.append(f"from {module} import *")
                if module == "os" and alias.name in BANNED_OS_CALLS:
                    found.append(f"from os import {alias.name}")
                if module == "asyncio" and alias.name in BANNED_ASYNCIO:
                    found.append(f"from asyncio import {alias.name}")
                if module == "importlib" and alias.name in BANNED_DYNAMIC_IMPORT:
                    found.append("from importlib import import_module")
                if module == "sys" and alias.name == "modules":
                    found.append("from sys import modules")
                if alias.name in BANNED_NAMES:
                    found.append(f"from {module} import {alias.name}")
        elif isinstance(node, ast.Attribute):
            base = node.value.id if isinstance(node.value, ast.Name) else None
            module = aliases.get(base, base) if base else None
            if module == "sys" and node.attr == "modules":
                found.append("sys.modules")
            if node.attr in BANNED_NAMES:
                found.append(f".{node.attr}")
        elif isinstance(node, ast.Name) and node.id in BANNED_BUILTINS | BANNED_NAMES:
            found.append(node.id)
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                base = func.value.id if isinstance(func.value, ast.Name) else None
                module = aliases.get(base, base) if base else None
                if module == "os" and func.attr in BANNED_OS_CALLS:
                    found.append(f"os.{func.attr}()")
                if module == "asyncio" and func.attr in BANNED_ASYNCIO:
                    found.append(f"asyncio.{func.attr}()")
                if module == "importlib" and func.attr in BANNED_DYNAMIC_IMPORT:
                    found.append("importlib.import_module()")
                if func.attr in BANNED_METHODS:
                    found.append(f".{func.attr}()")
            if isinstance(func, ast.Name) and func.id in BANNED_BUILTINS | BANNED_DYNAMIC_IMPORT:
                found.append(f"{func.id}()")
            if isinstance(func, ast.Name) and func.id == "getattr":
                name = _literal(node.args[1]) if len(node.args) > 1 else None
                if name is None:
                    found.append("getattr() with a name that is not a literal")
                elif name in BANNED_OS_CALLS | BANNED_METHODS | BANNED_DYNAMIC_IMPORT:
                    found.append(f"getattr(..., {name!r})")
    found += [f"text: {m.group(0).strip()}" for m in TEXT_IMPORT.finditer(source)]
    return found


def modules_other_than_gateway() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if p.name != "gateway.py" or p.parent != SRC)


def test_no_module_other_than_gateway_spawns_or_connects() -> None:
    offenders = {}
    for path in modules_other_than_gateway():
        found = find_violations(path.read_text(), str(path))
        if found:
            offenders[str(path.relative_to(SRC))] = found
    assert offenders == {}


def test_the_scan_covers_every_planned_module() -> None:
    names = {p.stem for p in modules_other_than_gateway()}
    assert {"cli", "config", "store", "hgit", "confirm", "hooks", "job", "cron"} <= names
    assert "gateway" not in names


def test_gateway_is_the_one_place_that_spawns() -> None:
    assert "import subprocess" in find_violations((SRC / "gateway.py").read_text())


@pytest.mark.parametrize(
    "source",
    [
        "import subprocess",
        "import subprocess as sp",
        "from subprocess import run",
        "from subprocess import Popen as P",
        "import socket",
        "from socket import create_connection",
        "import urllib.request",
        "from urllib import request\nfrom urllib.request import urlopen",
        "import http.client",
        "from http import client",
        "import requests, json",
        "import httpx",
        "import os\nos.system('x')",
        "import os\nos.popen('x')",
        "import os\nos.execv('/bin/sh', [])",
        "import os\nos.posix_spawn('/bin/sh', [], {})",
        "import os\nos.fork()",
        "from os import system",
        "from os import execvp",
        "import asyncio\nasyncio.create_subprocess_exec('x')",
        "from asyncio import create_subprocess_shell",
        "import asyncio.subprocess",
        "import multiprocessing",
        "import pty",
        "import ctypes",
        "__import__('subprocess')",
        "import importlib\nimportlib.import_module('subprocess')",
        "from importlib import import_module",
        "eval('1')",
        "exec('x = 1')",
        "x = 1\nimport  subprocess",
        "import os as o\no.system('x')",
        "import os as o\no.execv('/bin/sh', [])",
        "import asyncio as a\na.create_subprocess_exec('x')",
        "import importlib as i\ni.import_module('subprocess')",
        "from os import *",
        "from asyncio import *",
        "from subprocess import *",
        "import os\ngetattr(os, name)",
        "import os\ngetattr(os, 'sys' + 'tem')('x')",
        "import os\ngetattr(os, 'system')('x')",
        "x = getattr(m, name)",
        "import posix",
        "import posix as p\np.system('x')",
        "import _posixsubprocess",
        "import _socket",
        "import socketserver",
        "import selectors",
        "import runpy",
        "import builtins",
        "import builtins as b\nb.__import__('subprocess')",
        "from builtins import __import__",
        "import code",
        "import webbrowser",
        "import urllib",
        "from urllib import request",
        "import asyncio\nasyncio.get_event_loop().subprocess_exec(f, 'x')",
        "loop.subprocess_shell(f, 'x')",
        "import concurrent.futures.process",
        "from concurrent.futures import ProcessPoolExecutor",
        "import concurrent.futures as cf\ncf.ProcessPoolExecutor()",
        "import sys\nsys.modules['subprocess']",
        "import sys as s\ns.modules",
        "from sys import modules",
        "f = __import__\nf('subprocess')",
    ],
)
def test_the_scanner_catches_each_way_in(source: str) -> None:
    assert find_violations(source)


@pytest.mark.parametrize(
    "source",
    [
        "import json\nimport os\nos.path.join('a', 'b')\nos.getpid()",
        "import urllib.parse\nurllib.parse.quote('x')",
        "from review_respond.gateway import Gateway",
        "text = 'subprocess is only a word here'",
        "import shutil\nshutil.which('x')",
    ],
)
def test_the_scanner_leaves_harmless_code_alone(source: str) -> None:
    assert find_violations(source) == []
