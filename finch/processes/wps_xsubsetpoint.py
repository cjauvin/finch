from pywps import ComplexInput, ComplexOutput, FORMATS

from . import wpsio
from .subset import finch_subset_gridpoint
from .utils import make_metalink_output, write_log
from .wps_base import FinchProcess


class SubsetGridPointProcess(FinchProcess):
    """Subset a NetCDF file grid cells using a list of coordinates."""

    def __init__(self):
        inputs = [
            ComplexInput(
                "resource",
                "NetCDF resource",
                abstract="NetCDF files, can be OPEnDAP urls.",
                max_occurs=1000,
                supported_formats=[FORMATS.NETCDF, FORMATS.DODS],
            ),
            wpsio.lon,
            wpsio.lat,
            wpsio.start_date,
            wpsio.end_date,
            wpsio.variable_any,
        ]

        outputs = [
            ComplexOutput(
                "output",
                "netCDF output",
                as_reference=True,
                supported_formats=[FORMATS.NETCDF],
            ),
            ComplexOutput(
                "ref",
                "Link to all output files",
                abstract="Metalink file storing all references to output file.",
                as_reference=False,
                supported_formats=[FORMATS.META4],
            ),
        ]

        super().__init__(
            self._handler,
            identifier="subset_gridpoint",
            title="Subset with a grid point",
            version="0.2",
            abstract=(
                "Return the data for which grid cells includes the "
                "point coordinates for each input dataset as well as "
                "the time range selected."
            ),
            inputs=inputs,
            outputs=outputs,
            status_supported=True,
            store_supported=True,
        )

        self.status_percentage_steps = {
            "start": 5,
            "done": 99,
        }

    def _handler(self, request, response):
        write_log(self, "Processing started", process_step="start")

        output_files = finch_subset_gridpoint(
            self,
            netcdf_inputs=request.inputs["resource"],
            request_inputs=request.inputs,
        )
        metalink = make_metalink_output(self, output_files)

        response.outputs["output"].file = metalink.files[0].file
        response.outputs["ref"].data = metalink.xml

        write_log(self, "Processing finished successfully", process_step="done")

        return response
