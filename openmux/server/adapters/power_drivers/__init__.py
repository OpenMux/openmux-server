"""PDU power drivers, one module per driver.

The driver API lives in :mod:`.api` (``PduDriver`` + ``OutletReading``),
the shared read-failure backoff in :mod:`.readbackoff`. ``pdu.py`` holds
the adapter and the driver registries; each driver module registers its
class and Config-Editor metadata there under a ``driver:`` config key.
"""
