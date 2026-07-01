"""The hub's route handlers, one module per concern.

Each handler is thin — parse the request, call the Hub state-holder (and, as
the architecture plan's P3/P4 land, the app/ services), serialise the answer.
Handlers reach the shared state via ``request.app.state.hub`` (set by
``create_app``), so they are plain module-level functions: no god-object class
to grow, and the route-table test (tests/test_hub_routes.py) pins the full set.
"""
