# Test host record

Pinned Elasticsearch image for every probe and security run (BUILD-PLAN 1.2).
The probe gate (`tools/probe_gate.py`) refuses a probe result whose recorded
image digest differs from this one.

ES-image-digest: sha256:79b4f1de4c15e6eca824723086a51e5934406d614d410cc11be77748cafac537
ES-version: 9.5.6
