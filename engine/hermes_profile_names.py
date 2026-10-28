"""Hermes profile-name compatibility shared by discovery and enrollment."""

import re


# Hermes hermes_cli.profiles reserves these names; default remains its root-home alias.
RESERVED_NAMES = frozenset({'hermes', 'default', 'test', 'tmp', 'root', 'sudo'})
_RESERVED_ERROR = re.compile(r"\bProfile name ['\"]([a-z0-9][a-z0-9_-]{0,63})['\"] is reserved\b", re.I)


def reserved(name):
    return name != 'default' and name in RESERVED_NAMES


def message(name):
    return f'Profile 标识“{name}”被 Hermes 保留，请使用其他标识创建或选择成员。'


def validate(name):
    if reserved(name):
        raise ValueError(message(name))


def failure_message(detail):
    match = _RESERVED_ERROR.search(detail)
    return message(match.group(1)) if match else None
