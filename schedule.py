import traceback
import asyncio

from functools import wraps
from datetime import datetime, timedelta


def always_relaunch(sleep):
    def decorator(function):
        @wraps(function)
        async def wrap(*args, **kwargs):
            while True:
                try:
                    await function(*args, **kwargs)
                except KeyboardInterrupt:
                    return
                except Exception as e:
                    traceback.print_exc()
                    print(f"Error: exception in function '{function.__name__}', relaunch in {sleep} seconds")
                finally:
                    await asyncio.sleep(sleep)
        return wrap
    return decorator


def once_per_day(function):
    async def decorator(*args, **kwargs):
        while True:
            try:
                await function(*args, **kwargs)
            except KeyboardInterrupt:
                return
            except Exception as e:
                import traceback
                traceback.print_exc()
                print(f"Error: exception in function '{function.__name__}', relaunch in tomorrow at one am")
            finally:
                # launch tomorrow at 1 am
                now = datetime.now()
                tomorrow = now + timedelta(days=1)
                tomorrow = tomorrow.replace(hour=1, minute=0, second=0)
                seconds_until_next_run = (tomorrow - now).seconds

                # XXX if relaunched twice the same day that will duplicate the jobs
                await asyncio.sleep(seconds_until_next_run)

    return decorator
