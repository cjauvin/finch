import os

import sentry_sdk
from pywps.app.Service import Service

from .processes import get_processes

SENTRY_ENV = os.environ.get("SENTRY_ENV", "production")

if os.environ.get("SENTRY_DSN"):
    sentry_sdk.init(os.environ["SENTRY_DSN"], SENTRY_ENV)


def create_app(cfgfiles=None):
    config_files = [os.path.join(os.path.dirname(__file__), "default.cfg")]
    if isinstance(cfgfiles, str):
        cfgfiles = [cfgfiles]
    if cfgfiles:
        config_files += cfgfiles
    if "PYWPS_CFG" in os.environ:
        config_files.append(os.environ["PYWPS_CFG"])
    service = Service(cfgfiles=config_files)

    # delay the call to get_processes() so that the configuration is loaded
    # when instantiating the service
    service.processes = {p.identifier: p for p in get_processes()}

    return service


application = create_app()
