"""lookout-forecast: HRRR byte-range extraction at each viewpoint's sample points (PRD §8.3, §11.2).

Modules: :mod:`.hrrr` (bucket layout, run arithmetic), :mod:`.bucket` (anonymous HTTPS,
ListObjectsV2), :mod:`.discovery` (runs and hours), :mod:`.idx` (inventories, byte ranges),
:mod:`.fields` (catalogue and datums), :mod:`.grid` (Lambert conformal mapping),
:mod:`.points` (sample points), :mod:`.decode` (ecCodes), :mod:`.availability` (lag model,
run selection), :mod:`.output` (file schema), :mod:`.extract` (one run end to end),
:mod:`.backoff` (run-level back-off), :mod:`.evidence` (grid, datum and lag checks),
:mod:`.cli` and :mod:`.lambda_handler`.
"""

__version__ = "0.1.0"
