# -*- coding: utf-8 -*-
from packaging.version import Version
from typing import List, Iterator, TypeVar, Optional, Iterable


_N = TypeVar("_N", float, int)


def natural_size(num: float, unit: str = "B", sep: bool = True) -> str:
    """
    Convert number to a human readable string with decimal prefix.

    :param float num: Value in given unit.
    :param unit: Unit suffix.
    :param sep: Whether to separate unit and value with a space.
    :returns: Human readable string with decimal prefixes.
    """
    sep_char = " " if sep else ""

    for prefix in ("", "K", "M", "G"):
        if abs(num) < 1000.0:
            return f"{num:3.1f}{sep_char}{prefix}{unit}"
        num /= 1000.0

    prefix = "T"
    return f"{num:.1f}{sep_char}{prefix}{unit}"


def chunks(lst: List, n: int) -> Iterator[List]:
    """
    Partitions an iterable into chunks of length ``n``.

    :param lst: Iterable to partition.
    :param n: Chunk size.
    :returns: Iterator over chunks.
    """
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


def clamp(n: _N, minn: _N, maxn: _N) -> _N:
    """
    Clamps a number between a minimum and maximum value.

    :param n: Original value.
    :param minn: Minimum allowed value.
    :param maxn: Maximum allowed value.
    :returns: Clamped value.
    """

    if n > maxn:
        return maxn
    elif n < minn:
        return minn
    else:
        return n


def get_newer_version(version: str, releases: Iterable[str]) -> Optional[str]:
    """
    Checks a given release version against a version list of releases to see if an
    update is available. Only offers newer versions if they are not a prerelease.

    :param version: The current version.
    :param releases: A list of valid cleaned releases.
    :returns: The version string of the latest release if a newer release is available.
    """

    releases = [r for r in releases if not Version(r).is_prerelease]
    releases.sort(key=lambda x: Version(x))
    latest_release = releases[-1]

    return latest_release if Version(version) < Version(latest_release) else None
