#!/usr/bin/env python3

import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError


class CustomModel(BaseModel):
    model_config = ConfigDict(
        validate_default=True,
        extra="forbid",
    )


class Server(CustomModel):
    base_url: str
    port: int = 4242


class Service(CustomModel):
    debug: bool = False
    package_check_path: Path
    storage_path: Path


class Scheduling(CustomModel):
    monitor_apps_list: bool = True
    monitor_git: bool = True
    monthly_jobs: bool = True
    monitor_only_good_quality_apps: bool = False
    answer_to_auto_updater: bool = True
    apps_list_url: str = "https://app.yunohost.org/default/v3/apps.json"


class Tests(CustomModel):
    arch: str
    dist: str
    ynh_branch: str


class Workers(CustomModel):
    number: int = 1
    timeout: int = 10800


class Webhooks(CustomModel):
    triggers: list[str] = [
        "!testme",
        "!gogogadgetoci",
        "By the power of systemd, I invoke The Great App CI to test this Pull Request!",
    ]

    catchphrases: list[str] = [
        "Alrighty!",
        "Fingers crossed!",
        "May the CI gods be with you!",
        ":carousel_horse:",
        ":rocket:",
        ":sunflower:",
        "Meow :cat2:",
        ":v:",
        ":stuck_out_tongue_winking_eye:",
    ]

    github_commit_status_token: str = ""
    github_webhook_secret: str = ""


class Config(CustomModel):
    server: Server
    service: Service
    scheduling: Scheduling
    tests: Tests
    workers: Workers
    webhooks: Webhooks

    def __init__(self, path: Path) -> None:
        try:
            config = tomllib.load(path.open("rb"))
            super().__init__(**config)

        except FileNotFoundError:
            raise RuntimeError(f"Config file {path} not found!") from None

        except tomllib.TOMLDecodeError as err:
            raise RuntimeError(
                f"Config file {path} has invalid YAML syntax:\n{err}"
            ) from None

        except ValidationError as err:
            raise RuntimeError(f"Invalid config file {path}:\n{err}") from None
