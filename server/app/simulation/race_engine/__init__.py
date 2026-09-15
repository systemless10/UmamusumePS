"""Native Python port of uma-skill-tools' race physics/skill engine.

Ported (formula-for-formula, RNG bit-for-bit) from the vendored TypeScript
project at ``uma-tools/uma-skill-tools`` so races no longer need a Node.js
subprocess. See ``server/app/simulation/race_simulator.py`` for the
integration point and the plan this was built from.
"""
