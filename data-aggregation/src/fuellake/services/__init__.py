# fuel-lake — services package (read-only HTTP API and an example processing microservice)
# Licensed under Apache-2.0.
"""Services built on the consumer SDK.

- :mod:`fuellake.services.api`: read-only STAC-API-lite (FastAPI ``app``, :func:`~fuellake.services.api.create_app`),
  served by ``fuellake serve``.
- :mod:`fuellake.services.example_consumer`: example processing microservice (AOI statistics per new item).

See ``docs/microservices.md`` for how to write a new processing microservice.
"""
