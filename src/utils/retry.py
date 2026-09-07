"""Décorateur de retry avec backoff exponentiel + jitter.

Dépendance zéro (pas de tenacity) pour garder l'image CI légère.

Usage :

    from src.utils.retry import retry

    @retry(exceptions=(requests.RequestException,))
    def fetch(): ...

Le nombre de tentatives et les délais reprennent par défaut les valeurs de
`config.settings` mais restent surchargeables par appel.
"""

from __future__ import annotations

import functools
import random
import time
from typing import Callable, ParamSpec, TypeVar

from config import settings

from .logging import get_logger

log = get_logger("retry")

P = ParamSpec("P")
T = TypeVar("T")


class RetryError(RuntimeError):
    """Levée quand toutes les tentatives ont échoué. `__cause__` = dernière erreur."""


def retry(
    *,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
    attempts: int | None = None,
    base_delay: float | None = None,
    max_delay: float | None = None,
    jitter: bool = True,
    on_giveup: Callable[[BaseException], None] | None = None,
) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """Retry avec backoff exponentiel (`base_delay * 2**n`) borné à `max_delay`."""

    _attempts = attempts if attempts is not None else settings.RETRY_ATTEMPTS
    _base = base_delay if base_delay is not None else settings.RETRY_BASE_DELAY
    _max = max_delay if max_delay is not None else settings.RETRY_MAX_DELAY

    def decorator(func: Callable[P, T]) -> Callable[P, T]:
        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            last: BaseException | None = None
            for attempt in range(1, _attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:  # type: ignore[misc]
                    last = exc
                    if attempt == _attempts:
                        break
                    delay = min(_base * (2 ** (attempt - 1)), _max)
                    if jitter:
                        delay += random.uniform(0, _base)
                    log.warning(
                        "%s a échoué (tentative %d/%d) : %s — retry dans %.1fs",
                        func.__name__,
                        attempt,
                        _attempts,
                        exc,
                        delay,
                    )
                    time.sleep(delay)
            assert last is not None
            if on_giveup is not None:
                on_giveup(last)
            raise RetryError(
                f"{func.__name__} a échoué après {_attempts} tentatives"
            ) from last

        return wrapper

    return decorator
