"""Only ``gateway.py`` may spawn a process, open a network connection or read the clock."""

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
}  # fmt: skip
BANNED_OS_CALLS = {
    "system", "popen", "fork", "forkpty", "execl", "execle", "execlp", "execlpe", "execv",
    "execve", "execvp", "execvpe", "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv",
    "spawnve", "spawnvp", "spawnvpe", "posix_spawn", "posix_spawnp", "startfile",
}  # fmt: skip
BANNED_ASYNCIO = {"create_subprocess_exec", "create_subprocess_shell"}
BANNED_BUILTINS = {"__import__", "eval", "exec"}
BANNED_DYNAMIC_IMPORT = {"import_module"}
TEXT_IMPORT = re.compile(rf"^\s*(?:import|from)\s+(?:{'|'.join(sorted(BANNED_MODULES))})\b", re.M)


def _banned_module(name: str) -> bool:
    return any(name == m or name.startswith(m + ".") for m in BANNED_MODULES)


def find_violations(source: str, filename: str = "<memory>") -> list[str]:
    """Every way the source reaches a process, the network or a dynamic import."""
    found: list[str] = []
    tree = ast.parse(source, filename)
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
                if module == "os" and alias.name in BANNED_OS_CALLS:
                    found.append(f"from os import {alias.name}")
                if module == "asyncio" and alias.name in BANNED_ASYNCIO:
                    found.append(f"from asyncio import {alias.name}")
                if module == "importlib" and alias.name in BANNED_DYNAMIC_IMPORT:
                    found.append("from importlib import import_module")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                if func.value.id == "os" and func.attr in BANNED_OS_CALLS:
                    found.append(f"os.{func.attr}()")
                if func.value.id == "asyncio" and func.attr in BANNED_ASYNCIO:
                    found.append(f"asyncio.{func.attr}()")
                if func.value.id == "importlib" and func.attr in BANNED_DYNAMIC_IMPORT:
                    found.append("importlib.import_module()")
            if isinstance(func, ast.Name) and func.id in BANNED_BUILTINS | BANNED_DYNAMIC_IMPORT:
                found.append(f"{func.id}()")
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
