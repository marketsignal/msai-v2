"""Live trading services package.

Houses the supervisor-side and FastAPI-side helpers that coordinate the
deployment lifecycle described in the Phase 1 hardening plan:

- ``deployment_identity`` — canonical-JSON sha256 identity model
  (decision #7) used to key ``live_deployments`` rows so warm-restart
  vs cold-start is unambiguous.
"""
