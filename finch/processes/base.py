import zipfile

from dask.diagnostics import ProgressBar
from dask.diagnostics.progress import format_time
from pathlib import Path
from pywps import Process
from sentry_sdk import configure_scope
import xarray as xr
import logging
import os
from functools import wraps

from finch.processes.utils import is_opendap_url

LOGGER = logging.getLogger("PYWPS")

bccaqv2_link = "https://boreas.ouranos.ca/thredds/catalog/birdhouse/pcic/BCCAQv2/catalog.xml"


class FinchProcess(Process):
    def __init__(self, *args, **kwargs):
        def handler_wrapper(f):
            """Wrap the handler to call sentry initialization first."""
            @wraps(f)
            def wrapper(self_, request, response):
                self.sentry_configure_scope(request)
                return f(self_, request, response)
            return wrapper

        args = (handler_wrapper(args[0]), *args[1:])
        super().__init__(*args, **kwargs)

    def try_opendap(self, input, chunks=None):
        """Try to open the file as an OPeNDAP url and chunk it. If OPeNDAP fails, access the file directly. In both
        cases, return an xarray.Dataset.
        """
        url = input.url
        if is_opendap_url(url):
            ds = xr.open_dataset(url, chunks=chunks)
            if not chunks:
                ds = ds.chunk(chunk_dataset(ds, max_size=1000000))
            self.write_log("Opened dataset as an OPeNDAP url: {}".format(url))
        else:
            self.write_log("Downloading dataset for url: {}".format(url))
            # Accessing the file property loads the data in the data property
            # and writes it to disk
            ds = xr.open_dataset(input.file)

        return ds

    def log_file_path(self):
        return os.path.join(self.workdir, "log.txt")

    def write_log(self, message, response=None, percentage=None):
        open(self.log_file_path(), "a").write(message + "\n")
        LOGGER.info(message)
        if response:
            response.update_status(message, status_percentage=percentage)

    def sentry_configure_scope(self, request):
        """Add additional data to sentry error messages.

        When sentry is not initialized, this won't add any overhead.
        """
        with configure_scope() as scope:
            scope.set_extra("identifier", self.identifier)
            scope.set_extra("request_uuid", str(self.uuid))
            if request.http_request:
                # if the request has been put in the `stored_requests` table by pywps
                # the original request.http_request is not available anymore
                scope.set_extra("remote_addr", request.http_request.remote_addr)
                scope.set_extra("xml_request", request.http_request.data)

    def zip_metalink(self, output_filename, metalink, response, start_percentage=90):
        with zipfile.ZipFile(output_filename, mode="w") as z:
            n_files = len(metalink.files)
            for n, mf in enumerate(metalink.files):
                percentage = start_percentage + int(n / n_files * (100 - start_percentage))
                self.write_log(f"Zipping file {n + 1} of {n_files}", response, percentage)
                z.write(mf.file, arcname=Path(mf.file).name)


def chunk_dataset(ds, max_size=1000000):
    """Ensures the chunked size of a xarray.Dataset is below a certain size

    Cycle through the dimensions, divide the chunk size by 2 until criteria is met.
    """
    from functools import reduce
    from operator import mul
    from itertools import cycle

    chunks = dict(ds.sizes)

    def chunk_size():
        return reduce(mul, chunks.values())

    for dim in cycle(chunks):
        if chunk_size() < max_size:
            break
        chunks[dim] = max(chunks[dim] // 2, 1)

    return chunks


class FinchProgress(ProgressBar):
    def __init__(self, logging_function, start_percentage, *args, **kwargs):
        super(FinchProgress, self).__init__(*args, **kwargs)
        self._logging_function = logging_function
        self._start_percentage = start_percentage

    def _draw_bar(self, frac, elapsed):
        start = self._start_percentage / 100

        frac += start - frac * start
        bar = "#" * int(self._width * frac)
        percent = int(100 * frac)
        elapsed = format_time(elapsed)
        msg = "[{0:<{1}}] | {2}% Done | {3}".format(bar, self._width, percent, elapsed)

        self._logging_function(msg, percent)
