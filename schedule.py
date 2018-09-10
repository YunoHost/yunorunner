import asyncio
from functools import wraps


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
                    import traceback
                    traceback.print_exc()
                    print(f"Error: exception in function '{function.__name__}', relaunch in {sleep} seconds")
                finally:
                    await asyncio.sleep(sleep)
        return wrap
    return decorator
