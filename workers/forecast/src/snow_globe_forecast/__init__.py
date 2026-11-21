"""snow-globe-forecast: HRRR byte-range extraction at each viewpoint's sample points (PRD §8.3,
§11.2).

Modules: :mod:`.hrrr` (bucket layout, run arithmetic), :mod:`.bucket` (anonymous HTTPS,
ListObjectsV2), :mod:`.discovery` (runs and hours), :mod:`.idx` (inventories, byte ranges),
:mod:`.fields` (catalogue and datums), :mod:`.grid` (Lambert conformal mapping),
:mod:`.points` (sample points), :mod:`.decode` (ecCodes), :mod:`.availability` (lag model,
run selection), :mod:`.output` (file schema), :mod:`.extract` (one run end to end),
:mod:`.backoff` (run-level back-off), :mod:`.evidence` (grid, datum and lag checks),
:mod:`.cli` and :mod:`.lambda_handler`.

Until 0.2.0 this was ``lookout-forecast`` (import ``lookout_forecast``). 0.3.0 is the same
worker under the Snow Globe names (D-080): it writes ``kind: snow-globe.forecast-run`` and
``software.snow_globe_forecast``, and reads the files and logs written before as they are.
"""

__version__ = "0.3.0"
