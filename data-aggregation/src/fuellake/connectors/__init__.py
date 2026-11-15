# fuel-lake — source connectors package (concrete connectors are loaded by name, never imported here)
# Licensed under Apache-2.0.
"""Source connectors. Each dataset YAML names its connector as ``fuellake.connectors.<module>:<Class>``.

Only the base types are exported; concrete connector modules are imported dynamically by
:func:`fuellake.registry.load_connector_class`.
"""

from fuellake.connectors.base import (
    BaseConnector,
    Period,
    SourceRef,
    StagedAsset,
    StagedItem,
    StubConnector,
)

__all__ = ["BaseConnector", "Period", "SourceRef", "StagedAsset", "StagedItem", "StubConnector"]
