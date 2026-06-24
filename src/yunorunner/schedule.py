import asyncio
import datetime
import traceback
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any

AsyncRetNone = Callable[..., Awaitable[None]]


def always_relaunch(sleep: int | float) -> Callable[[AsyncRetNone], AsyncRetNone]:
    def decorator(function: AsyncRetNone) -> AsyncRetNone:
        @wraps(function)
        async def wrap(*args: Any, **kwargs: Any) -> None:
            while True:
                try:
                    await function(*args, **kwargs)
                except KeyboardInterrupt:
                    return
                except Exception:
                    traceback.print_exc()
                    # See https://docs.astral.sh/ty/reference/typing-faq/#why-does-ty-say-callable-has-no-attribute-__name__
                    name = getattr(function, "__name__", "unknown")
                    print(
                        f"Error: exception in function '{name}', "
                        f"relaunch in {sleep} seconds"
                    )
                finally:
                    await asyncio.sleep(sleep)

        return wrap

    return decorator


def once_per_day(function: AsyncRetNone) -> AsyncRetNone:
    async def decorator(*args: Any, **kwargs: Any) -> None:
        while True:
            try:
                await function(*args, **kwargs)
            except KeyboardInterrupt:
                return
            except Exception:
                traceback.print_exc()
                # See https://docs.astral.sh/ty/reference/typing-faq/#why-does-ty-say-callable-has-no-attribute-__name__
                name = getattr(function, "__name__", "unknown")
                print(
                    f"Error: exception in function '{name}', "
                    "relaunch in tomorrow at one am"
                )
            finally:
                # launch tomorrow at 1 am
                now = datetime.datetime.now(tz=datetime.UTC)
                tomorrow = now + datetime.timedelta(days=1)
                tomorrow = tomorrow.replace(hour=1, minute=0, second=0)
                seconds_until_next_run = (tomorrow - now).seconds

                # XXX if relaunched twice the same day that will duplicate the jobs
                await asyncio.sleep(seconds_until_next_run)

    return decorator
